from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from agents.adjudicate import (
    AdjudicationError,
    AdjudicationSummary,
    adjudicate,
    is_unmatched_recording,
    summarise_adjudication,
)
from agents.cassettes import CassetteError, save_cassette
from agents.llm import ModelClient, ModelError, build_request
from agents.prompts import STABLE_INSTRUCTIONS, build_messages
from core.models import (
    Candidate,
    ExplanationType,
    Signal,
    Transaction,
    Verdict,
    VerdictOutcome,
)
from core.settings import Settings

FAST_MODEL = "fast-model"
ESC_MODEL = "esc-model"
TIMING_CANDIDATES = 300
TIMING_BUDGET_SECONDS = 90.0

DISMISSAL_TYPES: tuple[ExplanationType, ...] = (
    ExplanationType.cancelled,
    ExplanationType.line_split,
    ExplanationType.progress_payment,
    ExplanationType.recurring,
    ExplanationType.different_po,
    ExplanationType.partial_pair,
)


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "model_fast": FAST_MODEL,
        "model_escalate": ESC_MODEL,
        "concurrency_limit": 16,
        "escalation_confidence_threshold": 0.7,
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def _live_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "api_key": "test-key",
        "api_base": "https://example.test/v1",
        "model_fast": FAST_MODEL,
        "model_escalate": ESC_MODEL,
        "concurrency_limit": 16,
        "escalation_confidence_threshold": 0.7,
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


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
    payment_date: date | None = date(2024, 6, 17),
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


def _judgement(
    candidate_id: str,
    *,
    verdict: str = "dismiss",
    explanation_type: str = "cancelled",
    reasoning: str = "explained",
    evidence: list[dict[str, object]] | None = None,
    confidence: float = 0.91,
) -> dict[str, object]:
    rows = (
        evidence
        if evidence is not None
        else [{"field": "description", "values": ["left", "right"]}]
    )
    return {
        "candidate_id": candidate_id,
        "confidence": confidence,
        "evidence": rows,
        "explanation_type": explanation_type,
        "reasoning": reasoning,
        "verdict": verdict,
    }


