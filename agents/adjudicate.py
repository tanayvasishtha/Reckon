"""Adjudicate residual candidate pairs with a two-tier model call.

Stage 5 of the pipeline. Blocking and dismiss have already produced the
pairs a rule cannot kill. This stage asks a model whether each pair is a
recoverable duplicate or has a legitimate explanation, and writes a
Verdict. The job is to kill candidates: most of what arrives here is
innocent, and a false positive is worse than a miss. Anything the model
cannot explain is escalated to a human. A response that fails schema
validation becomes verdict=errored and still reaches the queue; it is
never replaced with a guess. A missing replay recording is also written
as verdict=errored so the row is never dropped, but it is a setup miss
counted in unmatched_recordings rather than a genuine error.

Every candidate is sent to the fast model first. A second call to the
escalate model happens only when fast-model confidence is below
Settings.escalation_confidence_threshold (default 0.7). Calls run
concurrently through asyncio.gather behind a semaphore sized from
Settings.concurrency_limit (default 16). Results are sorted by
candidate_id so runs stay reproducible.

Complexity: O(C) fast-model calls for C candidates, plus a second call
for the subset whose fast-model confidence is below the threshold. At
most Settings.concurrency_limit candidates are in flight. Each call is
O(P) in the payload size to serialise the pair. Sorting C verdicts by
candidate_id is O(C log C). Extra memory is O(T + C) for the transaction
index and the result list. Naive sequential calls would miss the 300
candidates in 90 seconds budget at concurrency 16.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agents.cassettes import CassetteError
from agents.llm import ModelClient, ModelError, ModelResult
from agents.prompts import build_messages
from core.models import (
    Candidate,
    Evidence,
    ExplanationType,
    SchemaModel,
    Transaction,
    Verdict,
    VerdictOutcome,
)
from core.settings import Settings, load_settings

log = logging.getLogger("agents.adjudicate")

_UNMATCHED_RECORDING_PREFIX = "no recording:"


class AdjudicationError(Exception):
    """Stage 5 failed before any candidate could be judged."""


@dataclass(frozen=True, slots=True)
class AdjudicationSummary:
    """Four-way counts for one Stage 5 result list.

    errored is genuine failures only. unmatched_recordings are missing
    replay recordings, still written as verdict=errored so no row drops.
    """

    rows_in: int
    dismissed: int
    escalated: int
    errored: int
    unmatched_recordings: int
    passed_through: bool


def is_unmatched_recording(verdict: Verdict) -> bool:
    """Return True when reasoning marks a missing replay recording."""
    return verdict.reasoning.startswith(_UNMATCHED_RECORDING_PREFIX)


def summarise_adjudication(verdicts: Sequence[Verdict]) -> AdjudicationSummary:
    """Return counts that sum to len(verdicts).

    rows_in equals the verdict list length. unmatched_recordings are
    verdict=errored rows whose reasoning starts with "no recording:".
    errored excludes those unmatched rows. passed_through is True only
    when dismissed, genuine errored, and unmatched_recordings are all 0.
    """
    dismissed = 0
    escalated = 0
    genuine_errored = 0
    unmatched = 0
    for item in verdicts:
        if is_unmatched_recording(item):
            unmatched += 1
            continue
        if item.verdict is VerdictOutcome.dismiss:
            dismissed += 1
        elif item.verdict is VerdictOutcome.escalate:
            escalated += 1
        elif item.verdict is VerdictOutcome.errored:
            genuine_errored += 1
    return AdjudicationSummary(
        rows_in=len(verdicts),
        dismissed=dismissed,
        escalated=escalated,
        errored=genuine_errored,
        unmatched_recordings=unmatched,
        passed_through=(dismissed == 0 and genuine_errored == 0 and unmatched == 0),
    )


class ModelJudgement(SchemaModel):
    """Structured model output; accounting fields are filled by the client."""

    candidate_id: str
    verdict: VerdictOutcome
    explanation_type: ExplanationType
    reasoning: str
    evidence: list[Evidence]
    confidence: float


def _related_rows(
    candidate_id: str,
    related: Mapping[str, Sequence[Transaction]] | None,
) -> tuple[Transaction, ...]:
    if related is None:
        return ()
    rows = related.get(candidate_id, ())
    return tuple(rows)


def _context_text(
    candidate_id: str,
    context: Mapping[str, str] | None,
) -> str | None:
    if context is None:
        return None
    return context.get(candidate_id)


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


def _errored_verdict(
    candidate: Candidate,
    *,
    reasoning: str,
    model_used: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read: int = 0,
) -> Verdict:
    return Verdict(
        candidate_id=candidate.candidate_id,
        verdict=VerdictOutcome.errored,
        explanation_type=ExplanationType.none,
        reasoning=reasoning,
        evidence=[],
        confidence=0.0,
        model_used=model_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read=cache_read,
    )


def _verdict_from_result(
    candidate: Candidate,
    result: ModelResult[ModelJudgement],
    *,
    extra_tokens_in: int = 0,
    extra_tokens_out: int = 0,
    extra_cache_read: int = 0,
) -> Verdict:
    judgement = result.value
    return Verdict(
        candidate_id=candidate.candidate_id,
        verdict=judgement.verdict,
        explanation_type=judgement.explanation_type,
        reasoning=judgement.reasoning,
        evidence=list(judgement.evidence),
        confidence=judgement.confidence,
        model_used=result.model_used,
        tokens_in=result.tokens_in + extra_tokens_in,
        tokens_out=result.tokens_out + extra_tokens_out,
        cache_read=result.cache_read + extra_cache_read,
    )


def _add_tokens(verdict: Verdict, result: ModelResult[ModelJudgement]) -> Verdict:
    return verdict.model_copy(
        update={
            "tokens_in": verdict.tokens_in + result.tokens_in,
            "tokens_out": verdict.tokens_out + result.tokens_out,
            "cache_read": verdict.cache_read + result.cache_read,
        }
    )


def _cause_chain_has_cassette_error(exc: BaseException) -> bool:
    current: BaseException | None = exc.__cause__
    seen: set[int] = {id(exc)}
    while current is not None and id(current) not in seen:
        if isinstance(current, CassetteError):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def _is_unmatched_client_error(exc: BaseException) -> bool:
    if type(exc).__name__ == "ReplayMissError":
        return True
    if _cause_chain_has_cassette_error(exc):
        return True
    return "no cassette" in str(exc)


def _client_error_reasoning(exc: ModelError) -> str:
    text = str(exc)
    if not _is_unmatched_client_error(exc):
        return text
    return f"{_UNMATCHED_RECORDING_PREFIX}{text}"


async def _complete(
    client: ModelClient,
    messages: Sequence[Mapping[str, str]],
    model: str,
    candidate: Candidate,
) -> ModelResult[ModelJudgement] | Verdict:
    try:
        return await client.complete(messages, ModelJudgement, model=model)
    except ModelError as exc:
        return _errored_verdict(
            candidate,
            reasoning=_client_error_reasoning(exc),
            model_used=model,
        )


def _needs_second_pass(result: ModelResult[ModelJudgement], settings: Settings) -> bool:
    if result.value.confidence >= settings.escalation_confidence_threshold:
        return False
    escalate_model = settings.model_escalate
    if escalate_model is None or escalate_model == "":
        log.warning(
            "fast-model confidence %s is below threshold %s and "
            "RECKON_MODEL_ESCALATE is not set; keeping the fast verdict "
            "for candidate_id=%s",
            result.value.confidence,
            settings.escalation_confidence_threshold,
            result.value.candidate_id,
        )
        return False
    return escalate_model != result.model_used


async def _adjudicate_one(
    candidate: Candidate,
    *,
    index: Mapping[str, Transaction],
    settings: Settings,
    client: ModelClient,
    related: Mapping[str, Sequence[Transaction]] | None,
    context: Mapping[str, str] | None,
    run_id: str,
) -> Verdict:
    pair = _lookup_pair(candidate, index)
    fast_model = settings.model_fast if settings.model_fast is not None else ""
    if pair is None:
        log.warning(
            "run_id=%s candidate_id=%s missing transaction rows",
            run_id,
            candidate.candidate_id,
        )
        return _errored_verdict(
            candidate,
            reasoning=(
                "both transaction rows are required and at least one id "
                "is missing from the transaction set."
            ),
            model_used=fast_model,
        )

    left, right = pair
    messages = build_messages(
        candidate,
        left,
        right,
        related=_related_rows(candidate.candidate_id, related),
        context=_context_text(candidate.candidate_id, context),
    )
    if fast_model == "":
        return _errored_verdict(
            candidate,
            reasoning="no model name. Pass model= or set RECKON_MODEL_FAST.",
            model_used="",
        )

    first = await _complete(client, messages, fast_model, candidate)
    if isinstance(first, Verdict):
        return first
    if not _needs_second_pass(first, settings):
        return _verdict_from_result(candidate, first)

    escalate_model = settings.model_escalate or ""
    log.debug(
        "run_id=%s candidate_id=%s route=escalate confidence=%s",
        run_id,
        candidate.candidate_id,
        first.value.confidence,
    )
    second = await _complete(client, messages, escalate_model, candidate)
    if isinstance(second, Verdict):
        return _add_tokens(second, first)
    return _verdict_from_result(
        candidate,
        second,
        extra_tokens_in=first.tokens_in,
        extra_tokens_out=first.tokens_out,
        extra_cache_read=first.cache_read,
    )


def _model_escalations(verdicts: Sequence[Verdict], settings: Settings) -> int:
    escalate_model = settings.model_escalate
    if escalate_model is None or escalate_model == settings.model_fast:
        return 0
    return sum(1 for item in verdicts if item.model_used == escalate_model)


def _log_adjudicate_done(
    summary: AdjudicationSummary,
    *,
    model_escalations: int,
    elapsed_ms: int,
    run_id: str,
) -> None:
    log.info(
        "adjudicate done rows_in=%s dismissed=%s escalated=%s "
        "errored=%s unmatched_recordings=%s passed_through=%s "
        "model_escalations=%s elapsed_ms=%s run_id=%s",
        summary.rows_in,
        summary.dismissed,
        summary.escalated,
        summary.errored,
        summary.unmatched_recordings,
        summary.passed_through,
        model_escalations,
        elapsed_ms,
        run_id,
    )


async def adjudicate(
    candidates: Iterable[Candidate],
    transactions: Iterable[Transaction],
    settings: Settings | None = None,
    *,
    client: ModelClient | None = None,
    related: Mapping[str, Sequence[Transaction]] | None = None,
    context: Mapping[str, str] | None = None,
    cassette_dir: Path | None = None,
    run_id: str | None = None,
) -> list[Verdict]:
    """Return a Verdict for every candidate, sorted by candidate_id.

    Every candidate is sent to the fast model first. A second call to the
    escalate model happens only when fast-model confidence is below
    Settings.escalation_confidence_threshold. Schema failures and client
    errors become verdict=errored rather than a guessed judgement.
    Missing replay recordings stay in the list as verdict=errored with
    reasoning prefixed "no recording:".
    """
    resolved = settings if settings is not None else load_settings()
    if resolved.concurrency_limit < 1:
        raise AdjudicationError("RECKON_CONCURRENCY_LIMIT must be a positive integer.")

    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    started = time.perf_counter()
    candidate_list = list(candidates)
    log.info(
        "adjudicate start run_id=%s rows_in=%s concurrency=%s "
        "model_fast=%s model_escalate=%s",
        active_run_id,
        len(candidate_list),
        resolved.concurrency_limit,
        resolved.model_fast or "-",
        resolved.model_escalate or "-",
    )
    if not candidate_list:
        _log_adjudicate_done(
            summarise_adjudication([]),
            model_escalations=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            run_id=active_run_id,
        )
        return []

    index = _index_transactions(transactions)
    owns_client = False
    if client is None:
        active_client = ModelClient(
            resolved, cassette_dir=cassette_dir, run_id=active_run_id
        )
        owns_client = True
    else:
        active_client = client
    semaphore = asyncio.Semaphore(resolved.concurrency_limit)

    async def _bounded(candidate: Candidate) -> Verdict:
        async with semaphore:
            return await _adjudicate_one(
                candidate,
                index=index,
                settings=resolved,
                client=active_client,
                related=related,
                context=context,
                run_id=active_run_id,
            )

    try:
        gathered = await asyncio.gather(*[_bounded(item) for item in candidate_list])
    finally:
        if owns_client:
            await active_client.aclose()

    verdicts = sorted(gathered, key=lambda item: item.candidate_id)
    summary = summarise_adjudication(verdicts)
    _log_adjudicate_done(
        summary,
        model_escalations=_model_escalations(verdicts, resolved),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        run_id=active_run_id,
    )
    return verdicts
