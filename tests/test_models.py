from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from core.models import (
    Candidate,
    Decision,
    Decoy,
    Evidence,
    ExplanationType,
    GroundTruth,
    ReviewDecision,
    Signal,
    Transaction,
    TrueDuplicate,
    Verdict,
    VerdictOutcome,
)


def _transaction() -> Transaction:
    return Transaction(
        transaction_id="tx-1",
        vendor_name_raw="Acme LLC",
        vendor_id="V-1",
        vendor_canonical="acme",
        invoice_number_raw="INV-100",
        invoice_canonical="INV100",
        invoice_date=date(2024, 1, 15),
        payment_date=date(2024, 1, 20),
        amount=Decimal("1250.50"),
        invoice_amount=Decimal("1250.50"),
        po_number="PO-9",
        payment_method="ACH",
        payment_status="paid",
        department="Public Works",
        description="Asphalt repair",
        line_count=2,
        distinct_line_amounts=[Decimal("1000.00"), Decimal("250.50")],
    )


def _candidate() -> Candidate:
    return Candidate(
        candidate_id="cand-1",
        transaction_ids=("tx-1", "tx-2"),
        signal=Signal.exact_duplicate,
        amount_at_risk=Decimal("1250.50"),
        stage_generated="blocking",
    )


def _evidence() -> Evidence:
    return Evidence(field="invoice_canonical", values=["INV100", "INV-100"])


def _verdict() -> Verdict:
    return Verdict(
        candidate_id="cand-1",
        verdict=VerdictOutcome.escalate,
        explanation_type=ExplanationType.none,
        reasoning="Amounts match and vendors are aliases.",
        evidence=[_evidence()],
        confidence=0.42,
        model_used="fast-model",
        tokens_in=120,
        tokens_out=40,
        cache_read=80,
    )


def _review_decision() -> ReviewDecision:
    return ReviewDecision(
        candidate_id="cand-1",
        approver="ada",
        decision=Decision.confirmed,
        reason="Same invoice paid twice.",
        becomes_rule=True,
    )


def _true_duplicate() -> TrueDuplicate:
    return TrueDuplicate(
        case_id="dup-1",
        transaction_ids=["tx-1", "tx-2"],
        type="exact_duplicate",
        amount=Decimal("1250.50"),
    )


def _decoy() -> Decoy:
    return Decoy(
        case_id="decoy-1",
        transaction_ids=["tx-3", "tx-4"],
        reason="progress_payment",
        why_rules_cannot_tell="Only the description names draw 3 of 5.",
    )


def _ground_truth() -> GroundTruth:
    return GroundTruth(true_duplicates=[_true_duplicate()], decoys=[_decoy()])


def _assert_decimals_are_json_strings(obj: object, data: object) -> None:
    if isinstance(obj, Decimal):
        assert isinstance(data, str)
        assert Decimal(data) == obj
        return
    if isinstance(obj, BaseModel):
        assert isinstance(data, dict)
        for name in obj.__class__.model_fields:
            _assert_decimals_are_json_strings(getattr(obj, name), data[name])
        return
    if isinstance(obj, (list, tuple)):
        assert isinstance(data, list)
        for item, raw in zip(obj, data, strict=True):
            _assert_decimals_are_json_strings(item, raw)


@pytest.mark.parametrize(
    "instance",
    [
        _transaction(),
        _candidate(),
        _evidence(),
        _verdict(),
        _review_decision(),
        _true_duplicate(),
        _decoy(),
        _ground_truth(),
    ],
    ids=[
        "Transaction",
        "Candidate",
        "Evidence",
        "Verdict",
        "ReviewDecision",
        "TrueDuplicate",
        "Decoy",
        "GroundTruth",
    ],
)
def test_json_round_trip_keeps_decimals_as_strings(instance: BaseModel) -> None:
    raw = instance.model_dump_json()
    payload = json.loads(raw)
    restored = type(instance).model_validate_json(raw)
    assert restored == instance
    _assert_decimals_are_json_strings(instance, payload)


def test_float_amount_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="float is not allowed for monetary values"
    ):
        Transaction(
            transaction_id="tx-1",
            vendor_name_raw="Acme LLC",
            amount=1.25,
            line_count=1,
            distinct_line_amounts=[],
        )


def test_float_in_distinct_line_amounts_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="float is not allowed for monetary values"
    ):
        Transaction(
            transaction_id="tx-1",
            vendor_name_raw="Acme LLC",
            amount=Decimal("10.00"),
            line_count=1,
            distinct_line_amounts=[1.0],
        )


def test_float_amount_at_risk_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="float is not allowed for monetary values"
    ):
        Candidate(
            candidate_id="cand-1",
            transaction_ids=("tx-1", "tx-2"),
            signal=Signal.exact_duplicate,
            amount_at_risk=99.99,
            stage_generated="blocking",
        )


def test_string_and_int_money_are_accepted() -> None:
    tx = Transaction(
        transaction_id="tx-1",
        vendor_name_raw="Acme LLC",
        amount="10.25",
        invoice_amount=10,
        line_count=1,
        distinct_line_amounts=["1.00", 2],
    )
    assert tx.amount == Decimal("10.25")
    assert tx.invoice_amount == Decimal(10)
    assert tx.distinct_line_amounts == [Decimal("1.00"), Decimal(2)]


def test_signal_members_match_spec() -> None:
    assert {member.value for member in Signal} == {
        "exact_duplicate",
        "fuzzy_vendor",
        "invoice_variant",
        "split_payment",
        "cross_department",
        "overpayment_vs_invoice",
    }


def test_verdict_outcome_members_match_spec() -> None:
    assert {member.value for member in VerdictOutcome} == {
        "dismiss",
        "escalate",
        "errored",
    }


def test_explanation_type_members_match_spec() -> None:
    assert {member.value for member in ExplanationType} == {
        "cancelled",
        "line_split",
        "progress_payment",
        "recurring",
        "different_po",
        "partial_pair",
        "none",
    }


def test_unknown_signal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            candidate_id="cand-1",
            transaction_ids=("tx-1", "tx-2"),
            signal="not_a_signal",  # type: ignore[arg-type]
            amount_at_risk=Decimal("1.00"),
            stage_generated="blocking",
        )


def test_candidate_requires_two_transaction_ids() -> None:
    with pytest.raises(ValidationError):
        Candidate(
            candidate_id="cand-1",
            transaction_ids=("tx-1",),  # type: ignore[arg-type]
            signal=Signal.exact_duplicate,
            amount_at_risk=Decimal("1.00"),
            stage_generated="blocking",
        )