def _completion_body(
    judgement: Mapping[str, object] | None = None,
    *,
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cached_tokens: int = 4,
    raw_content: str | None = None,
) -> dict[str, object]:
    if raw_content is not None:
        content = raw_content
    else:
        assert judgement is not None
        content = json.dumps(judgement)
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {"content": content, "role": "assistant"},
            }
        ],
        "usage": {
            "completion_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


def _record(
    cassette_dir: Path,
    messages: Sequence[Mapping[str, str]],
    model: str,
    judgement: Mapping[str, object] | None = None,
    *,
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cached_tokens: int = 4,
    raw_content: str | None = None,
) -> None:
    save_cassette(
        cassette_dir,
        build_request(model=model, messages=messages),
        _completion_body(
            judgement,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            raw_content=raw_content,
        ),
    )


def _case(
    explanation: ExplanationType,
) -> tuple[Candidate, Transaction, Transaction, tuple[Transaction, ...]]:
    if explanation is ExplanationType.cancelled:
        left = _tx("tx-canc-a", "3450.00", payment_status="PAID")
        right = _tx(
            "tx-canc-b",
            "-3450.00",
            payment_status="PAID",
            description="Cancelled duplicate entry",
        )
        candidate = _candidate(
            "cand-cancelled", "tx-canc-a", "tx-canc-b", amount="3450.00"
        )
        return candidate, left, right, ()
    if explanation is ExplanationType.line_split:
        left = _tx(
            "tx-split-a",
            "91.21",
            line_count=3,
            distinct_line_amounts=[
                Decimal("12.00"),
                Decimal("30.00"),
                Decimal("49.21"),
            ],
        )
        right = _tx(
            "tx-split-b",
            "1383.53",
            line_count=6,
            distinct_line_amounts=[
                Decimal("100.00"),
                Decimal("200.00"),
                Decimal("1083.53"),
            ],
        )
        candidate = _candidate(
            "cand-line-split",
            "tx-split-a",
            "tx-split-b",
            signal=Signal.split_payment,
            amount="91.21",
        )
        return candidate, left, right, ()
    if explanation is ExplanationType.progress_payment:
        left = _tx(
            "tx-prog-2",
            "25000.00",
            invoice_number_raw="INV-D2",
            invoice_canonical="D2",
            invoice_date=date(2024, 2, 1),
            payment_date=date(2024, 2, 15),
            description="Progress payment 2 of 5",
        )
        right = _tx(
            "tx-prog-3",
            "25000.00",
            invoice_number_raw="INV-D3",
            invoice_canonical="D3",
            invoice_date=date(2024, 3, 1),
            payment_date=date(2024, 3, 15),
            description="Progress payment 3 of 5",
        )
        candidate = _candidate(
            "cand-progress",
            "tx-prog-2",
            "tx-prog-3",
            amount="25000.00",
        )
        return candidate, left, right, ()
    if explanation is ExplanationType.recurring:
        months = [
            _tx(
                f"tx-rec-{month}",
                "1800.00",
                invoice_number_raw=f"INV-M{month}",
                invoice_canonical=f"M{month}",
                invoice_date=date(2018, month, 15),
                payment_date=date(2018, month, 20),
                description="Professional services",
            )
            for month in range(1, 7)
        ]
        candidate = _candidate(
            "cand-recurring",
            "tx-rec-2",
            "tx-rec-3",
            amount="1800.00",
        )
        related = tuple(
            row for row in months if row.transaction_id not in {"tx-rec-2", "tx-rec-3"}
        )
        return candidate, months[1], months[2], related
    if explanation is ExplanationType.different_po:
        left = _tx("tx-po-a", "440.00", po_number="EVALPO00012")
        right = _tx("tx-po-b", "440.00", po_number="EVALPO00013")
        candidate = _candidate(
            "cand-different-po", "tx-po-a", "tx-po-b", amount="440.00"
        )
        return candidate, left, right, ()
    if explanation is ExplanationType.partial_pair:
        left = _tx(
            "tx-part-a",
            "250.00",
            description="Partial payment, balance outstanding",
        )
        right = _tx(
            "tx-part-b",
            "750.00",
            payment_date=date(2024, 7, 17),
            description="Partial payment 2, remaining balance",
        )
        candidate = _candidate(
            "cand-partial",
            "tx-part-a",
            "tx-part-b",
            signal=Signal.split_payment,
            amount="250.00",
        )
        return candidate, left, right, ()
    raise AssertionError(f"unexpected explanation {explanation}")


def _evidence_for(explanation: ExplanationType) -> list[dict[str, object]]:
    field = {
        ExplanationType.cancelled: "description",
        ExplanationType.line_split: "line_count",
        ExplanationType.progress_payment: "description",
        ExplanationType.recurring: "invoice_date",
        ExplanationType.different_po: "po_number",
        ExplanationType.partial_pair: "description",
    }[explanation]
    return [{"field": field, "values": ["left", "right"]}]


async def _run_recorded(
    tmp_path: Path,
    candidate: Candidate,
    left: Transaction,
    right: Transaction,
    judgement: Mapping[str, object],
    *,
    related: Sequence[Transaction] = (),
    context: Mapping[str, str] | None = None,
    settings: Settings | None = None,
    model: str = FAST_MODEL,
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cached_tokens: int = 4,
) -> list[Verdict]:
    resolved = settings if settings is not None else _settings()
    note = None if context is None else context.get(candidate.candidate_id)
    messages = build_messages(candidate, left, right, related=related, context=note)
    _record(
        tmp_path,
        messages,
        model,
        judgement,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
    )
    related_map = {candidate.candidate_id: related} if related else None
    transactions = [left, right, *related]
    return await adjudicate(
        [candidate],
        transactions,
        resolved,
        related=related_map,
        context=context,
        cassette_dir=tmp_path,
    )


def _flatten_rows(
    rows: Sequence[tuple[Candidate, Transaction, Transaction, tuple[Transaction, ...]]],
) -> tuple[list[Candidate], list[Transaction]]:
    candidates = [item[0] for item in rows]
    transactions: list[Transaction] = []
    for _candidate, left, right, related in rows:
        transactions.extend((left, right, *related))
    return candidates, transactions


def _assert_summary_invariants(summary: AdjudicationSummary) -> None:
    assert summary.rows_in == (
        summary.dismissed
        + summary.escalated
        + summary.errored
        + summary.unmatched_recordings
    )
    assert summary.passed_through is (
        summary.dismissed == 0
        and summary.errored == 0
        and summary.unmatched_recordings == 0
    )


def test_stable_instructions_include_invoice_sum_test() -> None:
    assert "sum to the invoice amount" in STABLE_INSTRUCTIONS


def test_prompt_prefix_is_stable_and_payload_is_last() -> None:
    first = _case(ExplanationType.cancelled)
    second = _case(ExplanationType.recurring)
    messages_a = build_messages(first[0], first[1], first[2], related=first[3])
    messages_b = build_messages(second[0], second[1], second[2], related=second[3])
    assert [item["role"] for item in messages_a] == ["system", "user"]
    assert messages_a[0]["content"] == STABLE_INSTRUCTIONS
    assert messages_a[0] == messages_b[0]
    assert messages_a[-1]["role"] == "user"
    assert messages_a[-1]["content"] != messages_b[-1]["content"]
    assert first[0].candidate_id in messages_a[-1]["content"]
    assert "related" in messages_b[-1]["content"]
    payload: object = json.loads(messages_b[-1]["content"])
    assert isinstance(payload, dict)
    related_payload = payload["related"]
    assert isinstance(related_payload, list)
    assert len(related_payload) == len(second[3])


@pytest.mark.parametrize("explanation", DISMISSAL_TYPES)
async def test_dismissal_for_each_explanation_type(
    tmp_path: Path, explanation: ExplanationType
) -> None:
    candidate, left, right, related = _case(explanation)
    judgement = _judgement(
        candidate.candidate_id,
        verdict="dismiss",
        explanation_type=explanation.value,
        reasoning=f"{explanation.value} is a legitimate explanation",
        evidence=_evidence_for(explanation),
        confidence=0.94,
    )
    verdicts = await _run_recorded(
        tmp_path, candidate, left, right, judgement, related=related
    )
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.candidate_id == candidate.candidate_id
    assert verdict.verdict is VerdictOutcome.dismiss
    assert verdict.explanation_type is explanation
    assert verdict.model_used == FAST_MODEL
    assert verdict.tokens_in == 12
    assert verdict.tokens_out == 7
    assert verdict.cache_read == 4
    assert verdict.confidence == 0.94
    assert verdict.evidence[0].field == _evidence_for(explanation)[0]["field"]


async def test_low_confidence_is_escalated_to_stronger_model(tmp_path: Path) -> None:
    candidate, left, right, related = _case(ExplanationType.progress_payment)
    messages = build_messages(candidate, left, right, related=related)
    _record(
        tmp_path,
        messages,
        FAST_MODEL,
        _judgement(
            candidate.candidate_id,
            verdict="escalate",
            explanation_type="none",
            reasoning="fast-unsure",
            confidence=0.41,
        ),
        prompt_tokens=11,
        completion_tokens=5,
        cached_tokens=3,
    )
    _record(
        tmp_path,
        messages,
        ESC_MODEL,
        _judgement(
            candidate.candidate_id,
            verdict="dismiss",
            explanation_type="progress_payment",
            reasoning="esc-progress",
            evidence=_evidence_for(ExplanationType.progress_payment),
            confidence=0.93,
        ),
        prompt_tokens=22,
        completion_tokens=8,
        cached_tokens=6,
    )
    verdicts = await adjudicate(
        [candidate],
        [left, right, *related],
        _settings(),
        cassette_dir=tmp_path,
    )
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.verdict is VerdictOutcome.dismiss
    assert verdict.explanation_type is ExplanationType.progress_payment
    assert verdict.reasoning == "esc-progress"
    assert verdict.model_used == ESC_MODEL
    assert verdict.tokens_in == 33
    assert verdict.tokens_out == 13
    assert verdict.cache_read == 9


async def test_confidence_at_threshold_stays_on_fast_model(tmp_path: Path) -> None:
    candidate, left, right, related = _case(ExplanationType.different_po)
    judgement = _judgement(
        candidate.candidate_id,
        verdict="dismiss",
        explanation_type="different_po",
        reasoning="fast-threshold",
        evidence=_evidence_for(ExplanationType.different_po),
        confidence=0.7,
    )
    verdicts = await _run_recorded(
        tmp_path, candidate, left, right, judgement, related=related
    )
    assert verdicts[0].model_used == FAST_MODEL
    assert verdicts[0].reasoning == "fast-threshold"
    assert verdicts[0].verdict is VerdictOutcome.dismiss


async def test_recoverable_duplicate_is_escalated_to_human(tmp_path: Path) -> None:
    left = _tx("tx-dup-a", "1250.50", invoice_number_raw="INV-100")
    right = _tx("tx-dup-b", "1250.50", invoice_number_raw="INV-100")
    candidate = _candidate("cand-dup", "tx-dup-a", "tx-dup-b", amount="1250.50")
    judgement = _judgement(
        candidate.candidate_id,
        verdict="escalate",
        explanation_type="none",
        reasoning="same invoice paid twice",
        evidence=[{"field": "invoice_canonical", "values": ["100", "100"]}],
        confidence=0.88,
    )
    verdicts = await _run_recorded(tmp_path, candidate, left, right, judgement)
    verdict = verdicts[0]
    assert verdict.verdict is VerdictOutcome.escalate
    assert verdict.explanation_type is ExplanationType.none
    assert verdict.model_used == FAST_MODEL


async def test_schema_failure_becomes_errored(tmp_path: Path) -> None:
    candidate, left, right, related = _case(ExplanationType.cancelled)
    messages = build_messages(candidate, left, right, related=related)
    _record(
        tmp_path,
        messages,
        FAST_MODEL,
        None,
        raw_content=json.dumps({"verdict": "dismiss"}),
    )
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        cassette_dir=tmp_path,
    )
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.candidate_id == candidate.candidate_id
    assert verdict.verdict is VerdictOutcome.errored
    assert verdict.explanation_type is ExplanationType.none
    assert "errored; do not guess" in verdict.reasoning
    assert verdict.model_used == FAST_MODEL


