"""A balance read off a portal page by a ``balance`` step: JSON with the
captured text and the day it was read. Becomes one balance update."""

from __future__ import annotations

import json
from pathlib import Path

from . import BalanceUpdate, ParseError, Parsed, account_ref_from, parse_money


def parse(path: Path, **_) -> Parsed:
    data = json.loads(path.read_text())
    if data.get("kind") != "balance":
        raise ParseError(f"{path.name}: not a captured balance")
    amount = parse_money(data.get("text", ""))
    if amount is None:
        raise ParseError(f"{path.name}: no amount in the captured text")
    return Parsed(balance=BalanceUpdate(account_ref=account_ref_from(path), date=data["date"], amount=amount, raw_file=str(path)))
