"""Minimal Sure API client (``X-Api-Key``, ``/api/v1``)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

from .canonical import Record, format_amount


class SureError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Sure API {status}: {body[:300]}")
        self.status = status


@dataclass
class PostResult:
    created: bool  # True: 201, new; False: 200, already there (idempotent replay)
    id: str


class SureClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("X-Api-Key", self._key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise SureError(exc.code, exc.read().decode(errors="replace")) from None

    def accounts(self) -> list[dict]:
        out, page = [], 1
        while True:
            _, data = self._request("GET", f"/api/v1/accounts?page={page}&per_page=100")
            out += data.get("accounts", [])
            if page >= int(data.get("pagination", {}).get("total_pages", 1)):
                return out
            page += 1

    def post_transaction(self, record: Record, account_id: str) -> PostResult:
        nature = "income" if record.amount > 0 else "expense"
        body = {
            "transaction": {
                "account_id": account_id,
                "date": record.date,
                "amount": format_amount(abs(record.amount)),
                "nature": nature,
                "name": record.description,
                "currency": record.currency,
                "source": f"ingest:{record.source}",
                "external_id": record.external_id,
            }
        }
        status, data = self._request("POST", "/api/v1/transactions", body)
        if status not in (200, 201):
            raise SureError(status, json.dumps(data))
        return PostResult(created=status == 201, id=str(data.get("id", "")))

    def post_valuation(self, account_id: str, date: str, amount: Decimal, notes: str) -> PostResult:
        body = {
            "valuation": {"account_id": account_id, "date": date, "amount": format_amount(amount), "notes": notes},
            "upsert": True,
        }
        status, data = self._request("POST", "/api/v1/valuations", body)
        return PostResult(created=status == 201, id=str(data.get("id", "")))
