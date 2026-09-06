"""Dismiss candidate pairs that a deterministic rule can prove are not recoverable.

Stage 4 of the pipeline. Blocking emits pairs that look like duplicates.
Most of those pairs are cancelled payments, the same transaction listed
twice, or distribution lines of one invoice. This stage removes those
before any model call, and writes a Verdict for each dismissal so the
audit trail records why.

Rules fire only when they are certain. Anything a description, a PO, or
a payment schedule could still explain is left for stage 5. Dismissing a
genuine duplicate is the failure mode this file is built to avoid.

Complexity: O(T + C) for T transactions and C candidates. Transactions
are hashed by id in one pass, O(T). Each candidate is then a constant-time
lookup of two ids plus a fixed set of field comparisons. Candidates and
verdicts are sorted by candidate_id in O(C log C) so output does not
depend on input order. Extra memory is O(T + C).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal

from .models import (
    Candidate,
    Evidence,
    ExplanationType,
    Transaction,
    Verdict,
    VerdictOutcome,
)

log = logging.getLogger("core.dismiss")

_CANCELLED_STATUSES = frozenset(
    {
        "CANCELLED",
        "CANCELED",
        "REVERSED",
        "REVERSAL",
        "VOID",
        "VOIDED",
    }
)
_MIN_DISTRIBUTION_LINES = 2
_DETERMINISTIC = "deterministic"
_RULE_CONFIDENCE = 1.0


def dismiss(
    candidates: Iterable[Candidate],
    transactions: Iterable[Transaction],
    *,
    run_id: str | None = None,
) -> tuple[list[Candidate], list[Verdict]]:
    """Return residual candidates and a Verdict for each dismissed pair."""
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info("dismiss start run_id=%s", active_run_id)

    index = _index_transactions(transactions)
    residual: list[Candidate] = []
    verdicts: list[Verdict] = []
    rows_in = 0
    for candidate in candidates:
        rows_in += 1
        verdict = _verdict_for(candidate, index)
        if verdict is None:
            residual.append(candidate)
        else:
            verdicts.append(verdict)

    residual.sort(key=lambda item: item.candidate_id)
    verdicts.sort(key=lambda item: item.candidate_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "dismiss done rows_in=%s rows_out=%s dismissed=%s elapsed_ms=%s run_id=%s",
        rows_in,
        len(residual),
        len(verdicts),
        elapsed_ms,
        active_run_id,
    )
    return residual, verdicts


def _index_transactions(transactions: Iterable[Transaction]) -> dict[str, Transaction]:
    index: dict[str, Transaction] = {}
    for transaction in transactions:
        if transaction.transaction_id not in index:
            index[transaction.transaction_id] = transaction
    return index


def _verdict_for(
    candidate: Candidate, index: Mapping[str, Transaction]
) -> Verdict | None:
    if _same_transaction_id(candidate):
        return _make_verdict(
            candidate,
            explanation_type=ExplanationType.none,
            reasoning="Both sides are the same transaction_id, so this is not a pair.",
            evidence=[
                Evidence(
                    field="transaction_id",
                    values=[candidate.transaction_ids[0], candidate.transaction_ids[1]],
                )
            ],
        )

    pair = _lookup_pair(candidate, index)
    if pair is None:
        return None
    left, right = pair

    if _is_cancelled(left) or _is_cancelled(right):
        return _make_verdict(
            candidate,
            explanation_type=ExplanationType.cancelled,
            reasoning="payment_status shows a cancelled or reversed payment.",
            evidence=[
                Evidence(
                    field="payment_status",
                    values=[
                        _status_or_empty(left.payment_status),
                        _status_or_empty(right.payment_status),
                    ],
                )
            ],
        )

    if _is_line_split(left, right):
        return _make_verdict(
            candidate,
            explanation_type=ExplanationType.line_split,
            reasoning=(
                "line_count and distinct_line_amounts show distribution "
                "lines of one invoice rather than two payments."
            ),
            evidence=[
                Evidence(
                    field="line_count",
                    values=[str(left.line_count), str(right.line_count)],
                ),
                Evidence(
                    field="distinct_line_amounts",
                    values=[
                        _format_amounts(left.distinct_line_amounts),
                        _format_amounts(right.distinct_line_amounts),
                    ],
                ),
            ],
        )
    return None


def _same_transaction_id(candidate: Candidate) -> bool:
    left_id, right_id = candidate.transaction_ids
    return left_id != "" and left_id == right_id


def _lookup_pair(
    candidate: Candidate, index: Mapping[str, Transaction]
) -> tuple[Transaction, Transaction] | None:
    left_id, right_id = candidate.transaction_ids
    left = index.get(left_id)
    right = index.get(right_id)
    if left is None or right is None:
        return None
    return left, right


def _is_cancelled(transaction: Transaction) -> bool:
    status = transaction.payment_status
    if status is None:
        return False
    return _normalise_status(status) in _CANCELLED_STATUSES


def _normalise_status(status: str) -> str:
    cleaned = status.strip().upper().replace("-", " ").replace("_", " ")
    return " ".join(cleaned.split())


def _status_or_empty(status: str | None) -> str:
    return status if status is not None else ""


def _is_line_split(left: Transaction, right: Transaction) -> bool:
    """Return True only when GL line fields prove one invoice, not two payments."""
    if left.transaction_id == right.transaction_id:
        return False
    if not _same_invoice(left, right):
        return False
    if not _same_vendor(left, right):
        return False
    if left.amount == right.amount:
        return False
    if (
        left.line_count < _MIN_DISTRIBUTION_LINES
        or right.line_count < _MIN_DISTRIBUTION_LINES
    ):
        return False
    if not left.distinct_line_amounts or not right.distinct_line_amounts:
        return False
    if _amount_key(left.distinct_line_amounts) == _amount_key(
        right.distinct_line_amounts
    ):
        return False
    if not _same_optional_date(left.payment_date, right.payment_date):
        return False
    if _conflict(left.payment_method, right.payment_method):
        return False
    return not _conflict(left.po_number, right.po_number)


def _same_invoice(left: Transaction, right: Transaction) -> bool:
    if left.invoice_canonical is not None and right.invoice_canonical is not None:
        return (
            left.invoice_canonical != ""
            and left.invoice_canonical == right.invoice_canonical
        )
    if left.invoice_number_raw is not None and right.invoice_number_raw is not None:
        raw_left = left.invoice_number_raw.strip()
        raw_right = right.invoice_number_raw.strip()
        return raw_left != "" and raw_left == raw_right
    return False


def _same_vendor(left: Transaction, right: Transaction) -> bool:
    if left.vendor_id is not None and right.vendor_id is not None:
        return left.vendor_id == right.vendor_id
    if left.vendor_canonical is not None and right.vendor_canonical is not None:
        return left.vendor_canonical == right.vendor_canonical
    raw_left = left.vendor_name_raw.strip()
    raw_right = right.vendor_name_raw.strip()
    return raw_left != "" and raw_left == raw_right


def _same_optional_date(left: date | None, right: date | None) -> bool:
    return left is not None and right is not None and left == right


def _conflict(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and left != right


def _amount_key(values: list[Decimal]) -> tuple[Decimal, ...]:
    return tuple(sorted(values))


def _format_amounts(values: list[Decimal]) -> str:
    return ",".join(str(value) for value in values)


def _make_verdict(
    candidate: Candidate,
    *,
    explanation_type: ExplanationType,
    reasoning: str,
    evidence: list[Evidence],
) -> Verdict:
    return Verdict(
        candidate_id=candidate.candidate_id,
        verdict=VerdictOutcome.dismiss,
        explanation_type=explanation_type,
        reasoning=reasoning,
        evidence=evidence,
        confidence=_RULE_CONFIDENCE,
        model_used=_DETERMINISTIC,
        tokens_in=0,
        tokens_out=0,
        cache_read=0,
    )