async def test_missing_cassette_marks_errored_and_stays_in_results(
    tmp_path: Path,
) -> None:
    candidate, left, right, _related = _case(ExplanationType.cancelled)
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        cassette_dir=tmp_path,
    )
    assert len(verdicts) == 1
    assert verdicts[0].verdict is VerdictOutcome.errored
    assert verdicts[0].reasoning.startswith("no recording:")
    assert "no cassette" in verdicts[0].reasoning


async def test_missing_cassette_counts_as_unmatched_not_genuine_errored(
    tmp_path: Path,
) -> None:
    candidate, left, right, _related = _case(ExplanationType.cancelled)
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        cassette_dir=tmp_path,
    )
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.rows_in == 1
    assert summary.unmatched_recordings == 1
    assert summary.errored == 0
    assert summary.passed_through is False
    assert is_unmatched_recording(verdicts[0]) is True


async def test_schema_failure_counts_as_genuine_errored_not_unmatched(
    tmp_path: Path,
) -> None:
    candidate, left, right, related = _case(ExplanationType.cancelled)
    messages = build_messages(candidate, left, right, related=related)
    _record(
        tmp_path,
        messages,
        FAST_MODEL,
        None,
        raw_content=json.dumps({"verdict": "dismiss"}),
    )
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        cassette_dir=tmp_path,
    )
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.rows_in == 1
    assert summary.errored == 1
    assert summary.unmatched_recordings == 0
    assert summary.passed_through is False
    assert is_unmatched_recording(verdicts[0]) is False
    assert not verdicts[0].reasoning.startswith("no recording:")


