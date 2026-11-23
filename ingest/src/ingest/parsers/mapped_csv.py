"""A CSV with a header row, mapped by column names in the loader config.

Config keys (per institution):
  date, description           column names
  amount                      one signed column, or
  debit, credit               two unsigned columns
  sign                        +1 if positive amounts are money in (bank style),
                              -1 if positive amounts are charges (card style)
  date_formats                optional list, tried in order
  balance                     optional: running-balance column; gives the
                              opening and closing balance for the check
  skip_rows                   optional: lines before the header
  source                      defaults to the institution name

Presets for formats not yet confirmed with a real file live in loader.example.json.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

from ..canonical import build_records
from . import DATE_FORMATS, ParseError, Parsed, account_ref_from, check_dates, parse_money, pick_date_format, to_iso


def parse(path: Path, *, institution: str, currency: str = "CAD", **cfg) -> Parsed:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()[int(cfg.get("skip_rows", 0)):]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    if not rows:
        return Parsed()
    headers = set(reader.fieldnames or [])
    needed = [cfg["date"], cfg["description"]] + ([cfg["amount"]] if "amount" in cfg else [cfg["debit"], cfg["credit"]])
    missing = [h for h in needed if h not in headers]
    if missing:
        raise ParseError(f"{path.name}: missing columns {missing}; have {sorted(headers)}")
    sign = Decimal(int(cfg.get("sign", 1)))
    fmt = pick_date_format([r[cfg["date"]] for r in rows], tuple(cfg.get("date_formats", DATE_FORMATS)))
    out: list[tuple[str, Decimal, str]] = []
    for i, r in enumerate(rows):
        if "amount" in cfg:
            amount = parse_money(r[cfg["amount"]])
            if amount is None:
                raise ParseError(f"{path.name} row {i + 1}: empty amount")
            amount *= sign
        else:
            debit, credit = parse_money(r[cfg["debit"]] or ""), parse_money(r[cfg["credit"]] or "")
            if (debit is None) == (credit is None):
                raise ParseError(f"{path.name} row {i + 1}: need exactly one of debit/credit")
            amount = -abs(debit) if debit is not None else abs(credit)
        out.append((to_iso(r[cfg["date"]], fmt), amount, r[cfg["description"]]))
    records = build_records(
        out, account_ref=account_ref_from(path), currency=currency, source=cfg.get("source", institution), raw_file=str(path)
    )
    check_dates(records)
    parsed = Parsed(records=records)
    if "balance" in cfg:
        balances = [parse_money(r[cfg["balance"]]) for r in rows]
        amounts = [rec.amount for rec in records]
        if records[0].date > records[-1].date:  # newest first
            balances.reverse()
            amounts.reverse()
        if all(b is not None for b in balances):
            parsed.opening = balances[0] - amounts[0]
            parsed.closing = balances[-1]
    return parsed
