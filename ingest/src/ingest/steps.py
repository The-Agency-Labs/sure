"""Scraper scripts: data, not code.

A bank script is a JSON file (see ``banks/``). Every step names its intent in
plain words so a broken step can be understood, and repaired, without the page
itself. Scripts are data so that a repair can only ever change a locator or
add one of a few step types; it can never inject code.

Step actions:
  goto      {url}                       open a page (login phase only)
  fill      {locator, secret}           type a stored bank login (set-credential.sh)
  click     {locator}
  wait      {locator}                   wait until visible
  dismiss   {locator}                   click if it shows up within a few seconds
                                        (promo pop-ups); skipped otherwise
  expect    {locator? , url?}           assert we are where we think we are;
                                        anything else stops the run
  mfa       {detect, input, submit}     if ``detect`` appears, ask the human;
            [request, remember]         optional: a "send me a code" button,
                                        clicked only when someone can answer,
                                        and a "remember this device" box
  select    {locator, option | option_secret}
                                        choose a dropdown option by its visible
                                        label (``option``), or the one option
                                        whose label contains a stored value
                                        (``option_secret``: keeps account
                                        numbers out of the script)
  download  {locator, save_as}          click and save the download
  balance   {locator, save_as}          save the text of a balance shown on the
                                        page (for portals with no export); the
                                        loader turns it into a balance update
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTIONS = {"goto", "fill", "click", "wait", "dismiss", "expect", "mfa", "download", "balance", "select"}
LOCATOR_KINDS = ("role", "label", "text", "placeholder", "testid", "css")


class ScriptError(ValueError):
    pass


@dataclass(frozen=True)
class Locator:
    kind: str
    value: str
    name: str | None = None
    exact: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Locator":
        if not isinstance(data, dict):
            raise ScriptError(f"locator must be an object, got {data!r}")
        kinds = [k for k in LOCATOR_KINDS if k in data]
        if len(kinds) != 1:
            raise ScriptError(f"locator needs exactly one of {LOCATOR_KINDS}: {data!r}")
        extra = set(data) - {kinds[0], "name", "exact"}
        if extra:
            raise ScriptError(f"unknown locator keys {sorted(extra)}")
        if "name" in data and kinds[0] != "role":
            raise ScriptError("only role locators take a name")
        return cls(kinds[0], str(data[kinds[0]]), data.get("name"), bool(data.get("exact", False)))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {self.kind: self.value}
        if self.name is not None:
            d["name"] = self.name
        if self.exact:
            d["exact"] = True
        return d

    def describe(self) -> str:
        if self.kind == "role":
            return f'role={self.value} name="{self.name or ""}"'
        return f'{self.kind}="{self.value}"'

    def resolve(self, page):
        if self.kind == "role":
            return page.get_by_role(self.value, name=self.name, exact=self.exact)
        if self.kind == "label":
            return page.get_by_label(self.value, exact=self.exact)
        if self.kind == "text":
            return page.get_by_text(self.value, exact=self.exact)
        if self.kind == "placeholder":
            return page.get_by_placeholder(self.value, exact=self.exact)
        if self.kind == "testid":
            return page.get_by_test_id(self.value)
        return page.locator(self.value)


@dataclass(frozen=True)
class Step:
    action: str
    intent: str
    locator: Locator | None = None
    url: str | None = None
    secret: str | None = None
    save_as: str | None = None
    detect: Locator | None = None
    input: Locator | None = None
    submit: Locator | None = None
    request: Locator | None = None
    remember: Locator | None = None
    option: str | None = None
    option_secret: str | None = None
    timeout_ms: int | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Step":
        action = data.get("action")
        if action not in ACTIONS:
            raise ScriptError(f"unknown action {action!r}")
        intent = data.get("intent")
        if not intent or not isinstance(intent, str):
            raise ScriptError(f"{action} step needs an intent")
        if "value" in data:
            raise ScriptError("literal values are not allowed; use \"secret\": \"<bank>/<field>\"")

        def loc(key):
            return Locator.parse(data[key]) if key in data else None

        step = cls(
            action=action,
            intent=intent,
            locator=loc("locator"),
            url=data.get("url"),
            secret=data.get("secret"),
            save_as=data.get("save_as"),
            detect=loc("detect"),
            input=loc("input"),
            submit=loc("submit"),
            request=loc("request"),
            remember=loc("remember"),
            option=data.get("option"),
            option_secret=data.get("option_secret"),
            timeout_ms=data.get("timeout_ms"),
        )
        step._check()
        return step

    def _check(self) -> None:
        need = {
            "goto": ["url"],
            "fill": ["locator", "secret"],
            "click": ["locator"],
            "wait": ["locator"],
            "dismiss": ["locator"],
            "expect": [],
            "mfa": ["detect", "input", "submit"],
            "download": ["locator", "save_as"],
            "balance": ["locator", "save_as"],
            "select": ["locator"],
        }[self.action]
        for key in need:
            if getattr(self, key) is None:
                raise ScriptError(f"{self.action} step '{self.intent}' needs {key}")
        if self.action == "select" and (self.option is None) == (self.option_secret is None):
            raise ScriptError(f"select step '{self.intent}' needs exactly one of option or option_secret")
        if self.action == "expect" and self.locator is None and self.url is None:
            raise ScriptError(f"expect step '{self.intent}' needs a locator or url")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action, "intent": self.intent}
        for key in ("url", "secret", "save_as", "option", "option_secret", "timeout_ms"):
            if getattr(self, key) is not None:
                d[key] = getattr(self, key)
        for key in ("locator", "detect", "input", "submit", "request", "remember"):
            if getattr(self, key) is not None:
                d[key] = getattr(self, key).to_dict()
        return d


@dataclass(frozen=True)
class Allowlist:
    """Which requests a scraper session may make.

    navigation: regexes a top-level page load must match.
    hosts:      hosts that may serve GET subresources (scripts, images, XHR).
    post:       regexes a non-GET request must match (login form, export).
    block:      regexes that are always aborted, checked first, case-insensitive.
    """

    navigation: tuple[str, ...]
    hosts: tuple[str, ...]
    post: tuple[str, ...]
    block: tuple[str, ...]

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Allowlist":
        out = cls(
            navigation=tuple(data.get("navigation", [])),
            hosts=tuple(data.get("hosts", [])),
            post=tuple(data.get("post", [])),
            block=tuple(data.get("block", [])) + DEFAULT_BLOCK,
        )
        for pattern in out.navigation + out.post + out.block:
            re.compile(pattern)
        if not out.navigation:
            raise ScriptError("allowlist needs at least one navigation pattern")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "navigation": list(self.navigation),
            "hosts": list(self.hosts),
            "post": list(self.post),
            "block": [b for b in self.block if b not in DEFAULT_BLOCK],
        }

    def is_blocked(self, url: str) -> bool:
        return any(re.search(p, url, re.IGNORECASE) for p in self.block)

    def decide(self, url: str, method: str, is_navigation: bool) -> str:
        """Return 'allow' or the reason the request is aborted."""
        from urllib.parse import urlsplit

        if self.is_blocked(url):
            return "blocked pattern"
        if url.startswith(("data:", "blob:")):
            return "allow"
        if is_navigation:
            return "allow" if any(re.search(p, url) for p in self.navigation) else "navigation not allowlisted"
        if method != "GET":
            return "allow" if any(re.search(p, url) for p in self.post) else f"{method} not allowlisted"
        host = urlsplit(url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in self.hosts):
            return "allow"
        return "host not allowlisted"


def _word(stem: str) -> str:
    """Match ``stem`` as a whole word in a URL (letters on neither side), plural allowed."""
    return rf"(?<![a-z]){stem}s?(?![a-z])"


# Money-moving paths, English and French. Always blocked whatever a script says.
# Whole words so "border.css" or "etrade.com" stay reachable.
DEFAULT_BLOCK: tuple[str, ...] = tuple(
    _word(stem)
    for stem in (
        r"transfer",
        r"e-?transfer",
        r"interac",
        r"virements?",
        r"payee",
        r"b[eé]n[eé]ficiaire",
        r"pay-?bills?",
        r"bill-?pay",
        r"payments?",
        r"paiements?",
        r"send-?money",
        r"wires?",
        r"withdrawa?l?",
        r"retraits?",
        r"trade",
        r"trading",
        r"orders?",
    )
)


@dataclass(frozen=True)
class Script:
    institution: str
    allowlist: Allowlist
    login: tuple[Step, ...]
    steps: tuple[Step, ...]
    currency: str = "CAD"
    placeholder: bool = False  # true until the real flow is recorded; the scraper refuses to run it
    path: Path | None = field(default=None, compare=False)

    @classmethod
    def parse(cls, data: dict[str, Any], path: Path | None = None) -> "Script":
        login = tuple(Step.parse(s) for s in data.get("login", []))
        steps = tuple(Step.parse(s) for s in data.get("steps", []))
        if not login or login[0].action != "goto":
            raise ScriptError("the login phase must start with a goto step")
        if any(s.action == "goto" for s in steps):
            raise ScriptError("goto is only allowed in the login phase; navigate by clicking")
        if any(s.action == "fill" for s in steps):
            raise ScriptError("fill is only allowed in the login phase")
        if any(s.action in ("download", "balance", "select") for s in login):
            raise ScriptError("download and balance steps belong after login")
        script = cls(
            institution=data["institution"],
            allowlist=Allowlist.parse(data.get("allowlist", {})),
            login=login,
            steps=steps,
            currency=data.get("currency", "CAD"),
            placeholder=bool(data.get("placeholder", False)),
            path=path,
        )
        for step in script.login:
            if step.action == "goto" and not script.allowlist.decide(step.url, "GET", True) == "allow":
                raise ScriptError(f"goto url {step.url} is not on the allowlist")
        return script

    @classmethod
    def load(cls, path: Path) -> "Script":
        return cls.parse(json.loads(path.read_text()), path=path)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "institution": self.institution,
            "currency": self.currency,
            "allowlist": self.allowlist.to_dict(),
            "login": [s.to_dict() for s in self.login],
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.placeholder:
            d = {"placeholder": True, **d}
        return d

    def secrets(self) -> set[str]:
        return {s.secret for s in self.login if s.secret} | {s.option_secret for s in self.steps if s.option_secret}
