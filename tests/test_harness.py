from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from core.models import (
    Decoy,
    Evidence,
    ExplanationType,
    GroundTruth,
    TrueDuplicate,
    Verdict,
    VerdictOutcome,
)
from evaluation.entity_eval import score_entity_resolution
from evaluation.harness import (
    Comparison,
    PipelineScore,
    RunStats,
    score_pipeline,
    write_results,
)

LA_FIELDS = (
    "transaction_id",
    "vendor_name",
    "vendor_id",
    "inv_num",
    "inv_date",
    "transaction_date",
    "dollar_amount",
    "po_num",
    "payment_method",
    "payment_status",
    "department_name",
    "description",
)


def _write_la(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(LA_FIELDS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _la_row(
    transaction_id: str,
    vendor_name: str,
    vendor_id: str,
    *,
    amount: str = "10.00",
) -> dict[str, str]:
    return {
        "transaction_id": transaction_id,
        "vendor_name": vendor_name,
        "vendor_id": vendor_id,
        "inv_num": "INV1",
        "inv_date": "2024-01-01",
        "transaction_date": "2024-01-02",
        "dollar_amount": amount,
        "po_num": "PO1",
        "payment_method": "EFT",
        "payment_status": "PAID",
        "department_name": "AGING",
        "description": "supplies",
    }


def _verdict(
    candidate_id: str,
    *,
    outcome: VerdictOutcome = VerdictOutcome.escalate,
    amount_at_risk: str | None = "10.00",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_read: int = 0,
    model_used: str = "fast-model",
    confidence: float = 0.9,
) -> Verdict:
    evidence: list[Evidence] = []
    if amount_at_risk is not None:
        evidence.append(Evidence(field="amount_at_risk", values=[amount_at_risk]))
    return Verdict(
        candidate_id=candidate_id,
        verdict=outcome,
        explanation_type=ExplanationType.none,
        reasoning="test",
        evidence=evidence,
        confidence=confidence,
        model_used=model_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read=cache_read,
    )


def _truth() -> GroundTruth:
    return GroundTruth(
        true_duplicates=[
            TrueDuplicate(
                case_id="dup-exact-01",
                transaction_ids=["dup-a", "dup-b"],
                type="exact_duplicate",
                amount=Decimal("100.00"),
            ),
            TrueDuplicate(
                case_id="dup-split-01",
                transaction_ids=["split-a", "split-b"],
                type="split_payment_duplicate",
                amount=Decimal("40.00"),
            ),
        ],
        decoys=[
            Decoy(
                case_id="decoy-cancelled-01",
                transaction_ids=["canc-a", "canc-b"],
                reason="cancelled",
                why_rules_cannot_tell="status is PAID",
            ),
            Decoy(
                case_id="decoy-progress-01",
                transaction_ids=["p1", "p2", "p3", "p4", "p5"],
                reason="progress_payment",
                why_rules_cannot_tell="draw text only",
            ),
        ],
    )


def _empty_stats() -> RunStats:
    return RunStats(
        wall_clock_s=1.5,
        tokens_in=0,
        tokens_out=0,
        cache_read=0,
        cache_hit_rate=0.0,
        token_cost=Decimal(0),
        model_escalation_rate=0.0,
        escalate=0,
        dismiss=0,
        errored=0,
    )


def test_entity_resolution_perfect_on_name_variants(tmp_path: Path) -> None:
    path = _write_la(
        tmp_path / "perfect.csv",
        [
            _la_row("t1", "W W GRAINGER INC", "V-G"),
            _la_row("t2", "W. W. GRAINGER INC.", "V-G"),
            _la_row("t3", "Acme LLC", "V-A"),
        ],
    )
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 1.0
    assert recall == 1.0
    assert f1 == 1.0


def test_entity_resolution_penalises_over_merge(tmp_path: Path) -> None:
    path = _write_la(
        tmp_path / "overmerge.csv",
        [
            _la_row("t1", "W W GRAINGER INC", "V-G1"),
            _la_row("t2", "W. W. GRAINGER INC.", "V-G2"),
        ],
    )
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 0.0
    assert recall == 0.0
    assert f1 == 0.0


def test_entity_resolution_penalises_under_merge(tmp_path: Path) -> None:
    path = _write_la(
        tmp_path / "undermerge.csv",
        [
            _la_row("t1", "JOHNSON CONTROLS INC", "V-J"),
            _la_row("t2", "JOHNSON CONTROLS SECURITY", "V-J"),
        ],
    )
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 0.0
    assert recall == 0.0
    assert f1 == 0.0


def test_entity_resolution_skips_planted_eval_rows(tmp_path: Path) -> None:
    path = _write_la(
        tmp_path / "planted.csv",
        [
            _la_row("t1", "W W GRAINGER INC", "V-G"),
            _la_row("t2", "W. W. GRAINGER INC.", "V-G"),
            _la_row("EVAL00000001", "W.W. GRAINGER INC.", "V-OTHER"),
        ],
    )
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 1.0
    assert recall == 1.0
    assert f1 == 1.0


def test_entity_resolution_skips_missing_vendor_id(tmp_path: Path) -> None:
    path = _write_la(
        tmp_path / "noid.csv",
        [
            _la_row("t1", "W W GRAINGER INC", "V-G"),
            _la_row("t2", "W. W. GRAINGER INC.", "V-G"),
            _la_row("t3", "W.W. GRAINGER INC.", ""),
        ],
    )
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 1.0
    assert recall == 1.0
    assert f1 == 1.0


def test_entity_resolution_scores_every_row(tmp_path: Path) -> None:
    rows = [_la_row(f"acme-{i}", "Acme LLC", "V-A") for i in range(3)]
    rows.append(_la_row("other", "Solo Corp", "V-S"))
    path = _write_la(tmp_path / "rows.csv", rows)
    precision, recall, f1 = score_entity_resolution(path)
    assert precision == 1.0
    assert recall == 1.0
    assert f1 == 1.0


def test_score_pipeline_true_and_false_positives() -> None:
    verdicts = [
        _verdict("exact_duplicate:dup-a:dup-b", amount_at_risk="100.00"),
        _verdict("exact_duplicate:canc-a:canc-b", amount_at_risk="25.50"),
        _verdict("invoice_variant:p1:p2", amount_at_risk="9.00"),
        _verdict("exact_duplicate:noise-a:noise-b", amount_at_risk="999.00"),
    ]
    score = score_pipeline(verdicts, _truth())
    assert score.precision == 1 / 3
    assert score.recall == 0.5
    assert score.f1 == 0.4
    assert score.false_positives_by_reason["cancelled"] == 1
    assert score.false_positives_by_reason["progress_payment"] == 1
    assert score.false_positives_by_reason["recurring"] == 0
    assert score.dollars_correct == Decimal("100.00")
    assert score.dollars_wrong == Decimal("34.50")
    assert type(score.dollars_correct) is Decimal
    assert type(score.dollars_wrong) is Decimal


def test_score_pipeline_counts_a_case_once() -> None:
    verdicts = [
        _verdict("exact_duplicate:dup-a:dup-b", amount_at_risk="100.00"),
        _verdict("split_payment:dup-a:dup-b", amount_at_risk="100.00"),
        _verdict("invoice_variant:p1:p2", amount_at_risk="3.00"),
        _verdict("invoice_variant:p2:p3", amount_at_risk="8.00"),
    ]
    score = score_pipeline(verdicts, _truth())
    assert score.recall == 0.5
    assert score.false_positives_by_reason["progress_payment"] == 1
    assert score.dollars_correct == Decimal("100.00")
    assert score.dollars_wrong == Decimal("8.00")


def test_score_pipeline_dismiss_and_errored_are_not_findings() -> None:
    verdicts = [
        _verdict(
            "exact_duplicate:dup-a:dup-b",
            outcome=VerdictOutcome.dismiss,
            amount_at_risk="100.00",
        ),
        _verdict(
            "exact_duplicate:split-a:split-b",
            outcome=VerdictOutcome.errored,
            amount_at_risk="40.00",
        ),
        _verdict(
            "exact_duplicate:canc-a:canc-b",
            outcome=VerdictOutcome.dismiss,
            amount_at_risk="25.50",
        ),
    ]
    score = score_pipeline(verdicts, _truth())
    assert score.precision == 0.0
    assert score.recall == 0.0
    assert score.f1 == 0.0
    assert score.dollars_correct == Decimal(0)
    assert score.dollars_wrong == Decimal(0)
    assert all(count == 0 for count in score.false_positives_by_reason.values())


def test_score_pipeline_perfect_recall_and_f1() -> None:
    verdicts = [
        _verdict("exact_duplicate:dup-a:dup-b"),
        _verdict("split_payment:split-a:split-b"),
    ]
    score = score_pipeline(verdicts, _truth())
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.f1 == 1.0
    assert score.dollars_correct == Decimal("140.00")
    assert score.dollars_wrong == Decimal(0)


def test_score_pipeline_empty_verdicts() -> None:
    score = score_pipeline([], _truth())
    assert score.precision == 0.0
    assert score.recall == 0.0
    assert score.f1 == 0.0
    assert set(score.false_positives_by_reason) == {
        "cancelled",
        "different_po",
        "line_split",
        "partial_pair",
        "progress_payment",
        "recurring",
    }


def test_write_results_fills_three_sections(tmp_path: Path) -> None:
    comparison = Comparison(
        entity_precision=0.8,
        entity_recall=0.5,
        entity_f1=0.6153846153846154,
        baseline_score=PipelineScore(
            precision=0.4,
            recall=1.0,
            f1=0.5714285714285714,
            false_positives_by_reason={
                "cancelled": 10,
                "different_po": 10,
                "line_split": 10,
                "partial_pair": 10,
                "progress_payment": 10,
                "recurring": 10,
            },
            dollars_correct=Decimal("1234.56"),
            dollars_wrong=Decimal("78.90"),
        ),
        pipeline_score=PipelineScore(
            precision=0.0,
            recall=0.0,
            f1=0.0,
            false_positives_by_reason={
                "cancelled": 0,
                "different_po": 0,
                "line_split": 0,
                "partial_pair": 0,
                "progress_payment": 0,
                "recurring": 0,
            },
            dollars_correct=Decimal(0),
            dollars_wrong=Decimal(0),
        ),
        baseline_stats=_empty_stats(),
        pipeline_cold_stats=RunStats(
            wall_clock_s=2.25,
            tokens_in=100,
            tokens_out=40,
            cache_read=20,
            cache_hit_rate=0.2,
            token_cost=Decimal("0.0001"),
            model_escalation_rate=0.0,
            escalate=0,
            dismiss=11,
            errored=1731,
        ),
        pipeline_warm_stats=_empty_stats(),
    )
    dest = tmp_path / "RESULTS.md"
    write_results(dest, comparison)
    text = dest.read_text(encoding="utf-8")
    assert text.count("\r") == 0
    assert "## Entity resolution" in text
    assert "## Duplicate detection" in text
    assert "## Cost and runtime" in text
    assert "### False positives by decoy reason" in text
    assert "| precision | 0.800000 |" in text
    assert "| recall | 0.500000 |" in text
    assert "| f1 | 0.615385 |" in text
    assert "| precision | 0.400000 | 0.000000 |" in text
    assert "| dollars_correct | 1234.56 | 0 |" in text
    assert "| cancelled | 10 | 0 |" in text
    assert "| tokens_in | 0 | 100 | 0 |" in text
    assert "| errored | 0 | 1731 | 0 |" in text
    assert "e-" not in text.lower()