async def test_mixed_batch_four_outcomes_sum_to_rows_in(tmp_path: Path) -> None:
    dismiss_row = _case(ExplanationType.cancelled)
    escalate_row = _case(ExplanationType.different_po)
    schema_row = _case(ExplanationType.partial_pair)
    miss_row = _case(ExplanationType.progress_payment)
    _record(
        tmp_path,
        build_messages(
            dismiss_row[0], dismiss_row[1], dismiss_row[2], related=dismiss_row[3]
        ),
        FAST_MODEL,
        _judgement(
            dismiss_row[0].candidate_id,
            verdict="dismiss",
            explanation_type="cancelled",
            confidence=0.91,
        ),
    )
    _record(
        tmp_path,
        build_messages(
            escalate_row[0],
            escalate_row[1],
            escalate_row[2],
            related=escalate_row[3],
        ),
        FAST_MODEL,
        _judgement(
            escalate_row[0].candidate_id,
            verdict="escalate",
            explanation_type="none",
            confidence=0.88,
        ),
    )
    _record(
        tmp_path,
        build_messages(
            schema_row[0], schema_row[1], schema_row[2], related=schema_row[3]
        ),
        FAST_MODEL,
        None,
        raw_content=json.dumps({"verdict": "dismiss"}),
    )
    rows = (dismiss_row, escalate_row, schema_row, miss_row)
    candidates, transactions = _flatten_rows(rows)
    verdicts = await adjudicate(
        candidates, transactions, _settings(), cassette_dir=tmp_path
    )
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.rows_in == 4
    assert summary.dismissed == 1
    assert summary.escalated == 1
    assert summary.errored == 1
    assert summary.unmatched_recordings == 1
    assert (
        summary.dismissed
        + summary.escalated
        + summary.errored
        + summary.unmatched_recordings
        == 4
    )
    assert summary.passed_through is False
    by_id = {item.candidate_id: item for item in verdicts}
    assert by_id[dismiss_row[0].candidate_id].verdict is VerdictOutcome.dismiss
    assert by_id[escalate_row[0].candidate_id].verdict is VerdictOutcome.escalate
    assert by_id[schema_row[0].candidate_id].verdict is VerdictOutcome.errored
    assert is_unmatched_recording(by_id[schema_row[0].candidate_id]) is False
    assert is_unmatched_recording(by_id[miss_row[0].candidate_id]) is True


