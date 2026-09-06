from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.aggregate import AggregateError, aggregate, write_jsonl
from core.models import Transaction

DISTINCT_LINE_AMOUNTS: tuple[Decimal, ...] = (
    Decimal("0.10"),
    Decimal("0.20"),
    Decimal("1.13"),
    Decimal("99.99"),
    Decimal("0.07"),
)
LINES_PER_AMOUNT = 40
TWO_HUNDRED_LINE_COUNT = len(DISTINCT_LINE_AMOUNTS) * LINES_PER_AMOUNT
TWO_HUNDRED_LINE_SUM = (
    Decimal("0.10") * 40
    + Decimal("0.20") * 40
    + Decimal("1.13") * 40
    + Decimal("99.99") * 40
    + Decimal("0.07") * 40
)


def _line(
    transaction_id: str,
    amount: Decimal | str,
    *,
    vendor_name_raw: str = "Acme LLC",
    vendor_id: str | None = "V-1",
    invoice_number_raw: str | None = "INV-100",
    invoice_date: date | None = date(2024, 6, 11),
    payment_date: date | None = date(2024, 6, 17),
    invoice_amount: Decimal | None = Decimal("4059.60"),
    po_number: str | None = "PO-9",
    payment_method: str | None = "EFT",
    payment_status: str | None = "PAID",
    department: str | None = "AGING",
    description: str | None = "Nutrition",
) -> Transaction:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=vendor_id,
        invoice_number_raw=invoice_number_raw,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=money,
        invoice_amount=invoice_amount,
        po_number=po_number,
        payment_method=payment_method,
        payment_status=payment_status,
        department=department,
        description=description,
        line_count=1,
        distinct_line_amounts=[money],
    )


@pytest.fixture
def two_hundred_line_invoice() -> list[Transaction]:
    amounts = [
        amount for _ in range(LINES_PER_AMOUNT) for amount in DISTINCT_LINE_AMOUNTS
    ]
    assert len(amounts) == TWO_HUNDRED_LINE_COUNT
    return [_line("tx-mid", amount) for amount in amounts]


def test_fixture_sum_is_exact_to_the_cent() -> None:
    assert TWO_HUNDRED_LINE_COUNT == 200
    assert TWO_HUNDRED_LINE_SUM == Decimal("4059.60")


def test_two_hundred_line_invoice_collapses_to_one_transaction(
    two_hundred_line_invoice: list[Transaction],
) -> None:
    rows = two_hundred_line_invoice
    rows.insert(0, _line("tx-zzz", Decimal("1.00"), invoice_amount=Decimal("1.00")))
    rows.append(_line("tx-aaa", Decimal("5.00"), invoice_amount=Decimal("5.00")))

    result = aggregate(rows)

    assert [tx.transaction_id for tx in result] == ["tx-aaa", "tx-mid", "tx-zzz"]
    mid = result[1]
    assert mid.line_count == 200
    assert mid.distinct_line_amounts == sorted(DISTINCT_LINE_AMOUNTS)
    assert len(mid.distinct_line_amounts) == 5
    assert mid.amount == TWO_HUNDRED_LINE_SUM
    assert mid.amount == Decimal("4059.60")
    assert type(mid.amount) is Decimal
    assert all(type(value) is Decimal for value in mid.distinct_line_amounts)
    assert mid.vendor_name_raw == "Acme LLC"
    assert mid.invoice_number_raw == "INV-100"
    assert result[0].line_count == 1
    assert result[2].amount == Decimal("1.00")


def test_two_runs_produce_byte_identical_output(
    tmp_path: Path, two_hundred_line_invoice: list[Transaction]
) -> None:
    rows = (
        two_hundred_line_invoice
        + [_line("tx-aaa", Decimal("5.00"), invoice_amount=Decimal("5.00"))]
        + [_line("tx-zzz", Decimal("1.00"), invoice_amount=Decimal("1.00"))]
    )
    first = tmp_path / "run1.jsonl"
    second = tmp_path / "run2.jsonl"
    write_jsonl(aggregate(rows), first)
    write_jsonl(aggregate(rows), second)
    payload = first.read_bytes()
    assert payload == second.read_bytes()
    assert payload
    assert b"\r\n" not in payload


def test_reversed_input_is_byte_identical(
    tmp_path: Path, two_hundred_line_invoice: list[Transaction]
) -> None:
    rows = two_hundred_line_invoice
    first = tmp_path / "forward.jsonl"
    second = tmp_path / "reversed.jsonl"
    write_jsonl(aggregate(rows), first)
    write_jsonl(aggregate(reversed(rows)), second)
    assert first.read_bytes() == second.read_bytes()


def test_streaming_generator_is_consumed_incrementally(
    two_hundred_line_invoice: list[Transaction],
) -> None:
    def lines() -> Iterator[Transaction]:
        yield from two_hundred_line_invoice

    result = aggregate(lines())
    assert len(result) == 1
    assert result[0].line_count == 200
    assert result[0].amount == Decimal("4059.60")
    assert result[0].distinct_line_amounts == sorted(DISTINCT_LINE_AMOUNTS)


def test_first_non_empty_identity_value_is_kept() -> None:
    rows = [
        _line("tx-1", Decimal("1.00"), vendor_name_raw="  ", invoice_number_raw=None),
        _line(
            "tx-1", Decimal("2.00"), vendor_name_raw="Acme LLC", invoice_number_raw=""
        ),
        _line(
            "tx-1",
            Decimal("3.00"),
            vendor_name_raw="Acme LLC",
            invoice_number_raw="INV-100",
        ),
    ]
    result = aggregate(rows)
    assert len(result) == 1
    assert result[0].vendor_name_raw == "Acme LLC"
    assert result[0].invoice_number_raw == "INV-100"
    assert result[0].amount == Decimal("6.00")
    assert result[0].line_count == 3


def test_identity_disagreement_records_nothing() -> None:
    rows = [
        _line(
            "tx-1",
            Decimal("1.00"),
            vendor_name_raw="Acme LLC",
            invoice_number_raw="INV-100",
            department="AGING",
            description="Line A",
        ),
        _line(
            "tx-1",
            Decimal("2.00"),
            vendor_name_raw="Other Corp",
            invoice_number_raw="INV-200",
            department="POLICE",
            description="Line B",
        ),
    ]
    result = aggregate(rows)
    tx = result[0]
    assert tx.vendor_name_raw == ""
    assert tx.invoice_number_raw is None
    assert tx.department is None
    assert tx.description is None
    assert tx.amount == Decimal("3.00")
    assert tx.line_count == 2
    assert tx.distinct_line_amounts == [Decimal("1.00"), Decimal("2.00")]


def test_blank_transaction_id_raises() -> None:
    with pytest.raises(AggregateError, match="transaction_id is required"):
        aggregate([_line("   ", Decimal("1.00"))])


def test_empty_input_returns_empty_list() -> None:
    assert aggregate([]) == []


def test_single_line_passthrough() -> None:
    row = _line("tx-1", Decimal("10.25"))
    result = aggregate([row])
    assert len(result) == 1
    assert result[0].transaction_id == "tx-1"
    assert result[0].amount == Decimal("10.25")
    assert result[0].line_count == 1
    assert result[0].distinct_line_amounts == [Decimal("10.25")]
