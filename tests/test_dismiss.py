from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.dismiss import dismiss
from core.models import (
    Candidate,
    ExplanationType,
    Signal,
    Transaction,
    VerdictOutcome,
)

PAID_DATE = date(2024, 6, 17)


def _tx(
    transaction_id: str,
    amount: Decimal | str,
    *,
    vendor_name_raw: str = "Acme LLC",
    vendor_id: str | None = "V-1",
    vendor_canonical: str | None = "ACME",
    invoice_number_raw: str | None = "INV-100",
    invoice_canonical: str | None = "100",
    invoice_date: date | None = date(2024, 6, 11),
    payment_date: date | None = PAID_DATE,
    invoice_amount: Decimal | None = None,
    po_number: str | None = "PO-9",
    payment_method: str | None = "EFT",
    payment_status: str | None = "PAID",
    department: str | None = "AGING",
    description: str | None = "Supplies",
    line_count: int = 1,
    distinct_line_amounts: list[Decimal] | None = None,
) -> Transaction:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    lines = distinct_line_amounts if distinct_line_amounts is not None else [money]
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=vendor_id,
        vendor_canonical=vendor_canonical,
        invoice_number_raw=invoice_number_raw,
        invoice_canonical=invoice_canonical,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=money,
        invoice_amount=invoice_amount,
        po_number=po_number,
        payment_method=payment_method,
        payment_status=payment_status,
        department=department,
        description=description,
        line_count=line_count,
        distinct_line_amounts=lines,
    )


def _candidate(
    candidate_id: str,
    left_id: str,
    right_id: str,
    *,
    signal: Signal = Signal.exact_duplicate,
    amount: Decimal | str = Decimal("100.00"),
) -> Candidate:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    return Candidate(
        candidate_id=candidate_id,
        transaction_ids=(left_id, right_id),
        signal=signal,
        amount_at_risk=money,
        stage_generated="blocking",
    )


def _true_duplicate_fixture() -> list[tuple[Candidate, Transaction, Transaction]]:
    """Known recoverable duplicates. Stage 4 must not dismiss any of these."""
    cases: list[tuple[Candidate, Transaction, Transaction]] = []

    cases.append(
        (
            _candidate("dup-exact", "dup-exact-a", "dup-exact-b", amount="1250.50"),
            _tx("dup-exact-a", Decimal("1250.50")),
            _tx("dup-exact-b", Decimal("1250.50")),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-multiline",
                "dup-ml-a",
                "dup-ml-b",
                amount="4059.60",
            ),
            _tx(
                "dup-ml-a",
                Decimal("4059.60"),
                line_count=5,
                distinct_line_amounts=[
                    Decimal("0.07"),
                    Decimal("0.10"),
                    Decimal("0.20"),
                    Decimal("1.13"),
                    Decimal("99.99"),
                ],
            ),
            _tx(
                "dup-ml-b",
                Decimal("4059.60"),
                line_count=5,
                distinct_line_amounts=[
                    Decimal("0.07"),
                    Decimal("0.10"),
                    Decimal("0.20"),
                    Decimal("1.13"),
                    Decimal("99.99"),
                ],
            ),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-recoded-gl",
                "dup-gl-a",
                "dup-gl-b",
                amount="500.00",
            ),
            _tx(
                "dup-gl-a",
                Decimal("500.00"),
                line_count=3,
                distinct_line_amounts=[Decimal("100.00"), Decimal("400.00")],
            ),
            _tx(
                "dup-gl-b",
                Decimal("500.00"),
                line_count=2,
                distinct_line_amounts=[Decimal("200.00"), Decimal("300.00")],
            ),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-fuzzy",
                "dup-fz-a",
                "dup-fz-b",
                signal=Signal.fuzzy_vendor,
                amount="88.00",
            ),
            _tx(
                "dup-fz-a",
                Decimal("88.00"),
                vendor_name_raw="W W GRAINGER INC",
                vendor_canonical="WWGRAINGER",
            ),
            _tx(
                "dup-fz-b",
                Decimal("88.00"),
                vendor_name_raw="W.W. GRAINGER INC.",
                vendor_canonical="WWGRAINGER",
            ),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-invoice-variant",
                "dup-inv-a",
                "dup-inv-b",
                signal=Signal.invoice_variant,
                amount="42.00",
            ),
            _tx(
                "dup-inv-a",
                Decimal("42.00"),
                invoice_number_raw="INV-0042",
                invoice_canonical="42",
            ),
            _tx(
                "dup-inv-b",
                Decimal("42.00"),
                invoice_number_raw="42",
                invoice_canonical="42",
            ),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-cross-dept",
                "dup-cd-a",
                "dup-cd-b",
                signal=Signal.cross_department,
                amount="310.00",
            ),
            _tx("dup-cd-a", Decimal("310.00"), department="AGING"),
            _tx("dup-cd-b", Decimal("310.00"), department="POLICE"),
        )
    )
    cases.append(
        (
            _candidate(
                "dup-no-status",
                "dup-ok-a",
                "dup-ok-b",
                amount="19.25",
            ),
            _tx(
                "dup-ok-a",
                Decimal("19.25"),
                payment_status=None,
                invoice_canonical=None,
                invoice_number_raw=None,
            ),
            _tx(
                "dup-ok-b",
                Decimal("19.25"),
                payment_status=None,
                invoice_canonical=None,
                invoice_number_raw=None,
            ),
        )
    )
    return cases


