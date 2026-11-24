import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from ingest.notify import MfaTimeout, NoWaitCodes, NullNotifier
from ingest.runner import RunConfig, Runner
from ingest.secrets import DictStore
from ingest.steps import Script

from . import fakebank

pytestmark = pytest.mark.browser

SECRETS = {"fakebank/username": fakebank.USERNAME, "fakebank/password": fakebank.PASSWORD}


class Codes:
    attended = True

    def __init__(self, code=fakebank.MFA_CODE):
        self.code = code
        self.asked = 0

    def wait_for_code(self, timeout_s):
        self.asked += 1
        if self.code is None:
            raise MfaTimeout("no verification code within 1s")
        return self.code


def make_runner(tmp_path, data, secrets=SECRETS, codes=None):
    cfg = RunConfig(
        raw_root=tmp_path / "raw",
        profile_dir=tmp_path / "profile",
        bundle_root=tmp_path / "bundles",
        headless=True,
        attach=False,  # Playwright launches its bundled Chromium; see test_attached_chrome_*
        channel=None,
        step_timeout_ms=3000,
        human_pacing=False,
    )
    notifier = NullNotifier()
    runner = Runner(Script.parse(data), cfg, DictStore(secrets), codes or Codes(), notifier)
    return runner, notifier


def all_files(tmp_path):
    return [p for p in tmp_path.rglob("*") if p.is_file()]


def test_happy_path_downloads_csv_read_only(tmp_path):
    with fakebank.FakeBank() as bank:
        codes = Codes()
        runner, notifier = make_runner(tmp_path, fakebank.script_dict(bank.url), codes=codes)
        result = runner.run()

    assert result.ok, result.error
    assert codes.asked == 1
    assert notifier.sent == []
    [saved] = result.downloads
    assert saved.parent.parent.name == "fakebank"
    assert saved.name.startswith("chequing__") and saved.suffix == ".csv"
    assert saved.read_text() == fakebank.CSV
    assert not saved.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert bank.hits["POST /login"] == 1


def test_second_run_does_not_overwrite_raw_file(tmp_path):
    with fakebank.FakeBank() as bank:
        for _ in range(2):
            runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
            assert runner.run().ok
    files = sorted(p.name for p in (tmp_path / "raw").rglob("*.csv"))
    assert len(files) == 2 and files[0] != files[1]


def test_transfer_link_is_blocked_and_stops_the_run(tmp_path):
    with fakebank.FakeBank() as bank:
        data = fakebank.script_dict(bank.url)
        # Even a script that allowlists everything can't reach a money-moving page.
        data["allowlist"]["navigation"] = [rf"^{bank.url}/"]
        data["steps"].insert(1, {"action": "click", "intent": "open transfers", "locator": {"role": "link", "name": "Transfer funds"}})
        runner, notifier = make_runner(tmp_path, data)
        result = runner.run()

    assert not result.ok
    assert "UnexpectedPage" in result.error
    assert bank.hits["GET /transfer"] == 0
    assert any(pattern == "/transfer" and reason == "blocked pattern" for pattern, reason in result.aborted_requests)
    assert notifier.sent == [("Finance sync stopped: fakebank needs attention.", None)]
    assert result.downloads == []


def test_changed_selector_writes_scrubbed_failure_bundle(tmp_path):
    with fakebank.FakeBank(download_label="Export activity") as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        result = runner.run()

    assert not result.ok
    bundle_dir = result.failure_bundle
    bundle = json.loads((bundle_dir / "bundle.json").read_text())
    assert bundle["phase"] == "steps"
    assert bundle["step_index"] == 1
    assert bundle["step"]["intent"] == "download transactions"
    assert bundle["page"] == "/accounts"
    snap = bundle["snapshot"]
    assert 'button "Export activity"' in snap
    assert 'heading "Accounts"' in snap
    # Holder name, card number and balance must be gone; ids in URLs too.
    text = json.dumps(bundle)
    for leak in ("Jane", "Quixote", "4500", "9012", "1,234.56", "77812", fakebank.PASSWORD, fakebank.USERNAME):
        assert leak not in text, leak
    assert 'link "Account history"' in snap and "/accounts/:id/history" in snap
    # Screenshot only after login, and it stays local.
    assert (bundle_dir / "screenshot.local.png").exists()


