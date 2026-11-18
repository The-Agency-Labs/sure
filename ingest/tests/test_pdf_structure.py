from ingest.pdf_structure import mask_line, structure
from tests.test_parsers import make_pdf


def test_mask_line_keeps_formats_drops_values():
    line = "Oct 03   Oct 05   TIM HORTONS #4521 TORONTO ON      12.34"
    assert mask_line(line) == "Oct 9  Oct 9  ‹x› #9 ‹x› ON  9.9"
    assert mask_line("Jane Q. Quixote  4506 12** **** 5678") == "‹x›  9 9** **** 9"
    assert mask_line("New balance  $1,284.50 CR") == "New balance  $9,9.9 CR"
    assert mask_line("Payment - thank you   -500.00") == "Payment - thank you  -9.9"


def test_structure_reads_pdf(tmp_path):
    pdf = tmp_path / "s.pdf"
    make_pdf(pdf, ["Statement period Oct 1 to Oct 31, 2026", "Jane Quixote 123 Maple St", "Closing balance 41,234.56"])
    out = structure(pdf)
    assert "--- page 1 ---" in out
    for leak in ("Jane", "Quixote", "Maple", "123", "41", "234", "2026"):
        assert leak not in out, leak
    assert "9,9.9" in out