def test_cancelled_payment_is_dismissed() -> None:
    left = _tx("tx-paid", Decimal("3450.00"), payment_status="PAID")
    right = _tx(
        "tx-canc",
        Decimal("-3450.00"),
        payment_status="CANCELLED",
        payment_method="CANCELLATION",
    )
    candidate = _candidate("cand-cancelled", "tx-paid", "tx-canc", amount="3450.00")

    residual, verdicts = dismiss([candidate], [left, right])

    assert residual == []
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.candidate_id == "cand-cancelled"
    assert verdict.verdict is VerdictOutcome.dismiss
    assert verdict.explanation_type is ExplanationType.cancelled
    assert verdict.model_used == "deterministic"
    assert verdict.tokens_in == 0
    assert verdict.tokens_out == 0
    assert verdict.cache_read == 0
    assert verdict.confidence == 1.0
    assert verdict.evidence[0].field == "payment_status"
    assert "CANCELLED" in verdict.evidence[0].values


def test_same_transaction_id_is_dismissed() -> None:
    tx = _tx("tx-same", Decimal("100.00"))
    candidate = _candidate("cand-same", "tx-same", "tx-same")

    residual, verdicts = dismiss([candidate], [tx])

    assert residual == []
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.verdict is VerdictOutcome.dismiss
    assert verdict.explanation_type is ExplanationType.none
    assert verdict.evidence[0].field == "transaction_id"
    assert verdict.evidence[0].values == ["tx-same", "tx-same"]


def test_line_split_is_dismissed() -> None:
    left = _tx(
        "tx-split-a",
        Decimal("91.21"),
        line_count=3,
        distinct_line_amounts=[Decimal("12.00"), Decimal("30.00"), Decimal("49.21")],
    )
    right = _tx(
        "tx-split-b",
        Decimal("1383.53"),
        line_count=6,
        distinct_line_amounts=[
            Decimal("100.00"),
            Decimal("200.00"),
            Decimal("1083.53"),
        ],
    )
    candidate = _candidate(
        "cand-split",
        "tx-split-a",
        "tx-split-b",
        signal=Signal.split_payment,
        amount="91.21",
    )

    residual, verdicts = dismiss([candidate], [left, right])

    assert residual == []
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.verdict is VerdictOutcome.dismiss
    assert verdict.explanation_type is ExplanationType.line_split
    fields = {item.field for item in verdict.evidence}
    assert fields == {"line_count", "distinct_line_amounts"}
    assert all(type(value) is Decimal for value in left.distinct_line_amounts)
    assert all(type(value) is Decimal for value in right.distinct_line_amounts)


def test_true_duplicates_are_never_dismissed() -> None:
    fixture = _true_duplicate_fixture()
    candidates = [candidate for candidate, _left, _right in fixture]
    transactions = [tx for _candidate, left, right in fixture for tx in (left, right)]

    residual, verdicts = dismiss(candidates, transactions)

    assert verdicts == []
    assert [item.candidate_id for item in residual] == sorted(
        candidate.candidate_id for candidate in candidates
    )
    assert len(residual) == len(fixture)


def test_american_canceled_spelling_is_dismissed() -> None:
    left = _tx("tx-a", Decimal("10.00"), payment_status="PAID")
    right = _tx("tx-b", Decimal("10.00"), payment_status="Canceled")
    candidate = _candidate("cand-canceled", "tx-a", "tx-b", amount="10.00")
    _residual, verdicts = dismiss([candidate], [left, right])
    assert verdicts[0].explanation_type is ExplanationType.cancelled


def test_reversed_status_is_dismissed() -> None:
    left = _tx("tx-a", Decimal("10.00"), payment_status="REVERSED")
    right = _tx("tx-b", Decimal("10.00"), payment_status="PAID")
    candidate = _candidate("cand-reversed", "tx-a", "tx-b", amount="10.00")
    _residual, verdicts = dismiss([candidate], [left, right])
    assert verdicts[0].explanation_type is ExplanationType.cancelled


