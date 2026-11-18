from pathlib import Path

import pytest

from ingest.cli import expand, paths


def test_paths_expand_from_environment(monkeypatch):
    monkeypatch.setenv("FINANCE_DIR", "/x/finance")
    monkeypatch.setenv("STATE_DIR", "/x/state")
    p = paths({"paths": {"raw": "${FINANCE_DIR}/raw"}})
    assert p["raw"] == Path("/x/finance/raw")
    assert p["queue"] == Path("/x/state/repair-queue")  # default, config key repair_queue


def test_unset_variable_fails_only_when_used(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIR", raising=False)
    monkeypatch.setenv("FINANCE_DIR", "/x")
    p = paths({})
    assert p["raw"] == Path("/x/raw")
    with pytest.raises(SystemExit, match="variable that isn't set"):
        p["credentials"]


def test_plain_paths_still_work():
    assert expand("/srv/finance/raw") == Path("/srv/finance/raw")
