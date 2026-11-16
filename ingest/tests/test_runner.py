import json
import stat

import pytest

from ingest.notify import MfaTimeout, NullNotifier
from ingest.runner import RunConfig, Runner
from ingest.secrets import DictStore
from ingest.steps import Script

from . import fakebank

pytestmark = pytest.mark.browser

SECRETS = {"fakebank/username": fakebank.USERNAME, "fakebank/password": fakebank.PASSWORD}


class Codes:
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
