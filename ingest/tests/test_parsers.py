import zlib
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.parsers import ParseError, parse_money, pick_date_format
from ingest.parsers import mapped_csv, simplii, sunlife


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.mark.parametrize(
    "text,expected",
    [("1,234.56", "1234.56"), ("$1 234,56", "1234.56"), ("(12.00)", "-12.00"), ("-12.00", "-12.00"), ("12.00 CR", "12.00"), ("", None)],
)
def test_parse_money(text, expected):
    assert parse_money(text) == (Decimal(expected) if expected else None)


def test_date_format_detection_prefers_unambiguous():
    assert pick_date_format(["13/01/2026", "02/01/2026"]) == "%d/%m/%Y"
    assert pick_date_format(["01/13/2026"]) == "%m/%d/%Y"


def test_simplii_headerless_withdrawal_deposit(tmp_path):
    f = write(tmp_path, "chequing__2026-11-16.csv",
              "2026-11-02,TIM HORTONS #1234,4.25,\n2026-11-02,TIM HORTONS #1234,4.25,\n2026-11-03,PAYROLL DEPOSIT,,2500.00\n")
    recs = simplii.parse(f).records
    assert [(r.date, r.amount) for r in recs] == [("2026-11-02", Decimal("-4.25")), ("2026-11-02", Decimal("-4.25")), ("2026-11-03", Decimal("2500.00"))]
    assert all(r.account_ref == "chequing" and r.source == "simplii" for r in recs)
    assert len({r.external_id for r in recs}) == 3


def test_simplii_tolerates_header_slash_dates_and_extra_column(tmp_path):
    f = write(tmp_path, "savings__x.csv",
              "Date,Description,Withdrawals,Deposits,Card\n10/31/2026,INTEREST,,1.02,4500********9012\n11/01/2026,FEE,\"1,000.00\",,4500********9012\n")
    recs = simplii.parse(f).records
    assert [(r.date, r.amount) for r in recs] == [("2026-10-31", Decimal("1.02")), ("2026-11-01", Decimal("-1000.00"))]


def test_simplii_rejects_row_with_both_columns(tmp_path):
    f = write(tmp_path, "chequing__x.csv", "2026-11-02,ODD,1.00,2.00\n")
    with pytest.raises(ParseError):
        simplii.parse(f)


def test_raw_file_name_must_carry_account_ref(tmp_path):
    f = write(tmp_path, "transactions.csv", "2026-11-02,X,1.00,\n")
    with pytest.raises(ParseError):
        simplii.parse(f)


def test_mapped_csv_card_style_sign_and_running_balance(tmp_path):
    f = write(tmp_path, "gold__x.csv",
              "Date,Description,Amount,Balance\n11/03/2026,PAYMENT RECEIVED,-500.00,100.00\n11/02/2026,GROCER,75.50,600.00\n")
    cfg = {"date": "Date", "description": "Description", "amount": "Amount", "sign": -1, "balance": "Balance"}
    parsed = mapped_csv.parse(f, institution="amex", **cfg)
    assert [r.amount for r in parsed.records] == [Decimal("500.00"), Decimal("-75.50")]
    # Card balances run the other way; the loader's check is on the parsed numbers.
    assert parsed.opening is not None and parsed.closing == Decimal("100.00")


def test_mapped_csv_missing_columns(tmp_path):
    f = write(tmp_path, "gold__x.csv", "When,What,HowMuch\n2026-11-02,X,1\n")
    with pytest.raises(ParseError, match="missing columns"):
        mapped_csv.parse(f, institution="amex", date="Date", description="Description", amount="Amount")


def make_pdf(path: Path, lines: list[str]) -> Path:
    """A minimal one-page PDF with text lines (no external dependency)."""
    ops = ["BT /F1 11 Tf 50 750 Td 14 TL"]
    for line in lines:
        esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({esc}) Tj T*")
    ops.append("ET")
    stream = zlib.compress("\n".join(ops).encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def test_sunlife_pdf_closing_balance(tmp_path):
    f = make_pdf(tmp_path / "rrsp__2026-q3.pdf", [
        "Group Retirement Savings Plan",
        "Statement for the period July 1, 2026 to September 30, 2026",
        "Opening balance $40,000.00",
        "Closing balance $41,234.56",
    ])
    b = sunlife.parse(f).balance
    assert (b.account_ref, b.date, b.amount) == ("rrsp", "2026-09-30", Decimal("41234.56"))


def test_sunlife_french_statement():
    parsed = sunlife.parse_text(
        "Relevé pour la période du 1 juillet 2026 au 30 septembre 2026\nSolde de clôture 41 234,56 $\n",
        account_ref="rrsp", raw_file="x.pdf",
    )
    assert (parsed.balance.date, parsed.balance.amount) == ("2026-09-30", Decimal("41234.56"))


def test_sunlife_ambiguous_balance_is_an_error():
    with pytest.raises(ParseError, match="exactly one"):
        sunlife.parse_text("as of September 30, 2026\nClosing balance $1.00\nEnding balance $2.00\n", account_ref="r", raw_file="x")