def test_voided_status_is_dismissed() -> None:
    left = _tx("tx-a", Decimal("10.00"), payment_status="VOID")
    right = _tx("tx-b", Decimal("10.00"), payment_status="voided")
    candidate = _candidate("cand-void", "tx-a", "tx-b", amount="10.00")
    _residual, verdicts = dismiss([candidate], [left, right])
    assert verdicts[0].explanation_type is ExplanationType.cancelled


def test_paid_and_missing_status_are_not_cancelled() -> None:
    left = _tx("tx-a", Decimal("10.00"), payment_status="PAID")
    right = _tx("tx-b", Decimal("10.00"), payment_status=None)
    candidate = _candidate("cand-paid", "tx-a", "tx-b", amount="10.00")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_unknown_status_is_not_cancelled() -> None:
    left = _tx("tx-a", Decimal("10.00"), payment_status="PENDING")
    right = _tx("tx-b", Decimal("10.00"), payment_status="NOT CANCELLED")
    candidate = _candidate("cand-pending", "tx-a", "tx-b", amount="10.00")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_blank_transaction_ids_are_not_same_id() -> None:
    left = _tx("", Decimal("10.00"))
    candidate = _candidate("cand-blank", "", "")
    residual, verdicts = dismiss([candidate], [left])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_requires_both_sides_multiline() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        line_count=3,
        distinct_line_amounts=[Decimal("12.00"), Decimal("79.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("12.21"),
        line_count=1,
        distinct_line_amounts=[Decimal("12.21")],
    )
    candidate = _candidate(
        "cand-partial",
        "tx-a",
        "tx-b",
        signal=Signal.split_payment,
        amount="12.21",
    )
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_when_amounts_match() -> None:
    left = _tx(
        "tx-a",
        Decimal("500.00"),
        line_count=3,
        distinct_line_amounts=[Decimal("100.00"), Decimal("400.00")],
    )
    right = _tx(
        "tx-b",
        Decimal("500.00"),
        line_count=2,
        distinct_line_amounts=[Decimal("200.00"), Decimal("300.00")],
    )
    candidate = _candidate("cand-same-amt", "tx-a", "tx-b", amount="500.00")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_without_invoice() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        invoice_canonical=None,
        invoice_number_raw=None,
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        invoice_canonical=None,
        invoice_number_raw=None,
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-no-inv", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_on_different_invoices() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        invoice_canonical="100",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        invoice_canonical="200",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-diff-inv", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_on_different_vendors() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        vendor_id="V-1",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        vendor_id="V-2",
        vendor_canonical="OTHER",
        vendor_name_raw="Other Corp",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-diff-vendor", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_on_identical_line_amounts() -> None:
    lines = [Decimal("10.00"), Decimal("20.00"), Decimal("30.00")]
    left = _tx(
        "tx-a",
        Decimal("60.00"),
        line_count=3,
        distinct_line_amounts=lines,
    )
    right = _tx(
        "tx-b",
        Decimal("90.00"),
        line_count=4,
        distinct_line_amounts=list(lines),
    )
    candidate = _candidate("cand-same-lines", "tx-a", "tx-b", amount="60.00")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_on_progress_dates() -> None:
    left = _tx(
        "tx-a",
        Decimal("100.00"),
        payment_date=date(2024, 1, 15),
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("90.00")],
        description="Progress payment 1 of 2",
    )
    right = _tx(
        "tx-b",
        Decimal("150.00"),
        payment_date=date(2024, 2, 15),
        line_count=2,
        distinct_line_amounts=[Decimal("50.00"), Decimal("100.00")],
        description="Progress payment 2 of 2",
    )
    candidate = _candidate(
        "cand-progress",
        "tx-a",
        "tx-b",
        signal=Signal.split_payment,
        amount="100.00",
    )
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_on_different_payment_method() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        payment_method="EFT",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        payment_method="CHECK",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-diff-method", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_when_line_amounts_missing() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        line_count=3,
        distinct_line_amounts=[],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-empty-lines", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_does_not_fire_without_payment_date() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        payment_date=None,
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        payment_date=None,
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-no-date", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_line_split_matches_on_raw_invoice_when_canonical_is_absent() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        invoice_canonical=None,
        invoice_number_raw="INV-100",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        invoice_canonical=None,
        invoice_number_raw="INV-100",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-raw-inv", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == []
    assert verdicts[0].explanation_type is ExplanationType.line_split


def test_line_split_matches_on_raw_vendor_when_ids_and_canonical_are_absent() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        vendor_id=None,
        vendor_canonical=None,
        vendor_name_raw="Acme LLC",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        vendor_id=None,
        vendor_canonical=None,
        vendor_name_raw="Acme LLC",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-raw-vendor", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == []
    assert verdicts[0].explanation_type is ExplanationType.line_split


