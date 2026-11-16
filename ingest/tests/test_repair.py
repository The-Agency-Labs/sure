import json
import subprocess

import pytest

from ingest.notify import NullNotifier
from ingest.repair import ModelEndpoint, ModelOffline, RepairIO, RepairRejected, SCHEMA, apply_proposal, build_prompt, repair, validate_proposal
from ingest.steps import Script

from . import fakebank

BASE = "https://bank.example"


@pytest.fixture
def script():
    return Script.parse(fakebank.script_dict(BASE))


def change(**kw):
    p = {"kind": "change_locator", "phase": "steps", "step_index": 1, "locator": {"role": "button", "name": "Export activity"}, "reason": "Button was renamed."}
    p.update(kw)
    return p


def test_valid_locator_change_applies(script):
    validate_proposal(change(), script)
    data = apply_proposal(script, change())
    assert data["steps"][1]["locator"] == {"role": "button", "name": "Export activity"}
    assert data["steps"][1]["intent"] == "download transactions"


def test_valid_added_dismiss_step(script):
    p = {"kind": "add_step", "phase": "steps", "step_index": 1, "reason": "New cookie banner.",
         "new_step": {"action": "dismiss", "intent": "close the cookie banner", "locator": {"role": "button", "name": "Accept"}}}
    validate_proposal(p, script)
    assert apply_proposal(script, p)["steps"][1]["action"] == "dismiss"


@pytest.mark.parametrize(
    "proposal,match",
    [
        (change(kind="run_code"), "kind"),
        (change(code="import os; os.system('x')"), "unknown keys"),
        (change(locator={"css": "button"}), "unknown locator keys"),
        (change(locator={"role": "button", "name": "x", "label": "y"}), "exactly one"),
        (change(locator={"role": "button", "name": "Transfer funds"}), "forbidden"),
        (change(locator={"role": "link", "name": "Virement Interac"}), "forbidden"),
        (change(locator={"role": "button", "name": "Pay a bill"}), "forbidden"),
        (change(locator={"text": "javascript:alert(1)"}), "URL-like"),
        (change(locator={"text": "https://evil.example/steal"}), "URL-like"),
        (change(step_index=9), "out of range"),
        (change(step_index="1"), "integer"),
        (change(field="detect"), "has no detect"),
        ({"kind": "add_step", "phase": "steps", "step_index": 0, "reason": "r",
          "new_step": {"action": "goto", "intent": "go", "locator": {"text": "x"}}}, "not allowed"),
        ({"kind": "add_step", "phase": "steps", "step_index": 0, "reason": "r",
          "new_step": {"action": "fill", "intent": "type", "locator": {"label": "x"}, "secret": "fakebank/password"}}, "fill may only"),
        ({"kind": "add_step", "phase": "login", "step_index": 1, "reason": "r",
          "new_step": {"action": "fill", "intent": "type", "locator": {"label": "x"}, "secret": "other/password"}}, "fill may only"),
        ({"kind": "add_step", "phase": "steps", "step_index": 0, "reason": "r",
          "new_step": {"action": "click", "intent": "x", "locator": {"text": "x"}, "value": "4500"}}, "unknown new_step keys"),
        ("not json object", "object"),
    ],
)
def test_rejections(script, proposal, match):
    with pytest.raises(RepairRejected, match=match):
        validate_proposal(proposal, script)


def test_schema_only_offers_safe_actions():
    assert SCHEMA["properties"]["new_step"]["properties"]["action"]["enum"] == ["click", "fill", "wait", "dismiss"]
    assert SCHEMA["additionalProperties"] is False


# -- the interactive command, with a stub model --------------------------------


class StubModel(ModelEndpoint):
    def __init__(self, reply=None, offline=False):
        super().__init__(url="http://stub", model="stub")
        self.reply, self.offline, self.prompts = reply, offline, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if self.offline:
            raise ModelOffline("connection refused")
        return self.reply


def make_bundle(tmp_path, script_path, snapshot='- heading "Accounts"\n- button "Export activity"'):
    script = Script.load(script_path)
    d = tmp_path / "bundles" / "fakebank-20261116T030000"
    d.mkdir(parents=True)
    bundle = {
        "institution": "fakebank", "phase": "steps", "step_index": 1, "step": script.steps[1].to_dict(),
        "error_type": "TimeoutError", "error": "Locator.click: Timeout", "page": "/accounts",
        "snapshot": snapshot, "script_path": str(script_path), "script": script.to_dict(),
    }
    (d / "bundle.json").write_text(json.dumps(bundle))
    (d / "screenshot.local.png").write_bytes(b"png")
    return d


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(r), "config", "user.name", "t"], check=True)
    path = r / "fakebank.json"
    path.write_text(json.dumps(fakebank.script_dict(BASE), indent=2))
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True)
    return path


def run(tmp_path, script_path, model, answers, snapshot=None, values=()):
    out = []
    it = iter(answers)
    bundle = make_bundle(tmp_path, script_path, **({"snapshot": snapshot} if snapshot else {}))
    result = repair(bundle, endpoint=model, sensitive_values=list(values), recent_amounts=[], io=RepairIO(out.append, lambda q: next(it)),
                    notifier=NullNotifier(), queue_dir=tmp_path / "queue")
    return result, "\n".join(out)


def test_full_flow_applies_and_commits_after_two_approvals(tmp_path, repo):
    model = StubModel(change())
    result, out = run(tmp_path, repo, model, ["yes", "yes"])
    assert result == "applied"
    assert 'button "Export activity"' in out and "Send it?" not in out  # prompt text printed
    assert '+        "name": "Export activity"' in out
    assert json.loads(repo.read_text())["steps"][1]["locator"]["name"] == "Export activity"
    log = subprocess.run(["git", "-C", str(repo.parent), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("Repair fakebank: Fix locator for steps[1]")


def test_declining_to_send_sends_nothing(tmp_path, repo):
    model = StubModel(change())
    assert run(tmp_path, repo, model, ["no"])[0] == "declined"
    assert model.prompts == []


def test_declining_the_diff_changes_nothing(tmp_path, repo):
    before = repo.read_text()
    assert run(tmp_path, repo, StubModel(change()), ["yes", "no"])[0] == "not_applied"
    assert repo.read_text() == before


def test_sensitive_value_blocks_before_anything_is_shown_or_sent(tmp_path, repo):
    model = StubModel(change())
    result, out = run(tmp_path, repo, model, [], snapshot='- heading "Hello Quixote"', values=["Jane Quixote"])
    assert result == "blocked" and model.prompts == [] and "Quixote" not in out


def test_bad_proposal_is_rejected_not_applied(tmp_path, repo):
    before = repo.read_text()
    result, _ = run(tmp_path, repo, StubModel(change(locator={"role": "link", "name": "Transfer funds"})), ["yes"])
    assert result == "rejected" and repo.read_text() == before


def test_offline_model_queues_without_screenshot(tmp_path, repo):
    result, _ = run(tmp_path, repo, StubModel(offline=True), ["yes"])
    assert result == "queued"
    [queued] = list((tmp_path / "queue").iterdir())
    assert (queued / "bundle.json").exists() and not (queued / "screenshot.local.png").exists()


def test_prompt_is_built_only_from_scrubbed_parts(tmp_path, repo):
    bundle = json.loads((make_bundle(tmp_path, repo) / "bundle.json").read_text())
    prompt = build_prompt(bundle)
    assert "bank.example" not in prompt  # goto URLs are not sent
    assert "download transactions" in prompt
