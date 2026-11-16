"""Simplii (CIBC platform) account CSV.

Unverified: believed headerless, ``date, description, withdrawal, deposit``,
sometimes followed by a card/account column. A header row, if present, is
skipped. Confirm against a real export and tighten.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

from ..canonical import build_records
from . import DATE_FORMATS, ParseError, Parsed, account_ref_from, check_dates, parse_money, pick_date_format, to_iso


def parse(path: Path, *, currency: str = "CAD", date_formats: tuple[str, ...] | None = None, **_) -> Parsed:
    text = path.read_text(encoding="utf-8-sig", errors="strict")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if rows and not any(ch.isdigit() for ch in rows[0][0]):
        rows = rows[1:]  # header
    if not rows:
        return Parsed()
    for i, r in enumerate(rows):
        if len(r) < 4:
            raise ParseError(f"{path.name} row {i + 1}: expected at least 4 columns, got {len(r)}")
    fmt = pick_date_format([r[0] for r in rows], tuple(date_formats) if date_formats else DATE_FORMATS)
    out: list[tuple[str, Decimal, str]] = []
    for i, r in enumerate(rows):
        withdrawal, deposit = parse_money(r[2]), parse_money(r[3])
        if (withdrawal is None) == (deposit is None):
            raise ParseError(f"{path.name} row {i + 1}: need exactly one of withdrawal/deposit")
        amount = -abs(withdrawal) if withdrawal is not None else abs(deposit)
        out.append((to_iso(r[0], fmt), amount, r[1]))
    records = build_records(out, account_ref=account_ref_from(path), currency=currency, source="simplii", raw_file=str(path))
    check_dates(records)
    return Parsed(records=records)
