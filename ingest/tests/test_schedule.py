from datetime import date

import pytest

from ingest.schedule import Entry, ScheduleError, due_banks, parse_list

LIST = """
# order matters
simplii
sunlife  every=14d anchor=2026-11-13 offset=2d   # payday + 2 days
"""


def test_parse():
    assert parse_list(LIST) == [Entry("simplii"), Entry("sunlife", 14, date(2026, 11, 13), 2)]


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 11, 14), None),                # before the first scheduled day
        (date(2026, 11, 15), date(2026, 11, 15)),  # payday + 2
        (date(2026, 11, 28), date(2026, 11, 15)),
        (date(2026, 11, 29), date(2026, 11, 29)),  # two weeks later
    ],
)
def test_last_scheduled(today, expected):
    assert Entry("sunlife", 14, date(2026, 11, 13), 2).last_scheduled(today) == expected


def test_biweekly_runs_on_schedule_and_catches_up_after_a_miss(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    # First scheduled night: both due.
    assert due_banks(LIST, state, date(2026, 11, 15)) == ["simplii", "sunlife"]
    (state / "sunlife").write_text("2026-11-15")
    (state / "simplii").write_text("2026-11-15")
    # Following nights: only the daily bank.
    assert due_banks(LIST, state, date(2026, 11, 16)) == ["simplii"]
    assert due_banks(LIST, state, date(2026, 11, 28)) == ["simplii"]
    # Scheduled night 2026-11-29 fails (no success written): still due the next night.
    assert "sunlife" in due_banks(LIST, state, date(2026, 11, 29))
    assert "sunlife" in due_banks(LIST, state, date(2026, 11, 30))
    (state / "sunlife").write_text("2026-11-30")
    assert "sunlife" not in due_banks(LIST, state, date(2026, 12, 1))


def test_daily_bank_runs_once_a_day(tmp_path):
    (tmp_path / "simplii").write_text("2026-11-16")
    assert due_banks("simplii", tmp_path, date(2026, 11, 16)) == []
    assert due_banks("simplii", tmp_path, date(2026, 11, 17)) == ["simplii"]


@pytest.mark.parametrize(
    "line,match",
    [
        ("sunlife every=14d", "needs anchor"),
        ("sunlife every=14d anchor=13/11/2026", "YYYY-MM-DD"),
        ("sunlife every=0d anchor=2026-11-13", "whole number"),
        ("simplii offset=2d", "only make sense"),
        ("simplii weekly", "unknown option"),
        ("Sun Life", "bad bank name"),
    ],
)
def test_bad_lines(line, match):
    with pytest.raises(ScheduleError, match=match):
        parse_list(line)
