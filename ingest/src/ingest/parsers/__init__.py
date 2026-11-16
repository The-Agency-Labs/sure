"""Parsers turn one raw file into canonical records (and, optionally, the
statement's opening/closing balances or a point-in-time balance)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..canonical import Record


class ParseError(ValueError):
    pass


@dataclass
class BalanceUpdate:
    account_ref: str
    date: str
    amount: Decimal
    raw_file: str


@dataclass
class Parsed:
    records: list[Record] = field(default_factory=list)
    opening: Decimal | None = None
    closing: Decimal | None = None
    balance: BalanceUpdate | None = None


def account_ref_from(path: Path) -> str:
    """Raw files are named ``<account_ref>__<anything>.<ext>`` by the scraper."""
    ref, sep, _ = path.name.partition("__")
    if not sep or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", ref):
        raise ParseError(f"{path.name}: file name must start with '<account_ref>__'")
    return ref


def parse_money(text: str) -> Decimal | None:
    """'1,234.56', '$1 234,56', '(12.00)', '-12.00', '12.00 CR' -> Decimal; '' -> None."""
    t = text.strip().replace(" ", " ").replace("$", "").replace("CAD", "").replace("USD", "").strip()
    if not t:
        return None
    negative = (t.startswith("(") and t.endswith(")")) or t.startswith("-") or t.endswith("-")
    t = t.strip("()-+ ")
    credit = t.upper().endswith("CR")
    t = re.sub(r"\s*CR$", "", t, flags=re.IGNORECASE)
    if re.fullmatch(r"\d{1,3}(?:[ .]\d{3})*,\d{2}", t):  # French: 1 234,56
        t = t.replace(" ", "").replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "").replace(" ", "")
    try:
        value = Decimal(t)
    except InvalidOperation:
        raise ParseError(f"not an amount: {text!r}") from None
    if credit:
        return value
    return -value if negative else value


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y", "%m/%d/%y")


def pick_date_format(values: list[str], preferred: tuple[str, ...] = DATE_FORMATS) -> str:
    """The first format that parses every value. Ambiguous files (all days <= 12)
    resolve to whichever of m/d and d/m comes first in ``preferred``."""
    for fmt in preferred:
        try:
            for v in values:
                datetime.strptime(v.strip(), fmt)
        except ValueError:
            continue
        return fmt
    raise ParseError(f"no date format fits {values[:3]!r}")


def to_iso(value: str, fmt: str) -> str:
    return datetime.strptime(value.strip(), fmt).date().isoformat()


def check_dates(records: list[Record], today: date | None = None) -> None:
    today = today or date.today()
    for r in records:
        d = date.fromisoformat(r.date)
        if d > today or d.year < 2000:
            raise ParseError(f"implausible date {r.date}: wrong date format?")
