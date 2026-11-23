"""Which banks are due tonight.

banks.list, one bank per line, scraped in this order:

    simplii                                      every night (the default)
    sunlife  every=14d anchor=2026-11-13 offset=2d

``every`` is the cadence in days. ``anchor`` is any one date in the cycle (a
payday), and ``offset`` is how long after it to run (time for contributions to
post). A bank is due when its most recent scheduled day has no successful run
on or after it, so a missed run (VM off, a code request) is caught up the next
night instead of waiting a whole cycle.

Success dates are one file per bank in the state directory, written by
nightly.sh after a scrape succeeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    bank: str
    every_days: int = 1
    anchor: date | None = None
    offset_days: int = 0

    def last_scheduled(self, today: date) -> date | None:
        """The most recent scheduled day on or before ``today``, or None if the first is still ahead."""
        if self.every_days == 1:
            return today
        first = self.anchor + timedelta(days=self.offset_days)
        if today < first:
            return None
        return first + timedelta(days=((today - first).days // self.every_days) * self.every_days)

    def is_due(self, today: date, last_success: date | None) -> bool:
        scheduled = self.last_scheduled(today)
        return scheduled is not None and (last_success is None or last_success < scheduled)


def _days(value: str, key: str) -> int:
    m = re.fullmatch(r"(\d+)d?", value)
    if not m or int(m.group(1)) < (1 if key == "every" else 0):
        raise ScheduleError(f"{key}={value!r}: use a whole number of days, like 14d")
    return int(m.group(1))


def parse_list(text: str) -> list[Entry]:
    entries = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        bank, *opts = line.split()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", bank):
            raise ScheduleError(f"line {n}: bad bank name {bank!r}")
        kv = {}
        for opt in opts:
            key, sep, value = opt.partition("=")
            if not sep or key not in ("every", "anchor", "offset"):
                raise ScheduleError(f"line {n}: unknown option {opt!r} (every=, anchor=, offset=)")
            kv[key] = value
        every = _days(kv.get("every", "1"), "every")
        offset = _days(kv.get("offset", "0"), "offset")
        anchor = None
        if every > 1:
            if "anchor" not in kv:
                raise ScheduleError(f"line {n}: {bank} runs every {every} days, so it needs anchor=YYYY-MM-DD")
            try:
                anchor = date.fromisoformat(kv["anchor"])
            except ValueError:
                raise ScheduleError(f"line {n}: anchor must be YYYY-MM-DD") from None
        elif "anchor" in kv or "offset" in kv:
            raise ScheduleError(f"line {n}: anchor/offset only make sense with every=")
        entries.append(Entry(bank, every, anchor, offset))
    return entries


def last_success(state_dir: Path, bank: str) -> date | None:
    try:
        return date.fromisoformat((state_dir / bank).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def due_banks(list_text: str, state_dir: Path, today: date) -> list[str]:
    return [e.bank for e in parse_list(list_text) if e.is_due(today, last_success(state_dir, e.bank))]
