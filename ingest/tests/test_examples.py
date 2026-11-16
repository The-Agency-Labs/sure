"""The example files under deploy/ are copied onto the VM as-is; they must parse."""

import json
from pathlib import Path

import pytest

from ingest.steps import Script

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("path", sorted((ROOT / "deploy").rglob("*.json")), ids=lambda p: p.name)
def test_example_json_parses(path):
    json.loads(path.read_text())


@pytest.mark.parametrize("path", sorted((ROOT / "banks").glob("*.json")), ids=lambda p: p.name)
def test_bank_scripts_are_valid(path):
    Script.load(path)
