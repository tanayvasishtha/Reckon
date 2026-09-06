from __future__ import annotations

import csv
import json
from collections import Counter
from decimal import Decimal
from itertools import combinations
from pathlib import Path

import pytest

from adapters.la import LAAdapter
from core.aggregate import aggregate
from core.dismiss import dismiss
from core.models import Candidate, GroundTruth, Signal
from core.normalize import canonical_invoice, canonical_vendor
from data.synth import (
    DECOY_REASONS,
    DEFAULT_SOURCE,
    EVAL_LEDGER_NAME,
    GROUND_TRUTH_NAME,
    N_DECOYS,
    N_TRUE_DUPLICATES,
    PROGRESS_DRAWS,
    RECURRING_MONTHS,
    TRUE_DUPLICATE_TYPES,
    SynthError,
    generate_eval_set,
    main,
)

SEED = 0
FORBIDDEN_STATUSES = frozenset(
    {
        "CANCELLED",
        "CANCELED",
        "REVERSED",
        "REVERSAL",
        "VOID",
        "VOIDED",
    }
)


@pytest.fixture(scope="module")
def eval_set(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, GroundTruth]:
    dest = tmp_path_factory.mktemp("eval0")
    result = generate_eval_set(DEFAULT_SOURCE, dest, seed=SEED)
    truth = GroundTruth.model_validate_json(
        result.ground_truth_path.read_bytes().decode("utf-8")
    )
    return dest, truth


