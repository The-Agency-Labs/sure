from decimal import Decimal

from ingest.canonical import Record, build_records, external_id


def rows():
    return [
        ("2026-11-02", Decimal("-4.25"), "TIM HORTONS #1234"),
        ("2026-11-02", Decimal("-4.25"), "TIM  HORTONS #1234"),  # same purchase twice, odd spacing
        ("2026-11-03", Decimal("2500"), "PAYROLL DEPOSIT"),
    ]


def build(r):
    return build_records(r, account_ref="chequing", currency="CAD", source="simplii", raw_file="x.csv")


def test_identical_same_day_rows_get_distinct_ids():
    a, b, _ = build(rows())
    assert a.external_id != b.external_id
    assert a.description == b.description == "TIM HORTONS #1234"


def test_ids_are_stable_across_overlapping_files():
    first = build(rows())
    later = build([("2026-11-01", Decimal("-9.99"), "NETFLIX")] + rows())
    assert {r.external_id for r in first} <= {r.external_id for r in later}


def test_id_ignores_raw_file_and_amount_formatting():
    assert external_id("s", "a", "2026-01-01", Decimal("4.5"), "X", 0) == external_id("s", "a", "2026-01-01", Decimal("4.50"), "X", 0)
    assert external_id("s", "a", "2026-01-01", Decimal("4.5"), "X", 0) != external_id("s", "b", "2026-01-01", Decimal("4.5"), "X", 0)
    assert external_id("s", "a", "2026-01-01", Decimal("4.5"), "X", 0) != external_id("s", "a", "2026-01-01", Decimal("-4.5"), "X", 0)


def test_json_round_trip():
    r = build(rows())[0]
    assert Record.from_json(r.to_json()) == r
