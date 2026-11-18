from decimal import Decimal

import pytest

from ingest.scrub import SensitiveValueFound, blocking_check, scrub_name, scrub_snapshot, url_pattern

RAW = '''- banner:
  - img "Fake Bank"
  - heading "Welcome back, Jane Quixote" [level=1]
- navigation:
  - link "Accounts":
    - /url: https://bank.example/accounts/77812/summary?session=abc
  - link "Statements":
    - /url: /accounts/77812/statements#top
- main:
  - heading "Chequing 4500 1234 5678 9012" [level=2]
  - paragraph: Balance $1,234.56
  - text: 123 Maple Street, Springfield
  - textbox "Card number": "4500123456789012"
  - textbox "Email": jane.quixote@example.com
  - checkbox "Remember this device" [checked]
  - combobox "Date range":
    - option "Last 30 days" [selected]
  - button "Download transactions"
  - dialog "Special offer for Jane":
    - button "No thanks"
'''


def test_keeps_only_interactive_and_heading_roles_with_names():
    out = scrub_snapshot(RAW)
    assert 'button "Download transactions"' in out
    assert 'checkbox "Remember this device"' in out
    assert 'combobox "Date range"' in out
    assert 'textbox "Card number"' in out
    for gone in ("paragraph", "text:", "img", "option", "banner", "[checked]", "[level"):
        assert gone not in out


def test_values_digits_names_and_urls_are_gone():
    out = scrub_snapshot(RAW)
    for leak in ("Jane", "Quixote", "4500", "9012", "1,234", "Maple", "Springfield", "jane.quixote", "77812", "session", "bank.example"):
        assert leak not in out, leak
    assert 'heading "Chequing #"' in out
    assert 'heading "Welcome back, ‹x›"' in out
    assert "/url: /accounts/:id/summary" in out
    assert "/url: /accounts/:id/statements" in out


def test_french_ui_words_survive():
    assert scrub_name("Télécharger le relevé") == "Télécharger le relevé"
    assert scrub_name("Bonjour Jean-Luc Tremblay") == "‹x›"


def test_url_pattern():
    assert url_pattern("https://x.example/en/accounts/123/history?q=1") == "/en/accounts/:id/history"
    assert url_pattern("/a/9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c/x") == "/a/:id/x"


SECRETS = ["4500123456789012", "jquixote", "Jane Quixote", "123 Maple Street, Springfield", "jane.quixote@example.com", "416-555-0199"]
AMOUNTS = [Decimal("1234.56"), Decimal("4.25")]


def test_clean_scrubbed_snapshot_passes_the_check():
    blocking_check(scrub_snapshot(RAW), SECRETS, AMOUNTS)


@pytest.mark.parametrize(
    "leak",
    [
        '- textbox "Card": 4500123456789012',
        '- heading "Card ending 9012"',
        '- link "jquixote"',
        '- heading "Hello Quixote"',
        '- heading "Maple"',
        '- text "jane.quixote@example.com"',
        '- text "(416) 555-0199"',
        '- heading "Balance 1,234.56"',
        '- heading "Solde 1 234,56 $"',
    ],
)
def test_blocking_check_catches_fake_secrets(leak):
    with pytest.raises(SensitiveValueFound) as exc:
        blocking_check(scrub_snapshot(RAW) + "\n" + leak, SECRETS, AMOUNTS)
    # The error must not repeat the value.
    assert "Quixote" not in str(exc.value) and "4500" not in str(exc.value)


def test_short_amounts_do_not_cause_false_alarms():
    blocking_check('- button "Page 4"', [], [Decimal("4.25")])
