from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from core.models import (
    Candidate,
    Decision,
    Evidence,
    ExplanationType,
    ReviewDecision,
    Signal,
    Transaction,
    Verdict,
    VerdictOutcome,
)
from packet.builder import STAGE_ORDER, Packet, PacketError, build_packet

ISSUED_AT = datetime(2024, 7, 1, 9, 30, 0, tzinfo=UTC)
PAID_A = date(2024, 6, 11)
PAID_B = date(2024, 6, 12)


def _tx(
    transaction_id: str,
    amount: Decimal | str,
    *,
    vendor_name_raw: str = "Acme Paving Inc",
    vendor_canonical: str | None = "ACME PAVING INC",
    invoice_number_raw: str | None = "INV-100",
    invoice_canonical: str | None = "INV100",
    payment_date: date | None = PAID_A,
    invoice_date: date | None = date(2024, 6, 4),
    department: str | None = "PUBLIC WORKS",
    description: str | None = "Asphalt repair",
) -> Transaction:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id="V-1",
        vendor_canonical=vendor_canonical,
        invoice_number_raw=invoice_number_raw,
        invoice_canonical=invoice_canonical,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=money,
        invoice_amount=money,
        po_number="PO-9",
        payment_method="ACH",
        payment_status="PAID",
        department=department,
        description=description,
        line_count=1,
        distinct_line_amounts=[money],
    )


def _candidate(
    *,
    amount: Decimal | str = Decimal("1250.50"),
    left_id: str = "tx-100",
    right_id: str = "tx-200",
) -> Candidate:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    return Candidate(
        candidate_id="cand-1",
        transaction_ids=(left_id, right_id),
        signal=Signal.exact_duplicate,
        amount_at_risk=money,
        stage_generated="blocking",
    )


def _verdict() -> Verdict:
    return Verdict(
        candidate_id="cand-1",
        verdict=VerdictOutcome.escalate,
        explanation_type=ExplanationType.none,
        reasoning="Same invoice number and amount on two distinct payments.",
        evidence=[
            Evidence(field="invoice_canonical", values=["INV100", "INV100"]),
        ],
        confidence=0.91,
        model_used="fast-model",
        tokens_in=120,
        tokens_out=40,
        cache_read=80,
    )


def _decision(*, decision: Decision = Decision.confirmed) -> ReviewDecision:
    return ReviewDecision(
        candidate_id="cand-1",
        approver="Elena Voss",
        decision=decision,
        reason="Same invoice paid twice.",
        becomes_rule=True,
    )


def _company_rows() -> list[Transaction]:
    return [
        _tx("tx-200", Decimal("1250.50"), payment_date=PAID_B),
        _tx("tx-100", Decimal("1250.50"), payment_date=PAID_A),
    ]


def _build(
    *,
    candidate: Candidate | None = None,
    verdict: Verdict | None = None,
    decision: ReviewDecision | None = None,
    transactions: list[Transaction] | None = None,
    issued_at: datetime | date = ISSUED_AT,
) -> Packet:
    return build_packet(
        candidate if candidate is not None else _candidate(),
        verdict if verdict is not None else _verdict(),
        decision if decision is not None else _decision(),
        transactions if transactions is not None else _company_rows(),
        issued_at=issued_at,
        run_id="test",
    )


def test_confirmed_finding_produces_letter_journal_and_audit_trail() -> None:
    packet = _build()

    assert packet.packet_id == "cand-1"
    letter = packet.letter
    journal = packet.journal
    audit = packet.audit_trail

    assert "tx-100 (invoice INV-100)" in letter
    assert "tx-200 (invoice INV-100)" in letter
    assert "June 11, 2024" in letter
    assert "June 12, 2024" in letter
    assert "$1,250.50" in letter
    assert "Acme Paving Inc" in letter
    assert "query" in letter.lower()
    assert "not a claim" in letter.lower()
    assert "fraud" not in letter.lower()
    assert "you owe" not in letter.lower()

    assert "Debit" in journal
    assert "Credit" in journal
    assert "Due from supplier" in journal
    assert "Expenditure recovery" in journal
    assert journal.count("$1,250.50") >= 2
    assert "tx-100" in journal
    assert "tx-200" in journal
    assert "sourced from" in journal.lower()

    for stage in STAGE_ORDER:
        assert stage in audit
    assert "Elena Voss" in audit
    assert audit.rstrip().endswith("who confirmed this finding.")
    assert "2024-07-01 09:30:00 UTC" in audit
    assert "Same invoice paid twice" in audit
    assert "Verdict escalate" in audit


def test_individual_name_is_masked_in_shareable_artefacts() -> None:
    rows = [
        _tx(
            "tx-100",
            Decimal("80.00"),
            vendor_name_raw="Jane Doe",
            vendor_canonical="jane doe",
            invoice_number_raw="INV-9",
            payment_date=PAID_A,
        ),
        _tx(
            "tx-200",
            Decimal("80.00"),
            vendor_name_raw="Jane Doe",
            vendor_canonical="jane doe",
            invoice_number_raw="INV-9",
            payment_date=PAID_B,
        ),
    ]
    packet = _build(
        candidate=_candidate(amount=Decimal("80.00")),
        transactions=rows,
    )
    artefacts = (packet.letter, packet.journal, packet.audit_trail)
    combined = "\n".join(artefacts)
    assert "Jane Doe" not in combined
    assert "jane doe" not in combined
    assert "Individual · J. D." in packet.letter
    assert "Individual · J. D." in packet.journal
    assert "Elena Voss" in packet.audit_trail
    assert "$80.00" in packet.letter


def test_same_finding_twice_is_byte_identical() -> None:
    first = _build()
    second = _build()
    assert first == second
    assert first.letter == second.letter
    assert first.journal == second.journal
    assert first.audit_trail == second.audit_trail
    assert first.packet_id == "cand-1"


def test_dismissed_decision_is_refused() -> None:
    with pytest.raises(PacketError, match="not confirmed"):
        _build(decision=_decision(decision=Decision.dismissed))


def test_missing_transaction_fails_clearly() -> None:
    with pytest.raises(PacketError, match="missing transaction tx-200"):
        _build(transactions=[_company_rows()[1]])


def test_mismatched_ids_fail_clearly() -> None:
    verdict = _verdict().model_copy(update={"candidate_id": "other"})
    with pytest.raises(PacketError, match="verdict candidate_id"):
        _build(verdict=verdict)
    decision = _decision().model_copy(update={"candidate_id": "other"})
    with pytest.raises(PacketError, match="review decision candidate_id"):
        _build(decision=decision)
