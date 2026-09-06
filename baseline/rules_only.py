"""Rules-only duplicate-payment pipeline for evaluation comparison.

This is the honest strawman: stages 1 to 4 of the full pipeline, then
stop. Every candidate that survives deterministic dismissal is reported
as a finding, because a rules engine has no way to weigh a judgement
call. There is no stage 5 and there are no model calls.

Stages 1 to 4 are the same functions the full pipeline uses
(``core.aggregate.aggregate``, ``core.normalize``, ``core.blocking.block``,
``core.dismiss.dismiss``). The only difference is adjudication: residual
candidates become ``verdict=escalate`` with ``model_used=None`` and
``confidence=None``. Stage 4 dismissals pass through unchanged so a
cancelled payment or a proven line-split is not a finding here either.

Complexity: O(R + C log C) for R input rows and C candidates. Aggregate,
normalise, and block are each linear in R with a constant bucket cap;
dismiss is linear in C after an O(R) index; sorting the combined
verdicts is O(C log C). Extra memory is O(R + C). No model calls, so
there is no per-candidate network term.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal

from core.aggregate import aggregate
from core.blocking import BlockingCapabilities, block
from core.dismiss import dismiss
from core.models import (
    Candidate,
    Evidence,
    ExplanationType,
    Transaction,
    Verdict,
    VerdictOutcome,
)
from core.normalize import canonical_invoice, canonical_vendor
from core.settings import Settings

log = logging.getLogger("baseline.rules_only")

_FINDING_REASONING = (
    "Survived deterministic dismissal (cancelled, same-transaction, "
    "line-split). A rules engine cannot weigh this judgement call, so "
    "the pair is reported as a finding."
)


def run_rules_only(
    rows: Iterable[Transaction],
    capabilities: BlockingCapabilities,
    settings: Settings | None = None,
    *,
    run_id: str | None = None,
) -> list[Verdict]:
    """Run stages 1 to 4 and escalate every residual candidate.

    Returns one ``Verdict`` per candidate, sorted by ``candidate_id``.
    Residual findings use the shared Verdict schema with ``model_used``
    and ``confidence`` left unset. Settings that name a model or an API
    key are ignored; this function never calls a model.
    """
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    cfg = Settings() if settings is None else settings
    started = time.perf_counter()
    log.info(
        "rules_only start run_id=%s blocking_bucket_cap=%s mode=rules_only",
        active_run_id,
        cfg.blocking_bucket_cap,
    )

    aggregated = aggregate(rows, run_id=active_run_id)
    normalised = _normalise_transactions(aggregated, run_id=active_run_id)
    candidates = block(normalised, capabilities, cfg, run_id=active_run_id)
    residual, dismissed = dismiss(candidates, normalised, run_id=active_run_id)

    index = _index_transactions(normalised)
    findings = [_finding_verdict(candidate, index) for candidate in residual]
    verdicts = dismissed + findings
    verdicts.sort(key=lambda item: item.candidate_id)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "rules_only done rows_in=%s candidates=%s residual=%s dismissed=%s "
        "rows_out=%s elapsed_ms=%s run_id=%s",
        len(aggregated),
        len(candidates),
        len(residual),
        len(dismissed),
        len(verdicts),
        elapsed_ms,
        active_run_id,
    )
    return verdicts


def _normalise_transactions(
    transactions: Sequence[Transaction], *, run_id: str
) -> list[Transaction]:
    started = time.perf_counter()
    log.info("normalize start run_id=%s", run_id)
    result = [_normalise_one(item) for item in transactions]
    result.sort(key=lambda item: item.transaction_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "normalize done rows_in=%s rows_out=%s elapsed_ms=%s run_id=%s",
        len(transactions),
        len(result),
        elapsed_ms,
        run_id,
    )
    return result


def _normalise_one(transaction: Transaction) -> Transaction:
    return transaction.model_copy(
        update={
            "vendor_canonical": canonical_vendor(transaction.vendor_name_raw),
            "invoice_canonical": canonical_invoice(transaction.invoice_number_raw),
        }
    )


def _index_transactions(transactions: Iterable[Transaction]) -> dict[str, Transaction]:
    index: dict[str, Transaction] = {}
    for transaction in transactions:
        if transaction.transaction_id not in index:
            index[transaction.transaction_id] = transaction
    return index


def _lookup_pair(
    candidate: Candidate, index: Mapping[str, Transaction]
) -> tuple[Transaction, Transaction] | None:
    left_id, right_id = candidate.transaction_ids
    left = index.get(left_id)
    right = index.get(right_id)
    if left is None or right is None:
        return None
    return left, right


def _finding_verdict(candidate: Candidate, index: Mapping[str, Transaction]) -> Verdict:
    pair = _lookup_pair(candidate, index)
    if pair is None:
        evidence = [
            Evidence(
                field="transaction_ids",
                values=[candidate.transaction_ids[0], candidate.transaction_ids[1]],
            ),
            Evidence(field="signal", values=[candidate.signal.value]),
            Evidence(
                field="amount_at_risk",
                values=[_money_text(candidate.amount_at_risk)],
            ),
        ]
    else:
        left, right = pair
        evidence = _finding_evidence(candidate, left, right)
    # Shared Verdict requires these fields; a rules engine has no model.
    return Verdict.model_construct(
        candidate_id=candidate.candidate_id,
        verdict=VerdictOutcome.escalate,
        explanation_type=ExplanationType.none,
        reasoning=_FINDING_REASONING,
        evidence=evidence,
        confidence=None,
        model_used=None,
        tokens_in=0,
        tokens_out=0,
        cache_read=0,
    )


def _finding_evidence(
    candidate: Candidate, left: Transaction, right: Transaction
) -> list[Evidence]:
    return [
        Evidence(field="signal", values=[candidate.signal.value]),
        Evidence(
            field="amount_at_risk",
            values=[_money_text(candidate.amount_at_risk)],
        ),
        Evidence(
            field="vendor_canonical",
            values=[_text(left.vendor_canonical), _text(right.vendor_canonical)],
        ),
        Evidence(
            field="invoice_canonical",
            values=[_text(left.invoice_canonical), _text(right.invoice_canonical)],
        ),
        Evidence(
            field="amount",
            values=[_money_text(left.amount), _money_text(right.amount)],
        ),
        Evidence(
            field="po_number",
            values=[_text(left.po_number), _text(right.po_number)],
        ),
        Evidence(
            field="payment_date",
            values=[_date_text(left.payment_date), _date_text(right.payment_date)],
        ),
        Evidence(
            field="payment_status",
            values=[_text(left.payment_status), _text(right.payment_status)],
        ),
        Evidence(
            field="department",
            values=[_text(left.department), _text(right.department)],
        ),
        Evidence(
            field="description",
            values=[_text(left.description), _text(right.description)],
        ),
    ]


def _text(value: str | None) -> str:
    return value if value is not None else ""


def _date_text(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _money_text(value: Decimal) -> str:
    return str(value)