async def test_all_escalate_is_passed_through(tmp_path: Path) -> None:
    rows = (
        _case(ExplanationType.cancelled),
        _case(ExplanationType.different_po),
        _case(ExplanationType.partial_pair),
    )
    for candidate, left, right, related in rows:
        _record(
            tmp_path,
            build_messages(candidate, left, right, related=related),
            FAST_MODEL,
            _judgement(
                candidate.candidate_id,
                verdict="escalate",
                explanation_type="none",
                confidence=0.88,
            ),
        )
    candidates, transactions = _flatten_rows(rows)
    verdicts = await adjudicate(
        candidates, transactions, _settings(), cassette_dir=tmp_path
    )
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.rows_in == 3
    assert summary.escalated == 3
    assert summary.dismissed == 0
    assert summary.errored == 0
    assert summary.unmatched_recordings == 0
    assert summary.passed_through is True
    assert all(item.verdict is VerdictOutcome.escalate for item in verdicts)


async def test_all_missing_cassette_is_not_passed_through(tmp_path: Path) -> None:
    rows = (
        _case(ExplanationType.cancelled),
        _case(ExplanationType.different_po),
        _case(ExplanationType.partial_pair),
    )
    candidates, transactions = _flatten_rows(rows)
    verdicts = await adjudicate(
        candidates, transactions, _settings(), cassette_dir=tmp_path
    )
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.rows_in == len(verdicts)
    assert summary.unmatched_recordings == summary.rows_in
    assert summary.errored == 0
    assert summary.dismissed == 0
    assert summary.escalated == 0
    assert summary.passed_through is False
    assert all(item.verdict is VerdictOutcome.errored for item in verdicts)
    assert all(is_unmatched_recording(item) for item in verdicts)


async def test_replay_miss_type_name_is_unmatched_without_importing_class() -> None:
    class ReplayMissError(ModelError):
        pass

    class _Client:
        async def complete(self, *_args: object, **_kwargs: object) -> object:
            raise ReplayMissError("payload hash missed the cassette store")

    candidate, left, right, _related = _case(ExplanationType.cancelled)
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        client=_Client(),  # type: ignore[arg-type]
    )
    assert len(verdicts) == 1
    assert verdicts[0].verdict is VerdictOutcome.errored
    assert verdicts[0].reasoning.startswith("no recording:")
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.unmatched_recordings == 1
    assert summary.errored == 0
    assert summary.passed_through is False


async def test_cassette_error_cause_is_unmatched_without_no_cassette_text() -> None:
    class _Client:
        async def complete(self, *_args: object, **_kwargs: object) -> object:
            try:
                raise CassetteError("hashed request does not match stored cassette")
            except CassetteError as exc:
                raise ModelError("replay failed") from exc

    candidate, left, right, _related = _case(ExplanationType.cancelled)
    verdicts = await adjudicate(
        [candidate],
        [left, right],
        _settings(),
        client=_Client(),  # type: ignore[arg-type]
    )
    assert verdicts[0].verdict is VerdictOutcome.errored
    assert is_unmatched_recording(verdicts[0]) is True
    assert verdicts[0].reasoning.startswith("no recording:")
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.unmatched_recordings == 1
    assert summary.errored == 0


