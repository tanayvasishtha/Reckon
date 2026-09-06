"""Compare the rules-only baseline with the full pipeline on labelled data.

Ground truth is read here and nowhere in stages 1 to 6. Findings are
``verdict=escalate``. Complexity: O(R + C log C + A) for R eval rows, C
blocked candidates, and A residual adjudications.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

from adapters.la import LAAdapter
from agents.adjudicate import adjudicate
from agents.llm import run_header
from baseline.rules_only import run_rules_only
from core.aggregate import aggregate
from core.blocking import block
from core.dismiss import dismiss
from core.models import GroundTruth, Transaction, Verdict, VerdictOutcome
from core.normalize import canonical_invoice, canonical_vendor
from core.settings import Settings, load_settings
from data.synth import (
    DECOY_REASONS,
    DEFAULT_GENERATED_DIR,
    DEFAULT_SOURCE,
    generate_eval_set,
)
from evaluation.entity_eval import score_entity_resolution

log = logging.getLogger("evaluation.harness")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = REPO_ROOT / "RESULTS.md"
EVAL_SEED = 0
_MILLION = Decimal(1000000)
_DETERMINISTIC = "deterministic"


@dataclass(frozen=True, slots=True)
class PipelineScore:
    precision: float
    recall: float
    f1: float
    false_positives_by_reason: dict[str, int]
    dollars_correct: Decimal
    dollars_wrong: Decimal


@dataclass(frozen=True, slots=True)
class RunStats:
    wall_clock_s: float
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_hit_rate: float
    token_cost: Decimal
    model_escalation_rate: float
    escalate: int
    dismiss: int
    errored: int


@dataclass(frozen=True, slots=True)
class Comparison:
    entity_precision: float
    entity_recall: float
    entity_f1: float
    baseline_score: PipelineScore
    pipeline_score: PipelineScore
    baseline_stats: RunStats
    pipeline_cold_stats: RunStats
    pipeline_warm_stats: RunStats


def score_pipeline(
    verdicts: Sequence[Verdict], ground_truth: GroundTruth
) -> PipelineScore:
    """Score escalate findings against planted duplicates and decoys."""
    flagged_pairs, risk = _finding_index(verdicts)
    true_positives = 0
    dollars_correct = Decimal(0)
    for case in ground_truth.true_duplicates:
        hits = _flagged_pairs_in(case.transaction_ids, flagged_pairs)
        if hits:
            true_positives += 1
            dollars_correct += case.amount

    false_positives = {reason: 0 for reason in DECOY_REASONS}
    dollars_wrong = Decimal(0)
    for case in ground_truth.decoys:
        hits = _flagged_pairs_in(case.transaction_ids, flagged_pairs)
        if not hits:
            continue
        false_positives[case.reason] = false_positives.get(case.reason, 0) + 1
        amounts = [risk[key] for key in hits if key in risk]
        if amounts:
            dollars_wrong += max(amounts)

    predicted_positive = true_positives + sum(false_positives.values())
    precision = _ratio(true_positives, predicted_positive)
    recall = _ratio(true_positives, len(ground_truth.true_duplicates))
    return PipelineScore(
        precision=precision,
        recall=recall,
        f1=_f1(precision, recall),
        false_positives_by_reason={
            key: false_positives[key] for key in sorted(false_positives)
        },
        dollars_correct=dollars_correct,
        dollars_wrong=dollars_wrong,
    )


def run_comparison(
    *,
    source: Path | None = None,
    dest_dir: Path | None = None,
    seed: int = EVAL_SEED,
) -> Comparison:
    """Build the eval set, run baseline and full pipeline in replay, and score both."""
    ledger_source = DEFAULT_SOURCE if source is None else source
    out_dir = DEFAULT_GENERATED_DIR if dest_dir is None else dest_dir
    settings = _replay_settings()
    log.info(
        "harness start source=%s dest_dir=%s seed=%s %s",
        ledger_source,
        out_dir,
        seed,
        run_header(settings),
    )
    eval_set = generate_eval_set(ledger_source, out_dir, seed=seed)
    adapter = LAAdapter()
    rows = list(adapter.iter_transactions(eval_set.ledger_path))
    caps = adapter.capabilities
    truth = eval_set.ground_truth

    started = time.perf_counter()
    baseline_verdicts = run_rules_only(rows, caps, settings, run_id="eval-baseline")
    baseline_stats = _stats(baseline_verdicts, time.perf_counter() - started, settings)

    started = time.perf_counter()
    pipeline_verdicts = _run_full(rows, caps, settings, run_id="eval-full-cold")
    cold_stats = _stats(pipeline_verdicts, time.perf_counter() - started, settings)

    started = time.perf_counter()
    warm_verdicts = _run_full(rows, caps, settings, run_id="eval-full-warm")
    warm_stats = _stats(warm_verdicts, time.perf_counter() - started, settings)

    entity_precision, entity_recall, entity_f1 = score_entity_resolution(
        eval_set.ledger_path
    )
    return Comparison(
        entity_precision=entity_precision,
        entity_recall=entity_recall,
        entity_f1=entity_f1,
        baseline_score=score_pipeline(baseline_verdicts, truth),
        pipeline_score=score_pipeline(pipeline_verdicts, truth),
        baseline_stats=baseline_stats,
        pipeline_cold_stats=cold_stats,
        pipeline_warm_stats=warm_stats,
    )


def write_results(path: Path, comparison: Comparison | None = None) -> None:
    """Write the three RESULTS.md sections. Runs the comparison when omitted."""
    measured = run_comparison() if comparison is None else comparison
    path.write_text(_render(measured), encoding="utf-8", newline="\n")
    log.info("wrote %s", path)


def _run_full(
    rows: Sequence[Transaction],
    capabilities: object,
    settings: Settings,
    *,
    run_id: str,
) -> list[Verdict]:
    aggregated = aggregate(rows, run_id=run_id)
    normalised = _normalise(aggregated)
    candidates = block(normalised, capabilities, settings, run_id=run_id)
    residual, dismissed = dismiss(candidates, normalised, run_id=run_id)
    judged = asyncio.run(adjudicate(residual, normalised, settings, run_id=run_id))
    verdicts = dismissed + judged
    verdicts.sort(key=lambda item: item.candidate_id)
    return verdicts


def _normalise(transactions: Sequence[Transaction]) -> list[Transaction]:
    result = [
        item.model_copy(
            update={
                "vendor_canonical": canonical_vendor(item.vendor_name_raw),
                "invoice_canonical": canonical_invoice(item.invoice_number_raw),
            }
        )
        for item in transactions
    ]
    result.sort(key=lambda item: item.transaction_id)
    return result


def _replay_settings() -> Settings:
    loaded = load_settings()
    fast = loaded.model_fast if loaded.model_fast else "fast-model"
    return loaded.model_copy(
        update={"api_base": None, "api_key": None, "model_fast": fast}
    )


def _finding_index(
    verdicts: Sequence[Verdict],
) -> tuple[set[frozenset[str]], dict[frozenset[str], Decimal]]:
    flagged: set[frozenset[str]] = set()
    risk: dict[frozenset[str], Decimal] = {}
    for verdict in verdicts:
        if verdict.verdict is not VerdictOutcome.escalate:
            continue
        pair = _pair_ids(verdict.candidate_id)
        if pair is None:
            continue
        key = frozenset(pair)
        flagged.add(key)
        amount = _risk(verdict)
        if amount is None:
            continue
        previous = risk.get(key)
        if previous is None or amount > previous:
            risk[key] = amount
    return flagged, risk


def _pair_ids(candidate_id: str) -> tuple[str, str] | None:
    parts = candidate_id.split(":")
    if len(parts) < 3 or parts[-2] == "" or parts[-1] == "":
        return None
    return parts[-2], parts[-1]


def _risk(verdict: Verdict) -> Decimal | None:
    for item in verdict.evidence:
        if item.field != "amount_at_risk" or not item.values:
            continue
        try:
            return Decimal(item.values[0])
        except InvalidOperation:
            return None
    return None


def _flagged_pairs_in(
    transaction_ids: Sequence[str], flagged_pairs: set[frozenset[str]]
) -> list[frozenset[str]]:
    return [
        frozenset((left, right))
        for left, right in combinations(transaction_ids, 2)
        if frozenset((left, right)) in flagged_pairs
    ]


def _stats(
    verdicts: Sequence[Verdict], elapsed_s: float, settings: Settings
) -> RunStats:
    tokens_in = sum(item.tokens_in for item in verdicts)
    tokens_out = sum(item.tokens_out for item in verdicts)
    cache_read = sum(item.cache_read for item in verdicts)
    escalate = sum(1 for item in verdicts if item.verdict is VerdictOutcome.escalate)
    dismiss = sum(1 for item in verdicts if item.verdict is VerdictOutcome.dismiss)
    errored = len(verdicts) - escalate - dismiss
    return RunStats(
        wall_clock_s=elapsed_s,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read=cache_read,
        cache_hit_rate=_ratio(cache_read, tokens_in),
        token_cost=_token_cost(verdicts, settings),
        model_escalation_rate=_model_escalation_rate(verdicts, settings),
        escalate=escalate,
        dismiss=dismiss,
        errored=errored,
    )


def _token_cost(verdicts: Sequence[Verdict], settings: Settings) -> Decimal:
    cost = Decimal(0)
    esc = settings.model_escalate
    for item in verdicts:
        use_esc = (
            esc is not None and item.model_used is not None and item.model_used == esc
        )
        price_in = settings.price_esc_in if use_esc else settings.price_fast_in
        price_out = settings.price_esc_out if use_esc else settings.price_fast_out
        cost += (
            Decimal(item.tokens_in) * price_in + Decimal(item.tokens_out) * price_out
        )
    return cost / _MILLION


def _model_escalation_rate(verdicts: Sequence[Verdict], settings: Settings) -> float:
    esc = settings.model_escalate
    if esc is None or esc == "" or esc == settings.model_fast:
        return 0.0
    judged = 0
    routed = 0
    for item in verdicts:
        used = item.model_used
        if used is None or used == _DETERMINISTIC:
            continue
        judged += 1
        if used == esc:
            routed += 1
    return _ratio(routed, judged)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _cell(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(item) for item in row) + " |" for row in rows)
    return "\n".join(lines)


def _attr_rows(names: Sequence[str], *objs: object) -> list[list[object]]:
    return [[name, *[getattr(obj, name) for obj in objs]] for name in names]


def _render(comparison: Comparison) -> str:
    baseline = comparison.baseline_score
    pipeline = comparison.pipeline_score
    reasons = sorted(
        set(baseline.false_positives_by_reason)
        | set(pipeline.false_positives_by_reason)
    )
    fp_rows = [
        [
            reason,
            baseline.false_positives_by_reason.get(reason, 0),
            pipeline.false_positives_by_reason.get(reason, 0),
        ]
        for reason in reasons
    ]
    detect = _attr_rows(
        ("precision", "recall", "f1", "dollars_correct", "dollars_wrong"),
        baseline,
        pipeline,
    )
    cost = _attr_rows(
        (
            "wall_clock_s",
            "tokens_in",
            "tokens_out",
            "cache_read",
            "cache_hit_rate",
            "token_cost",
            "model_escalation_rate",
            "escalate",
            "dismiss",
            "errored",
        ),
        comparison.baseline_stats,
        comparison.pipeline_cold_stats,
        comparison.pipeline_warm_stats,
    )
    entity = [
        ["precision", comparison.entity_precision],
        ["recall", comparison.entity_recall],
        ["f1", comparison.entity_f1],
    ]
    return "\n".join(
        [
            "# Results",
            "",
            "Numbers below are measured on the labelled eval set. They are not adjusted.",
            "",
            "## Entity resolution",
            "",
            "Vendor canonicalisation scored against the publisher `vendor_id` on real LA rows.",
            "",
            _table(["metric", "value"], entity),
            "",
            "## Duplicate detection",
            "",
            "Precision, recall, and F1 are computed on planted duplicates. A finding is `verdict=escalate`.",
            "",
            _table(["metric", "rules-only", "full pipeline"], detect),
            "",
            "### False positives by decoy reason",
            "",
            _table(["reason", "rules-only", "full pipeline"], fp_rows),
            "",
            "## Cost and runtime",
            "",
            _table(
                ["metric", "rules-only", "full pipeline cold", "full pipeline warm"],
                cost,
            ),
            "",
        ]
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    write_results(DEFAULT_RESULTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
