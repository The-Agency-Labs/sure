import json

from ingest.clean_recording import clean

RECORDING = '''
import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://online.simplii.com/ebm-resources/public/client/web/index.html?lang=en#/signon?token=abc123XYZ")
    page.get_by_role("button", name="Accept all").click()
    page.get_by_label("Card number").click()
    page.get_by_label("Card number").fill("4506 4455 1234 5678")
    page.get_by_role("textbox", name="Password").fill("Hunter2!Quixote")
    page.get_by_role("textbox", name="Password").press("Enter")
    page.get_by_label("Verification code").fill("831204")
    page.get_by_role("link", name="No Fee Chequing Account 4455-1234 Jane Quixote $2,345.67").click()
    page.get_by_text("Thanks, Jane").click()
    page.locator("#acct-44551234 >> text=Jane Quixote").click()
    page.get_by_role("combobox", name="Format").select_option("CSV")
    page.locator('[data-test-id="card-number-input"]').click()
    page.locator('[data-test-id="account-4455-Jane"]').click()
    page.get_by_role("link", name="Download Oct 16 to Nov 15").click()
    page.get_by_test_id("row-998877").nth(2).click()
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download transactions").click()
    download = download_info.value
    page.evaluate("secretThing('Jane Quixote')")
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
'''
LEAKS = ["4506", "4455", "1234", "5678", "Hunter2", "Quixote", "Jane", "831204", "2,345.67", "345", "abc123", "998877", "secretThing"]


def test_nothing_sensitive_survives():
    out = clean(RECORDING)
    for leak in LEAKS:
        assert leak not in out, leak
    assert "[typed value removed]" in out


def test_structure_is_kept():
    out = clean(RECORDING)
    assert "goto   https://online.simplii.com/ebm-resources/public/client/web/index.html" in out
    assert 'click  role=button "Accept all"' in out
    assert "fill   label \"Card number\"   [typed value removed]" in out
    assert "press  role=textbox \"Password\"   Enter" in out
    assert 'role=link "No Fee Chequing Account # ‹x› $ #"' in out
    assert 'select role=combobox "Format"   "CSV"' in out
    assert "expect download:" in out and '    click  role=button "Download transactions"' in out
    assert "[skipped: .evaluate(...)]" in out
    assert 'click  css [data-test-id="card-number-input"]' in out
    assert 'click  css [data-test-id="account - # - ‹x›"]' in out
    assert 'click  role=link "Download Oct # to Nov #"' in out


def test_keep_words_and_check(tmp_path):
    assert '"Thanks, ‹x›"' in clean(RECORDING)
    out = clean(RECORDING, keep=frozenset({"jane"}))
    assert '"Thanks, Jane"' in out
    import pytest

    from ingest.scrub import SensitiveValueFound

    with pytest.raises(SensitiveValueFound):
        clean(RECORDING, keep=frozenset({"jane"}), check_values=["Jane"])