def _ledger_rows(ledger_path: Path) -> list[dict[str, str]]:
    with ledger_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _rows_by_id(ledger_path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    for row in _ledger_rows(ledger_path):
        rows[row["transaction_id"]] = row
    return rows


def test_every_true_type_and_decoy_type_is_present(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    _dest, truth = eval_set
    assert {item.type for item in truth.true_duplicates} == set(TRUE_DUPLICATE_TYPES)
    assert {item.reason for item in truth.decoys} == set(DECOY_REASONS)


def test_counts_are_forty_true_and_sixty_decoys(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    assert len(truth.true_duplicates) == N_TRUE_DUPLICATES
    assert len(truth.decoys) == N_DECOYS
    true_counts = Counter(item.type for item in truth.true_duplicates)
    decoy_counts = Counter(item.reason for item in truth.decoys)
    assert sum(true_counts.values()) == 40
    assert sum(decoy_counts.values()) == 60
    assert max(true_counts.values()) - min(true_counts.values()) <= 1
    assert set(decoy_counts.values()) == {10}
    rows = _ledger_rows(dest / EVAL_LEDGER_NAME)
    planted = [row for row in rows if row["transaction_id"].startswith("EVAL")]
    decoy_rows = {
        "cancelled": 2,
        "line_split": 2,
        "progress_payment": PROGRESS_DRAWS,
        "recurring": RECURRING_MONTHS,
        "different_po": 2,
        "partial_pair": 2,
    }
    expected_planted = N_TRUE_DUPLICATES * 2 + sum(
        decoy_counts[reason] * decoy_rows[reason] for reason in DECOY_REASONS
    )
    assert len(rows) == 20_000 + len(planted)
    assert len(planted) == expected_planted


def test_ground_truth_validates_against_the_model(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    raw = (dest / GROUND_TRUTH_NAME).read_bytes()
    restored = GroundTruth.model_validate_json(raw.decode("utf-8"))
    assert restored == truth
    payload = json.loads(raw.decode("utf-8"))
    for item in payload["true_duplicates"]:
        assert isinstance(item["amount"], str)
        assert Decimal(item["amount"]) >= 0
        assert "." in item["amount"] or item["amount"].isdigit()
        assert "e" not in item["amount"].lower()
        assert type(truth.true_duplicates[0].amount) is Decimal


def test_same_seed_reproduces_byte_identical_output(
    eval_set: tuple[Path, GroundTruth], tmp_path: Path
) -> None:
    first_dir, _truth = eval_set
    second = generate_eval_set(DEFAULT_SOURCE, tmp_path / "a", seed=SEED)
    first_ledger = (first_dir / EVAL_LEDGER_NAME).read_bytes()
    first_truth = (first_dir / GROUND_TRUTH_NAME).read_bytes()
    assert first_ledger == second.ledger_path.read_bytes()
    assert first_truth == second.ground_truth_path.read_bytes()
    assert b"\r\n" not in first_ledger
    other = generate_eval_set(DEFAULT_SOURCE, tmp_path / "c", seed=SEED + 1)
    assert other.ledger_path.read_bytes() != first_ledger
    assert other.ground_truth_path.read_bytes() != first_truth


def test_ground_truth_ids_are_in_the_ledger(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _rows_by_id(dest / EVAL_LEDGER_NAME)
    for case in (*truth.true_duplicates, *truth.decoys):
        assert len(case.transaction_ids) >= 2
        assert case.transaction_ids == sorted(case.transaction_ids)
        for transaction_id in case.transaction_ids:
            assert transaction_id in rows
            assert transaction_id.startswith("EVAL")
            status = rows[transaction_id]["payment_status"].strip().upper()
            assert status == "PAID"
            assert status not in FORBIDDEN_STATUSES


def test_decoys_and_true_duplicates_survive_dismiss(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    transactions = aggregate(LAAdapter().iter_transactions(dest / EVAL_LEDGER_NAME))
    index = {item.transaction_id: item for item in transactions}
    candidates: list[Candidate] = []
    for case in (*truth.true_duplicates, *truth.decoys):
        for left_id, right_id in combinations(case.transaction_ids, 2):
            assert left_id in index
            assert right_id in index
            candidates.append(
                Candidate(
                    candidate_id=f"{case.case_id}:{left_id}:{right_id}",
                    transaction_ids=(left_id, right_id),
                    signal=Signal.exact_duplicate,
                    amount_at_risk=Decimal("1.00"),
                    stage_generated="eval",
                )
            )
    residual, verdicts = dismiss(candidates, transactions)
    assert verdicts == []
    assert len(residual) == len(candidates)


def test_fuzzy_vendor_pairs_share_a_canonical_name(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _rows_by_id(dest / EVAL_LEDGER_NAME)
    cases = [
        item for item in truth.true_duplicates if item.type == "fuzzy_vendor_duplicate"
    ]
    assert cases
    for case in cases:
        names = [rows[tx_id]["vendor_name"] for tx_id in case.transaction_ids]
        assert names[0] != names[1]
        assert canonical_vendor(names[0]) == canonical_vendor(names[1])
        ids = {rows[tx_id]["vendor_id"] for tx_id in case.transaction_ids}
        assert len(ids) == 1


def test_invoice_format_variants_share_a_canonical_invoice(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _rows_by_id(dest / EVAL_LEDGER_NAME)
    cases = [
        item for item in truth.true_duplicates if item.type == "invoice_format_variant"
    ]
    assert cases
    for case in cases:
        invoices = [rows[tx_id]["inv_num"] for tx_id in case.transaction_ids]
        assert invoices[0] != invoices[1]
        assert canonical_invoice(invoices[0]) == canonical_invoice(invoices[1])
        assert any(value.upper().startswith("INV") for value in invoices)


def test_progress_and_recurring_decoys_match_the_spec(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _rows_by_id(dest / EVAL_LEDGER_NAME)
    progress = [item for item in truth.decoys if item.reason == "progress_payment"]
    recurring = [item for item in truth.decoys if item.reason == "recurring"]
    assert progress
    assert recurring
    for case in progress:
        assert len(case.transaction_ids) == PROGRESS_DRAWS
        descriptions = [rows[tx_id]["description"] for tx_id in case.transaction_ids]
        assert set(descriptions) == {
            f"Progress payment {draw} of {PROGRESS_DRAWS}"
            for draw in range(1, PROGRESS_DRAWS + 1)
        }
        invoices = {rows[tx_id]["inv_num"] for tx_id in case.transaction_ids}
        assert len(invoices) == PROGRESS_DRAWS
    for case in recurring:
        assert len(case.transaction_ids) == RECURRING_MONTHS
        descriptions = {rows[tx_id]["description"] for tx_id in case.transaction_ids}
        assert descriptions == {"Professional services"}
        dates = sorted(
            rows[tx_id]["transaction_date"][:10] for tx_id in case.transaction_ids
        )
        months = [int(value[5:7]) for value in dates]
        assert months == list(range(1, RECURRING_MONTHS + 1))


def test_decoy_why_rules_cannot_tell_is_populated(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    _dest, truth = eval_set
    for case in truth.decoys:
        assert case.why_rules_cannot_tell.strip()


def test_different_po_decoys_differ_by_one_digit(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _rows_by_id(dest / EVAL_LEDGER_NAME)
    cases = [item for item in truth.decoys if item.reason == "different_po"]
    assert cases
    for case in cases:
        left, right = (rows[tx_id]["po_num"] for tx_id in case.transaction_ids)
        assert left != right
        assert len(left) == len(right)
        diffs = [pair for pair in zip(left, right, strict=True) if pair[0] != pair[1]]
        assert len(diffs) == 1
        assert diffs[0][0].isdigit() and diffs[0][1].isdigit()


def test_line_split_decoys_are_single_line_rows(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    dest, truth = eval_set
    rows = _ledger_rows(dest / EVAL_LEDGER_NAME)
    counts = Counter(row["transaction_id"] for row in rows)
    by_id = {row["transaction_id"]: row for row in rows}
    cases = [item for item in truth.decoys if item.reason == "line_split"]
    assert cases
    for case in cases:
        amounts = {by_id[tx_id]["dollar_amount"] for tx_id in case.transaction_ids}
        invoices = {by_id[tx_id]["inv_num"] for tx_id in case.transaction_ids}
        dates = {by_id[tx_id]["transaction_date"] for tx_id in case.transaction_ids}
        assert len(amounts) == 2
        assert len(invoices) == 1
        assert len(dates) == 1
        for tx_id in case.transaction_ids:
            assert counts[tx_id] == 1


def test_missing_source_is_a_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.csv"
    with pytest.raises(SynthError, match="ledger not found"):
        generate_eval_set(missing, tmp_path, seed=0)


def test_missing_columns_is_a_clear_error(tmp_path: Path) -> None:
    source = tmp_path / "short.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(SynthError, match="missing columns"):
        generate_eval_set(source, tmp_path, seed=0)


def test_not_enough_templates_is_a_clear_error(tmp_path: Path) -> None:
    with DEFAULT_SOURCE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        row = next(reader)
    source = tmp_path / "tiny.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(header)
        writer.writerow(row)
    with pytest.raises(SynthError, match="usable template rows"):
        generate_eval_set(source, tmp_path, seed=0)


def test_cli_error_exit_code(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--seed",
                "0",
                "--source",
                str(tmp_path / "nope.csv"),
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 1
    )


def test_cli_writes_eval_files(tmp_path: Path) -> None:
    code = main(
        ["--seed", "0", "--source", str(DEFAULT_SOURCE), "--out-dir", str(tmp_path)]
    )
    assert code == 0
    ledger = tmp_path / EVAL_LEDGER_NAME
    truth = tmp_path / GROUND_TRUTH_NAME
    assert ledger.is_file()
    assert truth.is_file()
    loaded = GroundTruth.model_validate_json(truth.read_bytes().decode("utf-8"))
    assert len(loaded.true_duplicates) == 40
    assert len(loaded.decoys) == 60


def test_true_duplicate_amounts_are_decimals(
    eval_set: tuple[Path, GroundTruth],
) -> None:
    _dest, truth = eval_set
    for case in truth.true_duplicates:
        assert type(case.amount) is Decimal
        assert case.amount > 0
