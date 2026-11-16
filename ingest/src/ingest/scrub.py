"""Scrub an accessibility snapshot down to what a repair needs, and refuse to
release it if anything sensitive survived.

Scrubbing keeps rather than removes: only interactive and heading roles, only
role and accessible name, digits as '#', words outside a small UI vocabulary as
'‹x›', and link URLs reduced to a path pattern. Field values are never kept.

Input is Playwright's ARIA snapshot format (``locator.aria_snapshot()``):

    - heading "Accounts" [level=1]
    - textbox "Card number": 4500 1234
    - link "Statements":
      - /url: /accounts/9876/statements
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from functools import lru_cache
from importlib import resources
from urllib.parse import urlsplit

KEEP_ROLES = frozenset(
    {"button", "link", "textbox", "combobox", "checkbox", "heading", "dialog", "menuitem", "tab"}
)
MASK = "‹x›"

_LINE = re.compile(r'^(?P<indent>\s*)- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?')
_URL = re.compile(r"^\s*- /url: (?P<url>.*)$")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?|#+|[^\w\s]", re.UNICODE)


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    text = resources.files("ingest").joinpath("vocab.txt").read_text(encoding="utf-8")
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#")
    )


def scrub_name(name: str) -> str:
    name = re.sub(r"\d+", "#", name.replace('\\"', '"'))
    vocab = vocabulary()
    out = []
    for token in _WORD.findall(name):
        if token.startswith("#") or not token[0].isalpha():
            out.append(token)
        elif token.lower().replace("’", "'") in vocab:
            out.append(token)
        else:
            out.append(MASK)
    # Collapse runs so the shape of a name, address or card number doesn't leak.
    text = " ".join(out)
    text = re.sub(rf"{MASK}(?:[\s\-'’.,]*{MASK})+", MASK, text)
    text = re.sub(r"#(?:[\s\-.,/*]*#)+", "#", text).strip()
    return re.sub(r"\s+([.,:;!?)])", r"\1", text)


def url_pattern(url: str) -> str:
    """Reduce a URL to its path shape: no host, query or fragment; ids become :id."""
    path = urlsplit(url.strip().strip('"')).path or "/"
    parts = []
    for seg in path.split("/"):
        if not seg:
            parts.append(seg)
        elif re.search(r"\d", seg) or len(seg) > 24:
            parts.append(":id")
        elif seg.lower() in vocabulary() or re.fullmatch(r"[a-z]{1,12}(?:[-_][a-z]{1,12}){0,3}(?:\.[a-z]{2,5})?", seg):
            parts.append(seg.lower())
        else:
            parts.append(":x")
    return "/".join(parts)


def scrub_snapshot(snapshot: str) -> str:
    lines = snapshot.splitlines()
    out: list[str] = []
    last_kept_indent: int | None = None
    for line in lines:
        url = _URL.match(line)
        if url and last_kept_indent is not None and out and out[-1].lstrip().startswith("- link"):
            out.append(" " * (last_kept_indent + 2) + f"- /url: {url_pattern(url.group('url'))}")
            continue
        m = _LINE.match(line)
        if not m or m.group("role") not in KEEP_ROLES:
            continue
        indent = len(m.group("indent"))
        role = m.group("role")
        name = m.group("name")
        kept = f"{' ' * indent}- {role}"
        if name:
            kept += f' "{scrub_name(name)}"'
        out.append(kept)
        last_kept_indent = indent
    return "\n".join(out)


class SensitiveValueFound(RuntimeError):
    pass


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9#]", "", text)


def amount_forms(amount: Decimal) -> list[str]:
    a = abs(amount).quantize(Decimal("0.01"))
    whole, cents = f"{a:f}".split(".")
    grouped = f"{int(whole):,}"
    return [f"{whole}.{cents}", f"{grouped}.{cents}", f"{grouped.replace(',', ' ')},{cents}"]


def sensitive_forms(values: list[str], amounts: list[Decimal]) -> set[str]:
    """Normalized needles. Short values would match by accident and are skipped:
    words under 3 letters, numbers under 4 digits."""
    forms: set[str] = set()
    for v in values:
        for piece in [v, *re.split(r"[\s,@]+", v)]:
            n = _norm(piece)
            digits = re.sub(r"\D", "", n)
            if digits and len(digits) >= 4:
                forms.add(n)
                forms.add(digits)
                forms.add(digits[-4:])  # masked card/account displays show the last four
            elif len(n) >= 3 and not digits:
                forms.add(n)
    for amt in amounts:
        for form in amount_forms(amt):
            n = _norm(form)
            if len(re.sub(r"\D", "", n)) >= 4:
                forms.add(n)
    return forms


def blocking_check(text: str, values: list[str], amounts: list[Decimal]) -> None:
    """Raise if any sensitive value appears in ``text``. Words are matched whole,
    numbers anywhere in the digit stream (before digits were masked, this is the
    backstop for a scrubber bug)."""
    hay_words = {_norm(w) for w in re.split(r"[^\w#@.'-]+", text) if w}
    hay_flat = _norm(text)
    hay_digits = re.sub(r"\D", "", text)
    hits = []
    for form in sensitive_forms(values, amounts):
        if form.isdigit():
            if form in hay_digits:
                hits.append(form)
        elif re.search(r"\d", form):
            if form in hay_flat:
                hits.append(form)
        elif form in hay_words or (len(form) >= 6 and form in hay_flat):
            hits.append(form)
    if hits:
        # Don't echo the value itself; say how many and what kind.
        kinds = sorted({"number" if h.isdigit() else "text" for h in hits})
        raise SensitiveValueFound(f"{len(hits)} sensitive value(s) found ({', '.join(kinds)}); not sending")