def test_line_split_matches_on_canonical_vendor_when_ids_are_absent() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        vendor_id=None,
        vendor_canonical="ACME",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        vendor_id=None,
        vendor_canonical="ACME",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-canon-vendor", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == []
    assert verdicts[0].explanation_type is ExplanationType.line_split


def test_line_split_does_not_fire_on_different_po() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        po_number="PO-1",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        po_number="PO-2",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-diff-po", "tx-a", "tx-b", amount="91.21")
    residual, verdicts = dismiss([candidate], [left, right])
    assert residual == [candidate]
    assert verdicts == []


def test_missing_transaction_is_not_dismissed() -> None:
    left = _tx("tx-a", Decimal("10.00"))
    candidate = _candidate("cand-missing", "tx-a", "tx-missing")
    residual, verdicts = dismiss([candidate], [left])
    assert residual == [candidate]
    assert verdicts == []


def test_same_id_does_not_need_the_transaction_row() -> None:
    candidate = _candidate("cand-orphan-same", "tx-orphan", "tx-orphan")
    residual, verdicts = dismiss([candidate], [])
    assert residual == []
    assert verdicts[0].explanation_type is ExplanationType.none


def test_same_id_takes_precedence_over_cancelled() -> None:
    tx = _tx("tx-same", Decimal("10.00"), payment_status="CANCELLED")
    candidate = _candidate("cand-both", "tx-same", "tx-same", amount="10.00")
    _residual, verdicts = dismiss([candidate], [tx])
    assert verdicts[0].explanation_type is ExplanationType.none


def test_cancelled_takes_precedence_over_line_split() -> None:
    left = _tx(
        "tx-a",
        Decimal("91.21"),
        payment_status="CANCELLED",
        line_count=3,
        distinct_line_amounts=[Decimal("10.00"), Decimal("81.21")],
    )
    right = _tx(
        "tx-b",
        Decimal("50.00"),
        payment_status="PAID",
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("30.00")],
    )
    candidate = _candidate("cand-canc-split", "tx-a", "tx-b", amount="91.21")
    _residual, verdicts = dismiss([candidate], [left, right])
    assert verdicts[0].explanation_type is ExplanationType.cancelled


def test_mixed_batch_is_sorted_and_splits_residual_from_verdicts() -> None:
    cancelled_left = _tx("c-a", Decimal("10.00"), payment_status="PAID")
    cancelled_right = _tx("c-b", Decimal("10.00"), payment_status="CANCELLED")
    dup_left = _tx("d-a", Decimal("25.00"))
    dup_right = _tx("d-b", Decimal("25.00"))
    split_left = _tx(
        "s-a",
        Decimal("40.00"),
        line_count=2,
        distinct_line_amounts=[Decimal("15.00"), Decimal("25.00")],
    )
    split_right = _tx(
        "s-b",
        Decimal("60.00"),
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("40.00")],
    )
    candidates = [
        _candidate("cand-z-dup", "d-a", "d-b", amount="25.00"),
        _candidate("cand-m-split", "s-a", "s-b", amount="40.00"),
        _candidate("cand-a-canc", "c-a", "c-b", amount="10.00"),
    ]
    transactions = [
        cancelled_left,
        cancelled_right,
        dup_left,
        dup_right,
        split_left,
        split_right,
    ]

    residual, verdicts = dismiss(candidates, transactions)

    assert [item.candidate_id for item in residual] == ["cand-z-dup"]
    assert [item.candidate_id for item in verdicts] == ["cand-a-canc", "cand-m-split"]
    assert verdicts[0].explanation_type is ExplanationType.cancelled
    assert verdicts[1].explanation_type is ExplanationType.line_split


def test_two_runs_are_identical() -> None:
    left = _tx(
        "tx-a",
        Decimal("40.00"),
        payment_status="CANCELLED",
        line_count=2,
        distinct_line_amounts=[Decimal("15.00"), Decimal("25.00")],
    )
    right = _tx(
        "tx-b",
        Decimal("60.00"),
        line_count=2,
        distinct_line_amounts=[Decimal("20.00"), Decimal("40.00")],
    )
    keep_left = _tx("keep-a", Decimal("7.00"))
    keep_right = _tx("keep-b", Decimal("7.00"))
    candidates = [
        _candidate("cand-keep", "keep-a", "keep-b", amount="7.00"),
        _candidate("cand-drop", "tx-a", "tx-b", amount="40.00"),
    ]
    transactions = [right, keep_right, left, keep_left]
    first = dismiss(candidates, transactions, run_id="run")
    second = dismiss(reversed(candidates), reversed(transactions), run_id="run")
    assert first == second


def test_empty_input_returns_empty() -> None:
    assert dismiss([], []) == ([], [])