async def test_missing_transaction_marks_errored(tmp_path: Path) -> None:
    candidate, left, _right, _related = _case(ExplanationType.cancelled)
    verdicts = await adjudicate(
        [candidate],
        [left],
        _settings(),
        cassette_dir=tmp_path,
    )
    assert verdicts[0].verdict is VerdictOutcome.errored
    assert "missing" in verdicts[0].reasoning
    assert not verdicts[0].reasoning.startswith("no recording:")
    assert is_unmatched_recording(verdicts[0]) is False
    summary = summarise_adjudication(verdicts)
    _assert_summary_invariants(summary)
    assert summary.errored == 1
    assert summary.unmatched_recordings == 0


async def test_results_are_sorted_by_candidate_id(tmp_path: Path) -> None:
    rows = [
        _case(ExplanationType.cancelled),
        _case(ExplanationType.different_po),
        _case(ExplanationType.partial_pair),
    ]
    candidates = [item[0] for item in rows]
    transactions = [tx for item in rows for tx in (item[1], item[2], *item[3])]
    for candidate, left, right, related in rows:
        messages = build_messages(candidate, left, right, related=related)
        _record(
            tmp_path,
            messages,
            FAST_MODEL,
            _judgement(
                candidate.candidate_id,
                verdict="dismiss",
                explanation_type="cancelled",
                confidence=0.9,
            ),
        )
    shuffled = list(reversed(candidates))
    verdicts = await adjudicate(
        shuffled, transactions, _settings(), cassette_dir=tmp_path
    )
    ids = [item.candidate_id for item in verdicts]
    assert ids == sorted(ids)
    assert ids != [item.candidate_id for item in shuffled]


async def test_three_hundred_candidates_complete_under_budget(tmp_path: Path) -> None:
    candidates: list[Candidate] = []
    transactions: list[Transaction] = []
    for index in range(TIMING_CANDIDATES):
        left = _tx(f"tx-{index:03d}-a", "10.00", description=f"row-{index:03d}-a")
        right = _tx(f"tx-{index:03d}-b", "10.00", description=f"row-{index:03d}-b")
        candidate = _candidate(
            f"cand-{index:03d}",
            left.transaction_id,
            right.transaction_id,
            amount="10.00",
        )
        candidates.append(candidate)
        transactions.extend((left, right))
        messages = build_messages(candidate, left, right)
        _record(
            tmp_path,
            messages,
            FAST_MODEL,
            _judgement(
                candidate.candidate_id,
                verdict="dismiss",
                explanation_type="recurring",
                reasoning="monthly series",
                confidence=0.95,
            ),
        )
    rng = random.Random(0)
    rng.shuffle(candidates)
    started = time.perf_counter()
    verdicts = await adjudicate(
        candidates,
        transactions,
        _settings(concurrency_limit=16),
        cassette_dir=tmp_path,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < TIMING_BUDGET_SECONDS
    assert len(verdicts) == TIMING_CANDIDATES
    assert [item.candidate_id for item in verdicts] == sorted(
        item.candidate_id for item in verdicts
    )
    assert all(item.verdict is VerdictOutcome.dismiss for item in verdicts)
    assert all(item.model_used == FAST_MODEL for item in verdicts)


async def test_in_flight_candidates_never_exceed_concurrency(tmp_path: Path) -> None:
    limit = 4
    current = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                json=_completion_body(
                    _judgement(
                        "x",
                        verdict="dismiss",
                        explanation_type="recurring",
                        confidence=0.9,
                    )
                ),
            )
        finally:
            current -= 1

    candidates: list[Candidate] = []
    transactions: list[Transaction] = []
    for index in range(12):
        left = _tx(f"tx-c-{index}-a", "10.00")
        right = _tx(f"tx-c-{index}-b", "10.00")
        candidate = _candidate(
            f"cand-c-{index}",
            left.transaction_id,
            right.transaction_id,
            amount="10.00",
        )
        candidates.append(candidate)
        transactions.extend((left, right))

    settings = _live_settings(concurrency_limit=limit)
    async with ModelClient(
        settings,
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        verdicts = await adjudicate(candidates, transactions, settings, client=client)

    assert peak <= limit
    assert peak == limit
    assert len(verdicts) == 12
    assert all(item.verdict is VerdictOutcome.dismiss for item in verdicts)


async def test_empty_input_returns_empty_list(tmp_path: Path) -> None:
    verdicts = await adjudicate([], [], _settings(), cassette_dir=tmp_path)
    assert verdicts == []


async def test_concurrency_limit_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(AdjudicationError, match="positive integer"):
        await adjudicate([], [], _settings(concurrency_limit=0), cassette_dir=tmp_path)
