"""The canonical transaction record every parser produces.

Sign convention: ``amount`` is from the account holder's point of view.
Positive means money into the account (deposit, refund, card payment received),
negative means money out (purchase, withdrawal, fee).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Record:
    account_ref: str
    date: str  # ISO 8601, YYYY-MM-DD
    amount: Decimal
    currency: str
    description: str
    source: str
    external_id: str
    raw_file: str

    def to_json(self) -> str:
        data = asdict(self)
        data["amount"] = format_amount(self.amount)
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "Record":
        data = json.loads(line)
        data["amount"] = Decimal(data["amount"])
        return cls(**data)


def format_amount(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01')):f}"


def normalize_description(description: str) -> str:
    return " ".join(description.split())


def external_id(source: str, account: str, date: str, amount: Decimal, description: str, n: int) -> str:
    key = "|".join([source, account, date, format_amount(amount), normalize_description(description), str(n)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def build_records(
    rows: list[tuple[str, Decimal, str]],
    *,
    account_ref: str,
    currency: str,
    source: str,
    raw_file: str,
) -> list[Record]:
    """Turn (date, signed amount, description) rows from one file into records.

    ``n`` counts earlier identical rows in the same file, so two identical
    same-day purchases get distinct ids while a later file that covers the
    same days reproduces the same ids.
    """
    seen: Counter[tuple[str, str, str]] = Counter()
    records = []
    for date, amount, description in rows:
        description = normalize_description(description)
        key = (date, format_amount(amount), description)
        n = seen[key]
        seen[key] += 1
        records.append(
            Record(
                account_ref=account_ref,
                date=date,
                amount=amount,
                currency=currency,
                description=description,
                source=source,
                external_id=external_id(source, account_ref, date, amount, description, n),
                raw_file=raw_file,
            )
        )
    return records