def test_login_failure_bundle_has_no_screenshot_and_one_attempt(tmp_path):
    with fakebank.FakeBank() as bank:
        secrets = dict(SECRETS, **{"fakebank/password": "wrong password"})
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url), secrets=secrets)
        result = runner.run()

    assert not result.ok
    assert bank.hits["POST /login"] == 1
    bundle = json.loads((result.failure_bundle / "bundle.json").read_text())
    assert bundle["phase"] == "login"
    assert not (result.failure_bundle / "screenshot.local.png").exists()
    assert "wrong password" not in json.dumps(bundle)


def test_mfa_timeout_stops_cleanly(tmp_path):
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url), codes=Codes(code=None))
        result = runner.run()
    assert not result.ok
    assert "MfaTimeout" in result.error
    assert bank.hits["GET /accounts"] == 0


def test_no_mfa_prompt_skips_the_code_form(tmp_path):
    with fakebank.FakeBank(require_mfa=False) as bank:
        codes = Codes()
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url), codes=codes)
        assert runner.run().ok
    assert codes.asked == 0


def test_tracing_and_video_are_never_used(tmp_path, monkeypatch):
    from playwright.sync_api._generated import Tracing

    def refuse(*a, **k):
        raise AssertionError("tracing must stay off")

    monkeypatch.setattr(Tracing, "start", refuse)
    monkeypatch.setattr(Tracing, "start_chunk", refuse)
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        assert runner.run().ok
    suspicious = [p for p in all_files(tmp_path / "raw") + all_files(tmp_path / "bundles") if p.suffix in (".zip", ".webm", ".trace", ".png")]
    assert suspicious == []
    profile_traces = [p for p in all_files(tmp_path / "profile") if p.suffix in (".zip", ".webm", ".trace")]
    assert profile_traces == []


def test_unlisted_post_from_page_script_never_reaches_the_server(tmp_path):
    # Single-page apps act through XHR/fetch, not page loads; non-GET must match "post".
    with fakebank.FakeBank() as bank:
        data = fakebank.script_dict(bank.url)
        data["steps"].insert(1, {"action": "click", "intent": "press the quick action", "locator": {"role": "button", "name": "Quick action"}})
        runner, _ = make_runner(tmp_path, data)
        result = runner.run()
    assert result.ok  # a blocked XHR alone doesn't stop the run; it just never happens
    assert bank.hits["POST /api/v2/quick"] == 0
    assert ("/api/:id/quick", "POST not allowlisted") in result.aborted_requests  # logged as a scrubbed pattern


def test_remembered_device_survives_between_runs(tmp_path):
    # Same profile dir, two runs: the bank's "trusted device" cookie persists,
    # so the second night signs in without a code. This is what keeps MFA rare.
    with fakebank.FakeBank() as bank:
        first_codes, second_codes = Codes(), Codes()
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url), codes=first_codes)
        assert runner.run().ok
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url), codes=second_codes)
        assert runner.run().ok
    assert (first_codes.asked, second_codes.asked) == (1, 0)
    assert bank.hits["GET /mfa"] == 1


def test_browser_does_not_announce_automation(tmp_path):
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        assert runner.config.chromium_sandbox is True  # sandbox is on by default
        assert runner.run().ok
    [beacon] = bank.beacons
    assert beacon["webdriver"] == "false"


def test_unattended_run_stops_at_once_when_a_code_is_asked(tmp_path):
    with fakebank.FakeBank() as bank:
        notifier = NullNotifier()
        cfg_runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        runner = type(cfg_runner)(cfg_runner.script, cfg_runner.config, cfg_runner.secrets, NoWaitCodes(notifier, "fakebank"), notifier)
        result = runner.run()
    assert not result.ok and "MfaNeeded" in result.error
    assert notifier.sent[0][0] == "Finance sync: fakebank wants a verification code. Run it by hand when you can."
    assert bank.hits["POST /mfa"] == 0


