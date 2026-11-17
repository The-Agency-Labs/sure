import json
import stat

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
        channel=None,
        step_timeout_ms=3000,
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
