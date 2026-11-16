"""Propose a fix for a broken scraper step with a local model. Human in the loop.

Flow: load a failure bundle -> build the prompt from the scrubbed snapshot ->
blocking check (abort on any known sensitive value) -> print the exact prompt
and ask before sending -> model answers in a JSON schema that can only say
"change this locator" or "add one click/fill/wait/dismiss step" -> validate
against the schema, the script rules and the URL allowlist -> show a diff ->
ask again -> write the script and commit. Nothing is ever applied automatically.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .notify import Notifier
from .scrub import KEEP_ROLES, blocking_check
from .steps import Script, ScriptError

REPAIR_ROLES = sorted(KEEP_ROLES - {"heading", "dialog"})
LOCATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": REPAIR_ROLES + ["heading"]},
        "name": {"type": "string", "maxLength": 120},
        "label": {"type": "string", "maxLength": 120},
        "text": {"type": "string", "maxLength": 120},
        "placeholder": {"type": "string", "maxLength": 120},
        "testid": {"type": "string", "maxLength": 80},
    },
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["change_locator", "add_step"]},
        "phase": {"type": "string", "enum": ["login", "steps"]},
        "step_index": {"type": "integer", "minimum": 0},
        "field": {"type": "string", "enum": ["locator", "detect", "input", "submit"]},
        "locator": LOCATOR_SCHEMA,
        "new_step": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["click", "fill", "wait", "dismiss"]},
                "intent": {"type": "string", "maxLength": 120},
                "locator": LOCATOR_SCHEMA,
                "secret": {"type": "string", "maxLength": 80},
            },
            "required": ["action", "intent", "locator"],
            "additionalProperties": False,
        },
        "reason": {"type": "string", "maxLength": 300},
    },
    "required": ["kind", "phase", "step_index", "reason"],
    "additionalProperties": False,
}

# Labels a repair must never point the scraper at, on top of the allowlist's
# URL block patterns. English and French.
FORBIDDEN_LABELS = re.compile(
    r"transfer|e-?transfer|interac|send\s*money|pay\b|pay\s*(a\s*)?bill|payee|payment|wire|withdraw|"
    r"buy|sell|trade|order|delete|remove|close\s+account|add\s+(a\s+)?(payee|recipient)|"
    r"virement|paiement|payer|bénéficiaire|beneficiaire|retrait|acheter|vendre|supprimer",
    re.IGNORECASE,
)
URLISH = re.compile(r"\b(?:https?|javascript|data|file|ftp|blob):|www\.|//|\.(?:com|ca|net|org)\b", re.IGNORECASE)


class RepairRejected(ValueError):
    pass


# -- prompt ------------------------------------------------------------------


def build_prompt(bundle: dict) -> str:
    script = bundle["script"]
    steps = "\n".join(
        f"  {phase}[{i}] {s['action']}: {s['intent']}"
        for phase in ("login", "steps")
        for i, s in enumerate(script[phase])
    )
    failing = json.dumps({k: v for k, v in bundle["step"].items() if k not in ("url",)}, ensure_ascii=False)
    first_error_line = (bundle.get("error") or "").splitlines()[0][:200] if bundle.get("error") else ""
    return f"""A browser automation script for a website stopped at one step. Propose ONE minimal fix.

Script steps (phase[index] action: intent):
{steps}

Failed: {bundle['phase']}[{bundle['step_index']}] {failing}
Error: {bundle['error_type']}: {first_error_line}
Page path: {bundle.get('page') or '(unknown)'}