def test_balance_step_saves_the_shown_balance(tmp_path):
    import json as _json

    with fakebank.FakeBank() as bank:
        data = fakebank.script_dict(bank.url)
        data["steps"] = [
            data["steps"][0],
            {"action": "balance", "intent": "read the plan balance", "locator": {"testid": "plan-balance"}, "save_as": "pension__{today}.{ext}"},
        ]
        runner, _ = make_runner(tmp_path, data)
        result = runner.run()
    assert result.ok, result.error
    [saved] = result.downloads
    assert saved.name.startswith("pension__") and saved.suffix == ".json"
    body = _json.loads(saved.read_text())
    assert body["kind"] == "balance" and body["text"] == "Total balance $1,234.56"
    assert not saved.stat().st_mode & 0o222


def _send_code_script(base):
    data = fakebank.script_dict(base)
    mfa = next(s for s in data["login"] if s["action"] == "mfa")
    mfa["request"] = {"role": "button", "name": "Send code"}
    mfa["remember"] = {"label": "Remember this device"}
    return data


def test_unattended_run_does_not_ask_the_bank_to_send_a_code(tmp_path):
    with fakebank.FakeBank(send_code=True) as bank:
        notifier = NullNotifier()
        base_runner, _ = make_runner(tmp_path, _send_code_script(bank.url))
        runner = type(base_runner)(base_runner.script, base_runner.config, base_runner.secrets, NoWaitCodes(notifier, "fakebank"), notifier)
        result = runner.run()
    assert not result.ok and "MfaNeeded" in result.error
    assert bank.hits["POST /mfa/send"] == 0  # nothing was sent at 3 a.m.


def test_attended_run_requests_the_code_and_ticks_remember(tmp_path):
    codes = Codes()
    with fakebank.FakeBank(send_code=True) as bank:
        runner, _ = make_runner(tmp_path, _send_code_script(bank.url), codes=codes)
        assert runner.run().ok
        assert bank.hits["POST /mfa/send"] == 1
        # "Remember this device" was ticked, so the next run is not asked again.
        again, _ = make_runner(tmp_path, _send_code_script(bank.url), codes=codes)
        assert again.run().ok
    assert codes.asked == 1


def _dropdown_script(base):
    data = fakebank.script_dict(base)
    data["steps"] = [
        data["steps"][0],
        {"action": "click", "intent": "open download transactions", "locator": {"role": "link", "name": "Download page"}},
        {"action": "select", "intent": "pick the card", "locator": {"label": "Account"}, "option_secret": "fakebank/card"},
        {"action": "select", "intent": "pick CSV", "locator": {"label": "Format"}, "option": "CSV"},
        {"action": "download", "intent": "download", "locator": {"role": "button", "name": "Download"}, "save_as": "card__{today}.{ext}"},
    ]
    return data


def test_select_picks_the_account_by_a_stored_value(tmp_path):
    secrets = dict(SECRETS, **{"fakebank/card": "9911"})
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, _dropdown_script(bank.url), secrets=secrets)
        result = runner.run()
    assert result.ok, result.error
    assert bank.exports == ["acct=a2&fmt=CSV"]
    assert result.downloads[0].name.startswith("card__")


def test_select_refuses_an_ambiguous_stored_value(tmp_path):
    secrets = dict(SECRETS, **{"fakebank/card": "991"})  # matches two cards
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, _dropdown_script(bank.url), secrets=secrets)
        result = runner.run()
    assert not result.ok and "UnexpectedPage" in result.error
    assert bank.exports == []
    bundle = (result.failure_bundle / "bundle.json").read_text()
    assert "991" not in bundle.replace("#", "")


