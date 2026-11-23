"""Sun Life quarterly statement PDF -> one closing balance.

Unverified: the wording is a guess ("Closing balance", "Ending balance",
"Total market value", or French equivalents) next to a period end date.
Exactly one balance must match, or the file is flagged for review.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from . import BalanceUpdate, ParseError, Parsed, account_ref_from, parse_money

BALANCE = re.compile(
    r"(?:closing|ending|end of period)\s+balance|total\s+(?:market\s+)?value|solde\s+de\s+(?:clôture|fermeture|fin)|valeur\s+totale",
    re.IGNORECASE,
)
AMOUNT = re.compile(r"\$?\s?\(?-?\d{1,3}(?:[ , ]\d{3})*(?:[.,]\d{2})\)?\s?\$?")
PERIOD_END = re.compile(
    r"(?:to|through|as of|ending|au|en date du)\s+([A-Z][a-zé]+\.? \d{1,2},? \d{4}|\d{1,2} [a-zé]+\.? \d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
MONTHS_FR = {
    "janvier": "January", "février": "February", "fevrier": "February", "mars": "March", "avril": "April",
    "mai": "May", "juin": "June", "juillet": "July", "août": "August", "aout": "August",
    "septembre": "September", "octobre": "October", "novembre": "November", "décembre": "December", "decembre": "December",
}


def _date(text: str) -> str:
    t = text.replace(".", "").replace(",", "")
    for fr, en in MONTHS_FR.items():
        t = re.sub(rf"\b{fr}\b", en, t, flags=re.IGNORECASE)
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    raise ParseError(f"unrecognised statement date {text!r}")


def extract_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def parse_text(text: str, *, account_ref: str, raw_file: str) -> Parsed:
    balances = set()
    for line in text.splitlines():
        if BALANCE.search(line):
            after = line[BALANCE.search(line).end():]
            m = AMOUNT.search(after)
            if m:
                balances.add(parse_money(m.group()))
    if len(balances) != 1:
        raise ParseError(f"expected exactly one closing balance, found {len(balances)}")
    ends = {_date(m.group(1)) for m in PERIOD_END.finditer(text)}
    if not ends:
        raise ParseError("no statement period end date found")
    return Parsed(balance=BalanceUpdate(account_ref=account_ref, date=max(ends), amount=balances.pop(), raw_file=raw_file))


def parse(path: Path, **_) -> Parsed:
    return parse_text(extract_text(path), account_ref=account_ref_from(path), raw_file=str(path))