Interactive elements on the page (accessibility tree; ‹x› = hidden word, # = hidden number):
{bundle['snapshot']}

Answer in the given JSON schema. Either:
- kind "change_locator": the same step, pointed at the element that now matches its intent
  ("field" is "locator" unless the step is an mfa step), or
- kind "add_step": insert a click/wait/dismiss step before step_index (e.g. to close a new pop-up).
Use locators like {{"role": "button", "name": "Download"}} or {{"label": "Card number"}}.
Never target anything that moves money, pays, transfers, trades or deletes.
Explain in "reason" in one sentence."""


# -- validation --------------------------------------------------------------


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def _check_locator(loc: Any, where: str) -> None:
    if not isinstance(loc, dict):
        raise RepairRejected(f"{where}: locator must be an object")
    allowed = set(LOCATOR_SCHEMA["properties"])
    extra = set(loc) - allowed
    if extra:
        raise RepairRejected(f"{where}: unknown locator keys {sorted(extra)}")
    kinds = [k for k in ("role", "label", "text", "placeholder", "testid") if k in loc]
    if len(kinds) != 1:
        raise RepairRejected(f"{where}: locator needs exactly one of role/label/text/placeholder/testid")
    if "name" in loc and "role" not in loc:
        raise RepairRejected(f"{where}: only role locators take a name")
    if "role" in loc and loc["role"] not in LOCATOR_SCHEMA["properties"]["role"]["enum"]:
        raise RepairRejected(f"{where}: role {loc['role']!r} not allowed")


def validate_proposal(proposal: Any, script: Script) -> None:
    if not isinstance(proposal, dict):
        raise RepairRejected("response is not a JSON object")
    extra = set(proposal) - set(SCHEMA["properties"])
    if extra:
        raise RepairRejected(f"unknown keys {sorted(extra)}")
    for key in SCHEMA["required"]:
        if key not in proposal:
            raise RepairRejected(f"missing {key}")
    kind, phase, idx = proposal["kind"], proposal["phase"], proposal["step_index"]
    if kind not in ("change_locator", "add_step"):
        raise RepairRejected(f"kind {kind!r} not allowed")
    if phase not in ("login", "steps"):
        raise RepairRejected(f"phase {phase!r} not allowed")
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise RepairRejected("step_index must be an integer")
    steps = script.login if phase == "login" else script.steps
    if not isinstance(proposal["reason"], str) or len(proposal["reason"]) > 300:
        raise RepairRejected("reason must be a short string")

    for s in _strings({k: v for k, v in proposal.items() if k != "reason"}):
        if any(ord(c) < 32 for c in s) or len(s) > 120:
            raise RepairRejected("strings must be short single lines")
        if URLISH.search(s):
            # Repairs never carry URLs; navigation is fixed by the human-written goto.
            if not (s.startswith("http") and script.allowlist.decide(s, "GET", True) == "allow"):
                raise RepairRejected(f"URL-like value not on the allowlist: {s[:60]!r}")
        if script.allowlist.is_blocked(s) or FORBIDDEN_LABELS.search(s):
            raise RepairRejected(f"value targets a forbidden action: {s[:60]!r}")

    if kind == "change_locator":
        if not 0 <= idx < len(steps):
            raise RepairRejected(f"step_index {idx} out of range for {phase}")
        field = proposal.get("field", "locator")
        if getattr(steps[idx], field, None) is None:
            raise RepairRejected(f"{phase}[{idx}] has no {field}")
        if "locator" not in proposal or "new_step" in proposal:
            raise RepairRejected("change_locator takes a locator and no new_step")
        _check_locator(proposal["locator"], "locator")
    else:
        if not 0 <= idx <= len(steps):
            raise RepairRejected(f"step_index {idx} out of range for {phase}")
        new = proposal.get("new_step")
        if not isinstance(new, dict) or "locator" in proposal or "field" in proposal:
            raise RepairRejected("add_step takes a new_step and nothing else")
        extra = set(new) - set(SCHEMA["properties"]["new_step"]["properties"])
        if extra:
            raise RepairRejected(f"unknown new_step keys {sorted(extra)}")
        if new.get("action") not in ("click", "fill", "wait", "dismiss"):
            raise RepairRejected(f"action {new.get('action')!r} not allowed")
        if not isinstance(new.get("intent"), str) or not new["intent"].strip():
            raise RepairRejected("new_step needs an intent")
        _check_locator(new.get("locator"), "new_step.locator")
        if new["action"] == "fill":
            if phase != "login" or new.get("secret") not in script.secrets():
                raise RepairRejected("fill may only reuse a secret the login already uses")
        elif "secret" in new:
            raise RepairRejected("only fill steps take a secret")


def apply_proposal(script: Script, proposal: dict) -> dict:
    data = script.to_dict()
    steps = data[proposal["phase"]]
    if proposal["kind"] == "change_locator":
        steps[proposal["step_index"]][proposal.get("field", "locator")] = proposal["locator"]
    else:
        steps.insert(proposal["step_index"], proposal["new_step"])
    try:
        Script.parse(data)
    except ScriptError as exc:
        raise RepairRejected(f"the fixed script would be invalid: {exc}") from None
    return data


def diff(before: dict, after: dict, name: str) -> str:
    a = json.dumps(before, indent=2, ensure_ascii=False).splitlines(keepends=True)
    b = json.dumps(after, indent=2, ensure_ascii=False).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"a/{name}", f"b/{name}"))


# -- model -------------------------------------------------------------------


class ModelOffline(RuntimeError):
    pass


@dataclass
class ModelEndpoint:
    url: str = ""  # from repair.url in the config: Ollama on a GPU machine on the tailnet
    model: str = "qwen2.5:7b-instruct-q4_K_M"
    timeout: float = 600

    def ask(self, prompt: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "format": SCHEMA,
            "stream": False,
            "keep_alive": 0,
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.url.rstrip('/')}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                reply = json.loads(resp.read())
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ModelOffline(str(exc)) from None
        content = reply.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise RepairRejected("model reply is not JSON") from None


# -- the command -------------------------------------------------------------


@dataclass
class RepairIO:
    out: Callable[[str], None] = print
    ask: Callable[[str], str] = input

    def confirm(self, question: str) -> bool:
        return self.ask(f"{question} Type 'yes' to continue: ").strip().lower() == "yes"


def queue_bundle(bundle_dir: Path, queue_dir: Path, notifier: Notifier) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    entry = queue_dir / bundle_dir.name
    if not entry.exists():
        shutil.copytree(bundle_dir, entry, ignore=shutil.ignore_patterns("*.local.png"))
    notifier.notify("A scraper repair is queued until the model is reachable.")
    return entry


def commit(script_path: Path, message: str, body: str) -> bool:
    repo = subprocess.run(["git", "-C", str(script_path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if repo.returncode != 0:
        return False
    subprocess.run(["git", "-C", str(script_path.parent), "add", str(script_path)], check=True)
    subprocess.run(["git", "-C", str(script_path.parent), "commit", "-m", message, "-m", body], check=True)
    return True


def repair(
    bundle_dir: Path,
    *,
    endpoint: ModelEndpoint,
    sensitive_values: list[str],
    recent_amounts: list[Decimal],
    io: RepairIO,
    notifier: Notifier,
    queue_dir: Path,
    script_path: Path | None = None,
) -> str:
    """Returns one of: blocked, declined, queued, rejected, not_applied, applied."""
    bundle = json.loads((bundle_dir / "bundle.json").read_text())
    script_path = script_path or (Path(bundle["script_path"]) if bundle.get("script_path") else None)
    if script_path is None or not script_path.exists():
        raise FileNotFoundError("the script this bundle came from is not on disk; pass --script")
    script = Script.load(script_path)

    prompt = build_prompt(bundle)
    try:
        blocking_check(prompt, sensitive_values, recent_amounts)
    except Exception as exc:  # SensitiveValueFound
        io.out(f"BLOCKED: {exc}. Nothing was sent. Fix the scrubber or vocabulary, then retry.")
        return "blocked"

    io.out("=" * 72 + "\nThis exact text would be sent to " + f"{endpoint.url} ({endpoint.model}):\n" + "=" * 72)
    io.out(prompt)
    io.out("=" * 72)
    if not io.confirm("Send it?"):
        return "declined"

    try:
        proposal = endpoint.ask(prompt)
    except ModelOffline as exc:
        entry = queue_bundle(bundle_dir, queue_dir, notifier)
        io.out(f"Model endpoint unreachable ({exc}); queued at {entry}.")
        return "queued"

    try:
        validate_proposal(proposal, script)
        new_data = apply_proposal(script, proposal)
    except RepairRejected as exc:
        io.out(f"REJECTED model proposal: {exc}\n{json.dumps(proposal, indent=2, ensure_ascii=False)[:2000]}")
        return "rejected"

    rel = script_path.name
    io.out(f"Reason given: {proposal['reason']}\n")
    io.out(diff(script.to_dict(), new_data, rel))
    if not io.confirm("Apply this change and commit it?"):
        return "not_applied"

    script_path.write_text(json.dumps(new_data, indent=2, ensure_ascii=False) + "\n")
    where = f"{proposal['phase']}[{proposal['step_index']}]"
    verb = "Fix locator for" if proposal["kind"] == "change_locator" else "Add step before"
    message = f"Repair {script.institution}: {verb} {where}"[:72]
    body = f"{proposal['reason']}\n\nProposed by {endpoint.model} from bundle {bundle_dir.name} " \
           f"on {dt.date.today().isoformat()}; reviewed and approved by hand."
    if commit(script_path, message, body):
        io.out("Applied and committed.")
    else:
        io.out("Applied. Not a git checkout, so nothing was committed.")
    return "applied"