def test_nothing_to_download_is_skipped_and_the_run_continues(tmp_path):
    secrets = dict(SECRETS, **{"fakebank/card": "9911", "fakebank/quiet": "9912"})
    with fakebank.FakeBank() as bank:
        data = _dropdown_script(bank.url)
        empty = {"text": "There are no transactions found"}
        data["steps"][-1]["empty"] = empty
        quiet = [dict(s) for s in data["steps"][1:]]
        quiet[1]["option_secret"] = "fakebank/quiet"
        quiet[-1]["save_as"] = "quiet__{today}.{ext}"
        # The quiet account first: its message shows, then the card still downloads.
        data["steps"] = [data["steps"][0], *quiet, *data["steps"][1:]]
        runner, _ = make_runner(tmp_path, data, secrets=secrets)
        result = runner.run()
    assert result.ok, result.error
    assert [p.name.split("__")[0] for p in result.downloads] == ["card"]
    assert bank.exports == ["acct=a2&fmt=CSV"]


def _every_account_script(base):
    data = _dropdown_script(base)
    data["for_each_account"] = {"label": "Account"}
    data["steps"][2] = {"action": "select", "intent": "pick the next account", "locator": {"label": "Account"}, "option": "{account}"}
    data["steps"][-1]["save_as"] = "{account}__{today}.{ext}"
    data["steps"][-1]["empty"] = {"text": "There are no transactions found"}
    return data


def test_every_account_in_the_dropdown_is_downloaded(tmp_path):
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, _every_account_script(bank.url))
        result = runner.run()
    assert result.ok, result.error
    assert bank.exports == ["acct=a1&fmt=CSV", "acct=a2&fmt=CSV"]  # a3 has nothing to download
    assert [p.name.split("__")[0] for p in result.downloads] == ["chequing-1234", "visa-ending-9911"]


def test_every_account_script_must_name_files_per_account(tmp_path):
    from ingest.steps import ScriptError

    data = _every_account_script("http://127.0.0.1:1")
    data["steps"][-1]["save_as"] = "card__{today}.{ext}"
    with pytest.raises(ScriptError, match="overwrites"):
        Script.parse(data)
    data = _every_account_script("http://127.0.0.1:1")
    del data["for_each_account"]
    with pytest.raises(ScriptError, match="needs for_each_account"):
        Script.parse(data)


def test_account_labels_stay_out_of_failure_bundles(tmp_path):
    with fakebank.FakeBank() as bank:
        data = _every_account_script(bank.url)
        data["steps"][3]["option"] = "PDF"  # no such format: fails after the account is chosen
        runner, _ = make_runner(tmp_path, data)
        result = runner.run()
    assert not result.ok
    bundle = (result.failure_bundle / "bundle.json").read_text()
    assert "Visa ending 9911" not in bundle and "Chequing 4455-1234" not in bundle


def test_scroll_loads_a_panel_that_appears_only_in_view(tmp_path):
    with fakebank.FakeBank() as bank:
        data = _dropdown_script(bank.url)
        data["steps"][2:2] = [
            {"action": "click", "intent": "open the long page", "locator": {"role": "link", "name": "Long page"}},
            {"action": "scroll", "intent": "bring the extras panel into view", "locator": {"role": "link", "name": "Download Transactions"}},
            {"action": "click", "intent": "open download transactions", "locator": {"role": "link", "name": "Download Transactions"}},
        ]
        runner, _ = make_runner(tmp_path, data, secrets=dict(SECRETS, **{"fakebank/card": "9911"}))
        result = runner.run()
    assert result.ok, result.error
    assert bank.exports == ["acct=a2&fmt=CSV"]


def test_without_scrolling_the_lazy_panel_never_appears(tmp_path):
    with fakebank.FakeBank() as bank:
        data = _dropdown_script(bank.url)
        data["steps"][2:2] = [
            {"action": "click", "intent": "open the long page", "locator": {"role": "link", "name": "Long page"}},
            {"action": "click", "intent": "open download transactions", "locator": {"role": "link", "name": "Download Transactions"}},
        ]
        runner, _ = make_runner(tmp_path, data, secrets=dict(SECRETS, **{"fakebank/card": "9911"}))
        result = runner.run()
    assert not result.ok and "open download transactions" in result.error


