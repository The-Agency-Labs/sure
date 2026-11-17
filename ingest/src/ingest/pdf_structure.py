"""Show a PDF statement's layout with every value masked, so a parser can be
written for it without sharing the statement.

  - every run of digits becomes 9 (so 1,234.56 -> 9,9.9 and 2026-10-03 -> 9-9-9):
    separators, signs, $ and CR/DR stay, sizes and values don't
  - words outside src/ingest/vocab.txt and month names become ‹x›
  - runs of spaces are kept as two spaces, so column breaks still show

Read the output yourself before sharing it. --check {"values": [...]} refuses to
print if any listed value remains.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .scrub import MASK, SensitiveValueFound, blocking_check, vocabulary

MONTHS = frozenset(
    "jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august "
    "september october november december janv févr fevr avr mai juin juil août aout déc".split()
)
# Markers a parser needs to see: credit/debit and currency codes.
MARKERS = frozenset("cr dr db ca usd cad".split())
_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?|\d+|\s+|.", re.UNICODE)


def mask_line(line: str, keep: frozenset[str] = frozenset()) -> str:
    vocab = vocabulary() | MONTHS | MARKERS | keep
    out = []
    for tok in _TOKEN.findall(line):
        if tok.isdigit():
            out.append("9")
        elif tok.isspace():
            out.append("  " if len(tok) > 1 else " ")
        elif tok[0].isalpha():
            out.append(tok if tok.lower().replace("’", "'") in vocab else MASK)
        else:
            out.append(tok)
    text = "".join(out)
    return re.sub(rf"{MASK}(?:[ \-'’.,&/]*{MASK})+", MASK, text).rstrip()


def structure(pdf: Path, keep: frozenset[str] = frozenset()) -> str:
    from pypdf import PdfReader

    out = []
    for n, page in enumerate(PdfReader(str(pdf)).pages, 1):
        out.append(f"--- page {n} ---")
        out += [mask_line(line, keep) for line in (page.extract_text() or "").splitlines() if line.strip()]
    return "\n".join(out)


def main_structure(pdf: Path, keep: list[str], check: Path | None) -> int:
    text = structure(pdf, frozenset(w.lower() for w in keep))
    if check:
        try:
            blocking_check(text, json.loads(check.read_text()).get("values", []), [])
        except SensitiveValueFound as exc:
            print(f"stopped: a checked value is still in the output ({exc}); nothing printed")
            return 1
    print(text)
    return 0
