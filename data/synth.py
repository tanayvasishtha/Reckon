"""Plant a labelled evaluation set into a real ledger slice.

Takes a committed Checkbook L.A. slice, copies it, and appends planted
true duplicates plus planted decoys. Ground truth is written beside the
ledger and is never read by stages 1 to 6.

Every decoy is constructed so stage 4 (`core.dismiss`) cannot prove it
away. Cancelled decoys keep `payment_status=PAID` and put the tell in
the description. Line-split decoys use one row per transaction_id so
`line_count` stays 1 after aggregation. Progress, recurring, different-PO
and partial-pair decoys differ only in description, cadence, or a
one-digit PO change.

Complexity: O(R + P log P) for R source rows and P planted rows. The
slice is read once, planted rows are sorted, then the ledger is written
once. Extra memory is O(R + P). The committed 20,000-row sample is the
intended input; this module does not stream a multi-gigabyte file.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import random
import sys
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from core.models import Decoy, GroundTruth, TrueDuplicate

log = logging.getLogger("data.synth")

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = PACKAGE_DIR / "samples" / "la_sample.csv"
DEFAULT_GENERATED_DIR = PACKAGE_DIR / "generated"
EVAL_LEDGER_NAME = "eval_ledger.csv"
GROUND_TRUTH_NAME = "ground_truth.json"

N_TRUE_DUPLICATES = 40
N_DECOYS = 60
PROGRESS_DRAWS = 5
RECURRING_MONTHS = 6

TRUE_DUPLICATE_TYPES: tuple[str, ...] = (
    "exact_duplicate",
    "fuzzy_vendor_duplicate",
    "invoice_format_variant",
    "split_payment_duplicate",
    "cross_department_duplicate",
    "overpayment_vs_invoice",
)
DECOY_REASONS: tuple[str, ...] = (
    "cancelled",
    "line_split",
    "progress_payment",
    "recurring",
    "different_po",
    "partial_pair",
)

REQUIRED_COLUMNS: tuple[str, ...] = (
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

_CENTS = Decimal("0.01")
_PAID = "PAID"
_DATE_BASE = date(2019, 3, 4)
_FORBIDDEN_STATUSES = frozenset(
    {
        "CANCELLED",
        "CANCELED",
        "REVERSED",
        "REVERSAL",
        "VOID",
        "VOIDED",
    }
)
_FORBIDDEN_METHODS = frozenset({"CANCELLATION", "CANCELLED", "CANCELED"})
_WHY: dict[str, str] = {
    "cancelled": (
        "payment_status is PAID on both rows; dismiss only reads cancelled "
        "status codes, and the cancellation is stated only in the description."
    ),
    "line_split": (
        "Each row has its own transaction_id, so after aggregation "
        "line_count is 1 and the GL line-split rule cannot fire. Only the "
        "description and inv_line fields show these are distribution lines."
    ),
    "progress_payment": (
        "The only tell is the description naming the draw "
        "('Progress payment N of 5'); invoice numbers and payment dates "
        "differ, and no cancelled status is set."
    ),
    "recurring": (
        "The only tell is monthly cadence across six rows; invoice numbers "
        "differ, payment_status is PAID, and no line-split proof is present."
    ),
    "different_po": (
        "Purchase order numbers differ by one digit; dismiss does not "
        "compare PO numbers except to refuse a line-split, and both rows "
        "are PAID single-line payments of the same invoice."
    ),
    "partial_pair": (
        "A short payment against the same invoice looks like a split "
        "duplicate; only the partial-payment description distinguishes a "
        "legitimate installment, and line_count stays 1."
    ),
}


class SynthError(Exception):
    """The evaluation set could not be generated from the given slice."""


@dataclass(frozen=True, slots=True)
class EvalSet:
    ledger_path: Path
    ground_truth_path: Path
    ground_truth: GroundTruth
    source_row_count: int
    planted_row_count: int


@dataclass(frozen=True, slots=True)
class _Plant:
    transaction_ids: list[str]
    amount: Decimal
    rows: list[dict[str, str]]
    why: str | None = None


class _IdFactory:
    def __init__(self, taken: set[str]) -> None:
        self._taken = taken
        self._n = 0

    def next(self) -> str:
        while True:
            self._n += 1
            value = f"EVAL{self._n:08d}"
            if value not in self._taken:
                self._taken.add(value)
                return value


def generate_eval_set(
    source: Path,
    dest_dir: Path,
    *,
    seed: int,
    n_true: int = N_TRUE_DUPLICATES,
    n_decoys: int = N_DECOYS,
) -> EvalSet:
    """Copy `source`, plant labelled cases, and write ledger plus ground truth."""
    run_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info("synth start source=%s seed=%s run_id=%s", source, seed, run_id)

    fieldnames, source_rows = _read_source(source)
    templates = [row for row in source_rows if _is_template(row)]
    needed = n_true + n_decoys
    if len(templates) < needed:
        raise SynthError(
            f"{source} has {len(templates)} usable template rows; need "
            f"{needed} to plant {n_true} duplicates and {n_decoys} decoys. "
            "Use a larger slice with PAID rows that have vendor, invoice, "
            "amount, department, and PO."
        )

    departments = tuple(
        sorted(
            {
                row["department_name"].strip()
                for row in source_rows
                if row["department_name"].strip()
            }
        )
    )
    if len(departments) < 2:
        raise SynthError(
            f"{source} has fewer than two departments, so "
            "cross_department_duplicate cannot be planted. Use a slice "
            "that spans more than one department."
        )

    rng = random.Random(seed)
    chosen = rng.sample(templates, needed)
    ids = _IdFactory({row["transaction_id"] for row in source_rows})

    true_cases: list[TrueDuplicate] = []
    decoy_cases: list[Decoy] = []
    planted_rows: list[dict[str, str]] = []
    case_index = 0

    for label, seq in _spread(n_true, TRUE_DUPLICATE_TYPES):
        plant = _plant_true(
            label,
            chosen[case_index],
            case_index=case_index + 1,
            departments=departments,
            ids=ids,
        )
        case_index += 1
        true_cases.append(
            TrueDuplicate(
                case_id=f"dup-{label}-{seq:02d}",
                transaction_ids=list(plant.transaction_ids),
                type=label,
                amount=plant.amount,
            )
        )
        planted_rows.extend(plant.rows)

    for label, seq in _spread(n_decoys, DECOY_REASONS):
        plant = _plant_decoy(
            label,
            chosen[case_index],
            case_index=case_index + 1,
            ids=ids,
        )
        case_index += 1
        why = plant.why if plant.why is not None else _WHY[label]
        decoy_cases.append(
            Decoy(
                case_id=f"decoy-{label}-{seq:02d}",
                transaction_ids=list(plant.transaction_ids),
                reason=label,
                why_rules_cannot_tell=why,
            )
        )
        planted_rows.extend(plant.rows)

    true_cases.sort(key=lambda item: item.case_id)
    decoy_cases.sort(key=lambda item: item.case_id)
    planted_rows.sort(
        key=lambda row: (
            row["transaction_id"],
            row.get("inv_line", ""),
            row.get("inv_dist_line", ""),
            row["dollar_amount"],
        )
    )
    ground_truth = GroundTruth(true_duplicates=true_cases, decoys=decoy_cases)

    dest_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = dest_dir / EVAL_LEDGER_NAME
    truth_path = dest_dir / GROUND_TRUTH_NAME
    _atomic_write_csv(ledger_path, fieldnames, source_rows, planted_rows)
    _atomic_write_json(truth_path, ground_truth)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    true_counts = dict(sorted(Counter(item.type for item in true_cases).items()))
    decoy_counts = dict(sorted(Counter(item.reason for item in decoy_cases).items()))
    log.info(
        "synth done source_rows=%s planted_rows=%s true=%s decoys=%s "
        "true_counts=%s decoy_counts=%s elapsed_ms=%s run_id=%s",
        len(source_rows),
        len(planted_rows),
        len(true_cases),
        len(decoy_cases),
        true_counts,
        decoy_counts,
        elapsed_ms,
        run_id,
    )
    return EvalSet(
        ledger_path=ledger_path,
        ground_truth_path=truth_path,
        ground_truth=ground_truth,
        source_row_count=len(source_rows),
        planted_row_count=len(planted_rows),
    )


def _spread(n: int, labels: tuple[str, ...]) -> list[tuple[str, int]]:
    if n < len(labels):
        raise SynthError(
            f"need at least {len(labels)} cases to cover every type; got {n}."
        )
    base, extra = divmod(n, len(labels))
    plan: list[tuple[str, int]] = []
    for index, label in enumerate(labels):
        count = base + (1 if index < extra else 0)
        for seq in range(1, count + 1):
            plan.append((label, seq))
    return plan


def _read_source(source: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not source.is_file():
        raise SynthError(
            f"ledger not found: {source}. Pass a path to an existing CSV slice."
        )
    with source.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SynthError(f"{source} has no header row. Use a CSV with a header.")
        fieldnames = list(reader.fieldnames)
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise SynthError(
                "CSV is missing columns: "
                + ", ".join(missing)
                + ". Use the Checkbook L.A. slice in data/samples/la_sample.csv."
            )
        rows = [
            {key: _clean_cell(row.get(key, "")) for key in fieldnames} for row in reader
        ]
    if not rows:
        raise SynthError(f"{source} has a header but no data rows.")
    return fieldnames, rows


def _clean_cell(value: str | None) -> str:
    text = "" if value is None else value
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _is_template(row: Mapping[str, str]) -> bool:
    status = _normalise_status(row.get("payment_status", ""))
    if status != _PAID or status in _FORBIDDEN_STATUSES:
        return False
    method = _normalise_status(row.get("payment_method", ""))
    if method in _FORBIDDEN_METHODS:
        return False
    required = (
        "transaction_id",
        "vendor_name",
        "vendor_id",
        "inv_num",
        "dollar_amount",
        "department_name",
        "po_num",
    )
    if any(not row.get(column, "").strip() for column in required):
        return False
    try:
        amount = _parse_money(row["dollar_amount"])
    except SynthError:
        return False
    return amount > 0


def _normalise_status(status: str) -> str:
    cleaned = status.strip().upper().replace("-", " ").replace("_", " ")
    return " ".join(cleaned.split())


def _parse_money(raw: str) -> Decimal:
    text = raw.strip().replace(",", "").replace("$", "")
    if text == "":
        raise SynthError("dollar_amount is empty on a template row.")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise SynthError(
            f"could not parse dollar_amount {raw!r} as a decimal."
        ) from exc


def _format_money(value: Decimal) -> str:
    return format(value.quantize(_CENTS), "f")


def _fmt_date(value: date) -> str:
    return value.isoformat() + "T00:00:00.000"


def _add_months(start: date, months: int) -> date:
    month0 = start.month - 1 + months
    year = start.year + month0 // 12
    month = month0 % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _isolated_amount(base: Decimal, case_index: int) -> Decimal:
    offset = Decimal(case_index) * _CENTS + Decimal("0.17")
    value = base.quantize(_CENTS) + offset
    if value <= 0:
        value = Decimal(case_index) + Decimal("0.17")
    return value.quantize(_CENTS)


def _invoice_token(case_index: int, suffix: str = "") -> str:
    return f"EVL{case_index:05d}{suffix}"


def _split_amounts(total: Decimal, ratio: Decimal) -> tuple[Decimal, Decimal]:
    left = (total * ratio).quantize(_CENTS, rounding=ROUND_HALF_EVEN)
    right = total - left
    if left <= 0 or right <= 0 or left == right:
        left = (total / Decimal(2)).quantize(_CENTS, rounding=ROUND_DOWN)
        right = total - left
    if left == right and total >= _CENTS * 2:
        left -= _CENTS
        right += _CENTS
    if left <= 0 or right <= 0 or left == right:
        raise SynthError(f"cannot split amount {total} into two unequal parts")
    return left, right


def _extra_amount(total: Decimal) -> Decimal:
    extra = (total * Decimal("0.10")).quantize(_CENTS, rounding=ROUND_HALF_EVEN)
    if extra <= 0:
        extra = _CENTS
    return extra


def _fuzzy_name_pair(name: str) -> tuple[str, str]:
    left = name.strip()
    upper = left.upper()
    if upper.endswith(" INC."):
        right = left[:-1]
    elif upper.endswith(" INC"):
        right = left + "."
    elif upper.endswith(" LLC."):
        right = left[:-1]
    elif upper.endswith(" LLC"):
        right = left + "."
    elif upper.endswith(" CORP."):
        right = left[:-1]
    elif upper.endswith(" CORP"):
        right = left + "."
    else:
        right = f"{left} INC."
    if left == right:
        left = f"{left} INC."
        right = f"{name.strip()} INC"
    if left == right:
        raise SynthError(f"could not form a fuzzy vendor pair from {name!r}")
    return left, right


def _adjacent_po(po: str) -> str:
    text = po.strip()
    if text == "":
        return "EVALPO1"
    chars = list(text)
    for index in range(len(chars) - 1, -1, -1):
        if chars[index].isdigit():
            chars[index] = str((int(chars[index]) + 1) % 10)
            return "".join(chars)
    return text + "1"


def _other_department(current: str, departments: Sequence[str]) -> str:
    for name in departments:
        if name != current:
            return name
    raise SynthError("need at least two departments to plant a cross-department case")


def _case_date(case_index: int, extra_days: int = 0) -> date:
    return _DATE_BASE + timedelta(days=case_index + extra_days)


def _base_row(
    template: Mapping[str, str],
    ids: _IdFactory,
    *,
    amount: Decimal,
    invoice: str,
    paid_at: date,
    invoiced_at: date | None = None,
    **extra: str,
) -> dict[str, str]:
    row = dict(template)
    row["transaction_id"] = ids.next()
    row["dollar_amount"] = _format_money(amount)
    row["inv_num"] = invoice
    row["transaction_date"] = _fmt_date(paid_at)
    row["inv_date"] = _fmt_date(invoiced_at if invoiced_at is not None else paid_at)
    row["payment_status"] = _PAID
    method = _normalise_status(row.get("payment_method", ""))
    if method in _FORBIDDEN_METHODS or method == "":
        row["payment_method"] = "EFT"
    row.update(extra)
    return row


def _ids_of(rows: Sequence[Mapping[str, str]]) -> list[str]:
    return sorted(row["transaction_id"] for row in rows)


def _plant_true(
    label: str,
    template: Mapping[str, str],
    *,
    case_index: int,
    departments: Sequence[str],
    ids: _IdFactory,
) -> _Plant:
    amount = _isolated_amount(_parse_money(template["dollar_amount"]), case_index)
    invoice = _invoice_token(case_index)
    paid_at = _case_date(case_index)
    if label == "exact_duplicate":
        rows = [
            _base_row(template, ids, amount=amount, invoice=invoice, paid_at=paid_at),
            _base_row(template, ids, amount=amount, invoice=invoice, paid_at=paid_at),
        ]
        return _Plant(_ids_of(rows), amount, rows)
    if label == "fuzzy_vendor_duplicate":
        left_name, right_name = _fuzzy_name_pair(template["vendor_name"])
        rows = [
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                vendor_name=left_name,
            ),
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                vendor_name=right_name,
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows)
    if label == "invoice_format_variant":
        left_inv, right_inv = invoice, f"INV-{invoice}"
        rows = [
            _base_row(template, ids, amount=amount, invoice=left_inv, paid_at=paid_at),
            _base_row(template, ids, amount=amount, invoice=right_inv, paid_at=paid_at),
        ]
        return _Plant(_ids_of(rows), amount, rows)
    if label == "split_payment_duplicate":
        left_amt, right_amt = _split_amounts(amount, Decimal("0.40"))
        rows = [
            _base_row(template, ids, amount=left_amt, invoice=invoice, paid_at=paid_at),
            _base_row(
                template,
                ids,
                amount=right_amt,
                invoice=invoice,
                paid_at=_case_date(case_index, extra_days=1),
            ),
        ]
        return _Plant(_ids_of(rows), min(left_amt, right_amt), rows)
    if label == "cross_department_duplicate":
        other = _other_department(template["department_name"].strip(), departments)
        rows = [
            _base_row(template, ids, amount=amount, invoice=invoice, paid_at=paid_at),
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                department_name=other,
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows)
    if label == "overpayment_vs_invoice":
        extra = _extra_amount(amount)
        rows = [
            _base_row(template, ids, amount=amount, invoice=invoice, paid_at=paid_at),
            _base_row(
                template,
                ids,
                amount=extra,
                invoice=invoice,
                paid_at=_case_date(case_index, extra_days=2),
            ),
        ]
        return _Plant(_ids_of(rows), extra, rows)
    raise SynthError(f"unknown true duplicate type {label!r}")


def _plant_decoy(
    label: str,
    template: Mapping[str, str],
    *,
    case_index: int,
    ids: _IdFactory,
) -> _Plant:
    amount = _isolated_amount(_parse_money(template["dollar_amount"]), case_index)
    invoice = _invoice_token(case_index)
    paid_at = _case_date(case_index)
    why = _WHY[label]
    if label == "cancelled":
        rows = [
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                description="Invoice as billed",
            ),
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                description="Cancelled by issuer, do not remit",
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    if label == "line_split":
        left_amt, right_amt = _split_amounts(amount, Decimal("0.30"))
        rows = [
            _base_row(
                template,
                ids,
                amount=left_amt,
                invoice=invoice,
                paid_at=paid_at,
                description="GL distribution line 1 of 2",
                inv_line="1",
                inv_dist_line="1",
            ),
            _base_row(
                template,
                ids,
                amount=right_amt,
                invoice=invoice,
                paid_at=paid_at,
                description="GL distribution line 2 of 2",
                inv_line="2",
                inv_dist_line="2",
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    if label == "progress_payment":
        rows = [
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=_invoice_token(case_index, suffix=f"D{draw}"),
                paid_at=_case_date(case_index, extra_days=draw * 14),
                invoiced_at=_case_date(case_index, extra_days=draw * 14),
                description=f"Progress payment {draw} of {PROGRESS_DRAWS}",
            )
            for draw in range(1, PROGRESS_DRAWS + 1)
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    if label == "recurring":
        start = date(2018, 1, 15)
        rows = [
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=_invoice_token(case_index, suffix=f"M{month}"),
                paid_at=_add_months(start, month - 1),
                invoiced_at=_add_months(start, month - 1),
                description="Professional services",
            )
            for month in range(1, RECURRING_MONTHS + 1)
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    if label == "different_po":
        po_a = f"EVALPO{case_index:05d}"
        po_b = _adjacent_po(po_a)
        rows = [
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                po_num=po_a,
            ),
            _base_row(
                template,
                ids,
                amount=amount,
                invoice=invoice,
                paid_at=paid_at,
                po_num=po_b,
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    if label == "partial_pair":
        first, second = _split_amounts(amount, Decimal("0.25"))
        rows = [
            _base_row(
                template,
                ids,
                amount=first,
                invoice=invoice,
                paid_at=paid_at,
                description="Partial payment, balance outstanding",
            ),
            _base_row(
                template,
                ids,
                amount=second,
                invoice=invoice,
                paid_at=_case_date(case_index, extra_days=30),
                description="Partial payment 2, remaining balance",
            ),
        ]
        return _Plant(_ids_of(rows), amount, rows, why)
    raise SynthError(f"unknown decoy reason {label!r}")


def _atomic_write_csv(
    dest: Path,
    fieldnames: Sequence[str],
    source_rows: Sequence[Mapping[str, str]],
    planted_rows: Sequence[Mapping[str, str]],
) -> None:
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()
    with part.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fieldnames),
            lineterminator="\n",
            quoting=csv.QUOTE_ALL,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in source_rows:
            writer.writerow(row)
        for row in planted_rows:
            writer.writerow(row)
    part.replace(dest)


def _atomic_write_json(dest: Path, ground_truth: GroundTruth) -> None:
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()
    payload = ground_truth.model_dump(mode="json")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    part.write_bytes(text.encode("utf-8"))
    part.replace(dest)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m data.synth",
        description=(
            "Plant labelled true duplicates and decoys into a real ledger slice."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        required=True,
        help="RNG seed. The same seed reproduces byte-identical output.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="CSV slice to copy before planting (default: data/samples/la_sample.csv).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_GENERATED_DIR,
        help="Directory for eval_ledger.csv and ground_truth.json.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    try:
        result = generate_eval_set(args.source, args.out_dir, seed=args.seed)
    except SynthError as exc:
        log.error("%s", exc)
        return 1
    log.info("wrote %s", result.ledger_path)
    log.info("wrote %s", result.ground_truth_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
