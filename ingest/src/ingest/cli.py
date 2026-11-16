"""ingest scrape <bank> | load | repair <bundle> | repair --queue | check <script.json>"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path

from .canonical import Record

DEFAULT_CONFIG = "/etc/ingest/ingest.json"
log = logging.getLogger("ingest")


def load_config(path: str | None) -> dict:
    path = path or os.environ.get("INGEST_CONFIG", DEFAULT_CONFIG)
    return json.loads(Path(path).read_text())


def paths(cfg: dict) -> dict[str, Path]:
    p = cfg.get("paths", {})
    return {
        "raw": Path(p.get("raw", "/srv/finance/raw")),
        "normalized": Path(p.get("normalized", "/srv/finance/normalized")),
        "bundles": Path(p.get("bundles", "/srv/ingest-state/bundles")),
        "review": Path(p.get("review", "/srv/ingest-state/review")),
        "queue": Path(p.get("repair_queue", "/srv/ingest-state/repair-queue")),
        "secrets": Path(p.get("secrets", "/srv/ingest-secrets")),
        "banks": Path(p.get("banks", "/opt/ingest/ingest/banks")),
    }


def read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text().strip() or None
    except FileNotFoundError:
        return None


def notifier(cfg: dict):
    """Push target, from "notify": {"type": "webhook" | "ntfy" | "none", ...}."""
    from .notify import NtfyNotifier, NullNotifier, WebhookNotifier

    n = cfg.get("notify") or {}
    kind = n.get("type", "none")
    if kind == "webhook":
        return WebhookNotifier(n["url"], read_secret_file(n.get("token_file")))
    if kind == "ntfy":
        return NtfyNotifier(n["url"], n["topic"], read_secret_file(n.get("token_file")))
    log.warning("no push target configured: failures are only in the logs")
    return NullNotifier()


def cmd_scrape(args, cfg) -> int:
    from .notify import MfaServer, NoWaitCodes, TerminalCodes
    from .runner import RunConfig, Runner
    from .secrets import BitwardenStore
    from .steps import Script

    p = paths(cfg)
    script = Script.load(p["banks"] / f"{args.bank}.json")
    if script.placeholder:
        log.error("%s.json is still a placeholder: record the real flow first (see README)", args.bank)
        return 2
    browser = cfg.get("browser", {})
    mfa = cfg.get("mfa", {})
    run_cfg = RunConfig(
        raw_root=p["raw"],
        profile_dir=p["secrets"] / "profiles" / args.bank,
        bundle_root=p["bundles"],
        headless=browser.get("headless", False),
        channel=browser.get("channel", "chrome"),
        chromium_sandbox=browser.get("chromium_sandbox", True),
        locale=os.environ.get("INGEST_LOCALE") or browser.get("locale"),
        timezone_id=os.environ.get("INGEST_TIMEZONE") or browser.get("timezone"),
        mfa_timeout_s=mfa.get("timeout_s", 300),
    )
    note = notifier(cfg)
    # By hand: type the code in the terminal. Unattended: "stop" (default) ends the
    # run with a push; "form" waits on a one-time page on the LAN/tailnet.
    if args.interactive:
        codes = TerminalCodes()
    elif mfa.get("unattended", "stop") == "form":
        codes = MfaServer(mfa.get("bind", "0.0.0.0"), mfa.get("port", 8765), mfa["public_base"], note)
    else:
        codes = NoWaitCodes(note, script.institution)
    store = BitwardenStore(p["secrets"] / "bw")
    try:
        result = Runner(script, run_cfg, store, codes, note).run()
    finally:
        store.close()
    for pattern, reason in result.aborted_requests:
        log.info("aborted request: %s (%s)", pattern, reason)
    if result.ok:
        log.info("%s: %d file(s) downloaded", args.bank, len(result.downloads))
        return 0
    log.error("%s failed: %s", args.bank, result.error)
    return 1


def cmd_load(args, cfg) -> int:
    from .loader import Loader
    from .sure import SureClient

    p = paths(cfg)
    client = None
    if not args.dry_run:
        key = read_secret_file(cfg["sure"]["api_key_file"])
        if not key:
            log.error("no Sure API key at %s", cfg["sure"]["api_key_file"])
            return 2
        client = SureClient(cfg["sure"]["url"], key)
    stats = Loader(cfg, p["raw"], p["normalized"], p["review"], client).run(dry_run=args.dry_run)
    log.info(
        "files %d, new %d, already present %d, balances %d, review %d, errors %d",
        stats.files, stats.created, stats.existing, stats.balances, len(stats.review), len(stats.errors),
    )
    if stats.review or stats.errors:
        notifier(cfg).notify("Finance sync: some files need review.")
        return 1
    return 0


def recent_amounts(normalized: Path, days: int = 120) -> list[Decimal]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = set()
    for f in normalized.glob("*/*.jsonl"):
        for line in f.read_text().splitlines():
            r = Record.from_json(line)
            if r.date >= cutoff:
                out.add(abs(r.amount))
    return sorted(out)


def sensitive_values(cfg: dict, p: dict[str, Path]) -> list[str]:
    from .secrets import BitwardenStore

    values: list[str] = []
    extra = Path(cfg.get("repair", {}).get("sensitive_file", p["secrets"] / "sensitive.json"))
    if extra.exists():
        values += json.loads(extra.read_text()).get("values", [])
    else:
        log.warning("no %s: personal details (name, address, phone, email) are not being checked", extra)
    store = BitwardenStore(p["secrets"] / "bw")
    try:
        values += store.all_values()
    finally:
        store.close()
    return values


def cmd_repair(args, cfg) -> int:
    from .repair import ModelEndpoint, RepairIO, repair

    p = paths(cfg)
    r = cfg.get("repair", {})
    endpoint = ModelEndpoint(url=args.endpoint or r.get("url", ""), model=args.model or r.get("model", ModelEndpoint.model))
    if not endpoint.url:
        print("Set repair.url in the config (or pass --endpoint): the Ollama host to ask.")
        return 2
    queue_dir = p["queue"]
    if args.queue:
        bundles = sorted(d for d in queue_dir.iterdir() if d.is_dir()) if queue_dir.exists() else []
    else:
        bundles = [Path(args.bundle)]
    if not bundles:
        print("Repair queue is empty.")
        return 0
    values = sensitive_values(cfg, p)
    amounts = recent_amounts(p["normalized"])
    status = 0
    for bundle in bundles:
        print(f"\n### {bundle.name}")
        outcome = repair(
            bundle,
            endpoint=endpoint,
            sensitive_values=values,
            recent_amounts=amounts,
            io=RepairIO(),
            notifier=notifier(cfg),
            queue_dir=queue_dir,
            script_path=Path(args.script) if args.script else None,
        )
        print(f"-> {outcome}")
        if args.queue and outcome in ("applied", "not_applied", "rejected"):
            shutil.rmtree(bundle)  # handled; "declined", "blocked" and "queued" stay for another try
        status |= outcome not in ("applied",)
    return status


def cmd_due(args, cfg) -> int:
    from .schedule import ScheduleError, due_banks

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    try:
        banks = due_banks(Path(args.list).read_text(), Path(args.state), today)
    except ScheduleError as exc:
        print(f"banks list: {exc}", file=sys.stderr)
        return 2
    print("\n".join(banks))
    return 0


def cmd_check(args, cfg) -> int:
    from .steps import Script

    script = Script.load(Path(args.script))
    print(f"{script.institution}: {len(script.login)} login step(s), {len(script.steps)} step(s); OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest")
    parser.add_argument("--config", help=f"config file (default $INGEST_CONFIG or {DEFAULT_CONFIG})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="run one bank script")
    s.add_argument("bank")
    s.add_argument("--interactive", action="store_true", help="started by hand: ask for any verification code in the terminal")
    s.set_defaults(fn=cmd_scrape)

    s = sub.add_parser("load", help="parse new raw files and load them into Sure")
    s.add_argument("--dry-run", action="store_true", help="parse and write JSONL only")
    s.set_defaults(fn=cmd_load)

    s = sub.add_parser("repair", help="propose a fix for a failure bundle (asks before sending and before applying)")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("bundle", nargs="?")
    g.add_argument("--queue", action="store_true", help="work through queued bundles")
    s.add_argument("--script", help="script path, if not where the bundle says")
    s.add_argument("--endpoint", help="override the Ollama-compatible endpoint URL")
    s.add_argument("--model")
    s.set_defaults(fn=cmd_repair)

    s = sub.add_parser("due", help="print the banks due tonight, from banks.list and the last successful runs")
    s.add_argument("--list", default="/etc/ingest/banks.list")
    s.add_argument("--state", default="/var/lib/ingest/last-success")
    s.add_argument("--today", help="YYYY-MM-DD, for checking a schedule")
    s.set_defaults(fn=cmd_due, no_config=True)

    s = sub.add_parser("check", help="validate a bank script")
    s.add_argument("script")
    s.set_defaults(fn=cmd_check, no_config=True)

    args = parser.parse_args(argv)
    os.umask(0o027)  # nothing we write is world-readable
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = {} if getattr(args, "no_config", False) else load_config(args.config)
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
