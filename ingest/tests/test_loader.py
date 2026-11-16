"""Loader against a fake Sure API that mimics the real idempotency contract
(201 on create, 200 on a repeated source + external_id for the same account)."""

import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ingest.loader import Loader
from ingest.sure import SureClient

KEY = "test-key"
ACCOUNTS = [{"id": "11111111-1111-1111-1111-111111111111", "name": "Simplii Chequing"},
            {"id": "22222222-2222-2222-2222-222222222222", "name": "Sun Life RRSP"}]


class FakeSure:
    def __init__(self):
        self.entries = {}
        self.valuations = {}
        self.requests = []
        sure = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("X-Api-Key") != KEY:
                    return self._json(401, {"error": "unauthorized"})
                self._json(200, {"accounts": ACCOUNTS, "pagination": {"page": 1, "per_page": 100, "total_count": 2, "total_pages": 1}})

            def do_POST(self):
                if self.headers.get("X-Api-Key") != KEY:
                    return self._json(401, {"error": "unauthorized"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sure.requests.append((self.path, body))
                if self.path == "/api/v1/transactions":
                    t = body["transaction"]
                    if t.get("source") and not t.get("external_id"):
                        return self._json(422, {"message": "Source requires external_id"})
                    key = (t["account_id"], t["source"], t["external_id"])
                    if key in sure.entries:
                        return self._json(200, sure.entries[key])
                    amt = Decimal(t["amount"])
                    signed = -abs(amt) if t["nature"] in ("income", "inflow") else abs(amt)
                    sure.entries[key] = {"id": f"e{len(sure.entries)}", "signed": str(signed)}
                    return self._json(201, sure.entries[key])
                if self.path == "/api/v1/valuations":
                    v = body["valuation"]
                    existed = (v["account_id"], v["date"]) in sure.valuations
                    sure.valuations[(v["account_id"], v["date"])] = v["amount"]
                    return self._json(200 if existed and body.get("upsert") else 201, {"id": "v"})
                self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sure():
    s = FakeSure()
    yield s
    s.close()


CONFIG = {
    "accounts": {"simplii:chequing": "Simplii Chequing", "sunlife:rrsp": "Sun Life RRSP", "amex:gold": "Amex Gold"},
    "formats": {
        "simplii": {"parser": "simplii"},
        "sunlife": {"parser": "sunlife_pdf"},
        "amex": {"parser": "mapped_csv", "date": "Date", "description": "Description", "amount": "Amount", "sign": -1, "balance": "Balance"},
    },
}

DAY1 = "2026-11-02,TIM HORTONS #1234,4.25,\n2026-11-02,TIM HORTONS #1234,4.25,\n2026-11-03,PAYROLL DEPOSIT,,2500.00\n"
DAY2 = DAY1 + "2026-11-04,GROCER,61.10,\n"


def setup(tmp_path, files):
    raw = tmp_path / "raw"
    for rel, text in files.items():
        p = raw / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return raw


def loader(tmp_path, raw, sure):
    return Loader(CONFIG, raw, tmp_path / "normalized", tmp_path / "review", SureClient(sure.url, KEY))


def test_first_load_creates_then_rerun_and_overlap_are_idempotent(tmp_path, sure):
    raw = setup(tmp_path, {"simplii/2026/chequing__2026-11-03.csv": DAY1})
    s1 = loader(tmp_path, raw, sure).run()
    assert (s1.created, s1.existing) == (3, 0)

    # Same file again (ledger cleared): every POST answers 200, nothing new.
    (tmp_path / "normalized" / ".ledger.json").unlink()
    s2 = loader(tmp_path, raw, sure).run()
    assert (s2.created, s2.existing) == (0, 3)

    # Next night's overlapping file: only the new row is created.
    (raw / "simplii/2026/chequing__2026-11-04.csv").write_text(DAY2)
    s3 = loader(tmp_path, raw, sure).run()
    assert (s3.files, s3.created, s3.existing) == (1, 1, 3)
    assert len(sure.entries) == 4

    # Ledger: nothing pending now.
    assert loader(tmp_path, raw, sure).run().files == 0


def test_signs_and_payload(tmp_path, sure):
    raw = setup(tmp_path, {"simplii/2026/chequing__x.csv": DAY1})
    loader(tmp_path, raw, sure).run()
    bodies = [b["transaction"] for p, b in sure.requests if p == "/api/v1/transactions"]
    assert bodies[0]["nature"] == "expense" and bodies[0]["amount"] == "4.25"
    assert bodies[2]["nature"] == "income" and bodies[2]["amount"] == "2500.00"
    assert all(b["source"] == "ingest:simplii" and len(b["external_id"]) == 64 for b in bodies)
    assert sorted(e["signed"] for e in sure.entries.values()) == ["-2500.00", "4.25", "4.25"]
    lines = (tmp_path / "normalized/simplii/chequing__x.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["amount"] == "-4.25"


def test_balance_mismatch_is_flagged_and_not_loaded(tmp_path, sure):
    bad = "Date,Description,Amount,Balance\n11/02/2026,GROCER,75.50,600.00\n11/03/2026,COFFEE,4.00,999.00\n"
    raw = setup(tmp_path, {"amex/2026/gold__x.csv": bad})
    stats = loader(tmp_path, raw, sure).run()
    assert stats.review == ["amex/2026/gold__x.csv"] and sure.requests == []
    note = json.loads((tmp_path / "review/amex_2026_gold__x.csv.json").read_text())
    assert "statement says" in note["reason"]
    # Flagged files are not retried every night.
    assert loader(tmp_path, raw, sure).run().files == 0


def test_balance_match_loads(tmp_path, sure):
    # Card style: positive = charge. Balance owed 524.50 -> 600.00 -> 604.00 means
    # from the holder's view the balance goes -524.50 -> -600.00 -> -604.00.
    good = "Date,Description,Amount,Balance\n11/02/2026,GROCER,75.50,-600.00\n11/03/2026,COFFEE,4.00,-604.00\n"
    raw = setup(tmp_path, {"amex/2026/gold__x.csv": good})
    sure_ids = {"Amex Gold": "33333333-3333-3333-3333-333333333333"}
    ACCOUNTS.append({"id": sure_ids["Amex Gold"], "name": "Amex Gold"})
    try:
        stats = loader(tmp_path, raw, sure).run()
    finally:
        ACCOUNTS.pop()
    assert stats.review == [] and stats.created == 2


def test_unmapped_account_leaves_file_pending(tmp_path, sure):
    raw = setup(tmp_path, {"simplii/2026/savings__x.csv": DAY1})
    stats = loader(tmp_path, raw, sure).run()
    assert stats.errors and stats.created == 0
    assert loader(tmp_path, raw, sure).pending_files()  # retried next run


def test_sunlife_pdf_posts_valuation_upsert(tmp_path, sure):
    from .test_parsers import make_pdf

    raw = tmp_path / "raw"
    (raw / "sunlife/2026").mkdir(parents=True)
    make_pdf(raw / "sunlife/2026/rrsp__q3.pdf", ["Statement as of September 30, 2026", "Closing balance $41,234.56"])
    stats = loader(tmp_path, raw, sure).run()
    assert stats.balances == 1
    [(path, body)] = sure.requests
    assert path == "/api/v1/valuations" and body["upsert"] is True
    assert body["valuation"] == {"account_id": ACCOUNTS[1]["id"], "date": "2026-09-30", "amount": "41234.56", "notes": "sunlife statement"}


def test_dry_run_writes_jsonl_without_api(tmp_path):
    raw = setup(tmp_path, {"simplii/2026/chequing__x.csv": DAY1})
    stats = Loader(CONFIG, raw, tmp_path / "normalized", tmp_path / "review", None).run(dry_run=True)
    assert stats.files == 1 and (tmp_path / "normalized/simplii/chequing__x.jsonl").exists()
    assert not (tmp_path / "normalized/.ledger.json").exists()
