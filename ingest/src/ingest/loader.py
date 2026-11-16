"""Parse new raw files, check them, write canonical JSONL, load into Sure.

A ledger (``normalized/.ledger.json``) remembers which raw files have loaded so
nightly runs don't replay history. Overlap between files is expected (each
scrape pulls the last ~30 days) and handled by ``source`` + ``external_id``.

A file whose opening balance + transactions != closing balance is flagged in
``review/`` and not loaded.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .parsers import ParseError, Parsed
from .sure import SureClient

log = logging.getLogger("ingest.loader")

PARSERS = {
    "simplii": "ingest.parsers.simplii",
    "mapped_csv": "ingest.parsers.mapped_csv",
    "sunlife_pdf": "ingest.parsers.sunlife",
}


@dataclass
class LoadStats:
    files: int = 0
    created: int = 0
    existing: int = 0
    balances: int = 0
    review: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Loader:
    def __init__(self, config: dict, raw_root: Path, normalized_root: Path, review_root: Path, client: SureClient | None):
        self.config = config
        self.raw_root = raw_root
        self.normalized_root = normalized_root
        self.review_root = review_root
        self.client = client
        self.ledger_path = normalized_root / ".ledger.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}
        self._account_ids: dict[str, str] | None = None

    # -- helpers -------------------------------------------------------------

    def _save_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ledger, indent=2, sort_keys=True))
        os.replace(tmp, self.ledger_path)

    def account_id(self, institution: str, account_ref: str) -> str:
        key = f"{institution}:{account_ref}"
        name = self.config.get("accounts", {}).get(key)
        if not name:
            raise ParseError(f"no Sure account mapped for {key} in loader config 'accounts'")
        if self._account_ids is None:
            self._account_ids = {a["name"]: a["id"] for a in self.client.accounts()}
        if name not in self._account_ids:
            raise ParseError(f"Sure has no account named {name!r} (mapped from {key})")
        return self._account_ids[name]

    def parse(self, institution: str, path: Path) -> Parsed:
        fmt = dict(self.config.get("formats", {}).get(institution) or {})
        if not fmt:
            raise ParseError(f"no format configured for {institution}")
        module = importlib.import_module(PARSERS[fmt.pop("parser")])
        return module.parse(path, institution=institution, **fmt)

    @staticmethod
    def balance_problem(parsed: Parsed) -> str | None:
        if parsed.opening is None or parsed.closing is None:
            return None
        total = parsed.opening + sum((r.amount for r in parsed.records), Decimal(0))
        if total != parsed.closing:
            return f"opening {parsed.opening} + transactions {total - parsed.opening} = {total}, statement says {parsed.closing}"
        return None

    def _flag(self, rel: str, reason: str, stats: LoadStats) -> None:
        self.review_root.mkdir(parents=True, exist_ok=True)
        note = self.review_root / (rel.replace("/", "_") + ".json")
        note.write_text(json.dumps({"raw_file": rel, "reason": reason, "flagged_at": dt.datetime.now().isoformat(timespec="seconds")}, indent=2))
        self.ledger[rel] = {"status": "review", "reason": reason}
        stats.review.append(rel)
        log.warning("review: %s: %s", rel, reason)

    # -- main ----------------------------------------------------------------

    def pending_files(self) -> list[tuple[str, Path]]:
        out = []
        for path in sorted(self.raw_root.glob("*/*/*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = str(path.relative_to(self.raw_root))
            if self.ledger.get(rel, {}).get("status") in ("loaded", "review"):
                continue
            out.append((path.parts[-3], path))
        return out

    def run(self, dry_run: bool = False) -> LoadStats:
        stats = LoadStats()
        for institution, path in self.pending_files():
            rel = str(path.relative_to(self.raw_root))
            stats.files += 1
            try:
                parsed = self.parse(institution, path)
            except (ParseError, ValueError, KeyError) as exc:
                self._flag(rel, f"parse failed: {exc}", stats)
                continue
            problem = self.balance_problem(parsed)
            if problem:
                self._flag(rel, problem, stats)
                continue

            out = self.normalized_root / institution / (path.stem + ".jsonl")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("".join(r.to_json() + "\n" for r in parsed.records))
            if dry_run:
                log.info("dry run: %s -> %d records", rel, len(parsed.records))
                continue

            try:
                created = existing = 0
                for record in parsed.records:
                    res = self.client.post_transaction(record, self.account_id(institution, record.account_ref))
                    created += res.created
                    existing += not res.created
                if parsed.balance:
                    b = parsed.balance
                    self.client.post_valuation(self.account_id(institution, b.account_ref), b.date, b.amount, f"{institution} statement")
                    stats.balances += 1
            except Exception as exc:  # noqa: BLE001 - leave the file pending; next run retries idempotently
                stats.errors.append(f"{rel}: {exc}")
                log.error("load failed for %s: %s", rel, exc)
                continue
            stats.created += created
            stats.existing += existing
            self.ledger[rel] = {
                "status": "loaded",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "created": created,
                "existing": existing,
                "loaded_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            log.info("%s: %d new, %d already present", rel, created, existing)
        if not dry_run:
            self._save_ledger()
        return stats
