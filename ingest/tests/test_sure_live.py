"""Loader against a real Sure instance. Skipped unless configured:

  SURE_TEST_URL=http://127.0.0.1:3000
  SURE_TEST_API_KEY_FILE=/path/to/key        (read_write key)
  SURE_TEST_ACCOUNT="Ingest Test"            (a throwaway manual account)

Never point this at an account with real data you care about: it creates
three transactions dated 2000-01-0x.
"""

import os
import uuid
from pathlib import Path

import pytest

from ingest.loader import Loader
from ingest.sure import SureClient

URL = os.environ.get("SURE_TEST_URL")
KEY_FILE = os.environ.get("SURE_TEST_API_KEY_FILE")
ACCOUNT = os.environ.get("SURE_TEST_ACCOUNT")

pytestmark = [pytest.mark.sure, pytest.mark.skipif(not (URL and KEY_FILE and ACCOUNT), reason="no live Sure configured")]


def test_rerun_creates_no_duplicates(tmp_path):
    client = SureClient(URL, Path(KEY_FILE).read_text().strip())
    marker = uuid.uuid4().hex[:8]  # unique descriptions so earlier test runs don't interfere
    raw = tmp_path / "raw/simplii/2000"
    raw.mkdir(parents=True)
    (raw / "testacct__x.csv").write_text(
        f"2000-01-02,TEST {marker},4.25,\n2000-01-02,TEST {marker},4.25,\n2000-01-03,TEST DEPOSIT {marker},,10.00\n"
    )
    cfg = {"accounts": {"simplii:testacct": ACCOUNT}, "formats": {"simplii": {"parser": "simplii"}}}

    first = Loader(cfg, tmp_path / "raw", tmp_path / "n", tmp_path / "r", client).run()
    assert (first.created, first.existing, first.errors) == (3, 0, [])

    (tmp_path / "n/.ledger.json").unlink()
    second = Loader(cfg, tmp_path / "raw", tmp_path / "n", tmp_path / "r", client).run()
    assert (second.created, second.existing, second.errors) == (0, 3, [])