def _history_script(base):
    data = _dropdown_script(base)
    fmt = data["steps"][3]
    data["steps"][3:4] = [
        dict(fmt, intent="recent: Quicken", option="Quicken", history="recent"),
        dict(fmt, intent="full: CSV", option="CSV", history="full"),
    ]
    return data


@pytest.mark.parametrize("full, fmt", [(False, "Quicken"), (True, "CSV")])
def test_history_steps_run_only_on_their_kind_of_run(tmp_path, full, fmt):
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, _history_script(bank.url), secrets=dict(SECRETS, **{"fakebank/card": "9911"}))
        runner.config.full_history = full
        result = runner.run()
    assert result.ok, result.error
    assert bank.exports == [f"acct=a2&fmt={fmt}"]


def test_dismiss_with_escape_closes_a_popup_without_clicking_it(tmp_path):
    with fakebank.FakeBank() as bank:
        data = fakebank.script_dict(bank.url)
        for step in data["login"] + data["steps"]:
            if step["action"] == "dismiss":
                step["locator"] = {"role": "button", "name": "Yes, I want help"}
                step["key"] = "Escape"
        runner, _ = make_runner(tmp_path, data)
        result = runner.run()
    assert result.ok, result.error
    assert bank.hits["GET /help-signup"] == 0


def test_human_pacing_types_the_same_values_and_finishes(tmp_path):
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        runner.config.human_pacing = True
        result = runner.run()
    assert result.ok, result.error
    assert result.downloads


def test_test_ids_are_listed_when_the_page_disables_eval(tmp_path):
    from playwright.sync_api import sync_playwright

    runner, _ = make_runner(tmp_path, fakebank.script_dict("http://127.0.0.1:1"))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(
            '<button data-testid="submit-button">Go</button>'
            '<div data-testid="myca/Table/latest-transactions/2026-11-04/download-button">x</div>'
            '<span data-testid="Jane Doe">n</span>'
            '<script>window.eval = () => { throw new Error("eval is disabled") }</script>'
        )
        ids = runner._test_ids(page)
        browser.close()
    assert "submit-button" in ids
    assert "myca/Table/latest-transactions/#-11-04/download-button" in ids
    assert not any("Jane" in i for i in ids)


@pytest.mark.skipif(not shutil.which("google-chrome"), reason="needs Google Chrome installed")
def test_attached_chrome_runs_the_fake_bank_and_saves_the_profile(tmp_path):
    """Production path: plain google-chrome, Playwright attached over CDP."""
    with fakebank.FakeBank() as bank:
        runner, _ = make_runner(tmp_path, fakebank.script_dict(bank.url))
        runner.config.attach = True
        runner.config.chromium_sandbox = os.environ.get("INGEST_TEST_SANDBOX") == "1"
        result = runner.run()
    assert result.ok, result.error
    assert result.downloads
    # Chrome closed cleanly: the profile is on disk and unlocked.
    profile = tmp_path / "profile"
    assert (profile / "Default").is_dir()
    assert not (profile / "SingletonLock").exists()


def test_chrome_command_matches_the_sign_in_window(tmp_path):
    from ingest.runner import chrome_command

    cfg = RunConfig(raw_root=tmp_path, profile_dir=tmp_path / "p", bundle_root=tmp_path, locale="en-CA")
    cmd = chrome_command(cfg)
    attend = (Path(__file__).parents[1] / "deploy/ingest/scraper-attend.sh").read_text()
    for flag in cmd[2:-2]:  # after binary + profile, before the debug port + URL
        assert flag.split("=")[0] in attend, flag
    assert not any(f.startswith(("--enable-automation", "--disable-", "--remote-debugging-pipe")) for f in cmd)
