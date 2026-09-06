"""Generate candidate duplicate-payment pairs by hash-bucketed blocking.

Stage 3 of the pipeline. Transactions are grouped by a composite key and
compared only inside a bucket. Any bucket larger than Settings.blocking_bucket_cap
(default 200) is split by a secondary key; a secondary group that is still
over the cap is chunked by sorted transaction_id so no comparison set exceeds
the cap. A high-volume vendor cannot blow up the run.

Complexity: O(R) in the number of transactions R, with cap C treated as a
constant (default 200). Each row is hashed into a constant number of blocking
keys (one primary key per enabled signal) in amortized O(1). Pairwise work
inside a bucket of size S <= C is O(S^2) = O(1). Across R rows the number of
buckets is at most R, so total work is O(R * C^2) = O(R). Extra memory is
O(R) to hold the hash tables. Naive all-pairs comparison is O(R^2) and is
not used: 700,000 rows would be 2.4e11 pairs and would never finish.

Output is deterministic. Candidates are sorted by (signal, transaction ids).
The same input always produces byte-identical JSON.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from typing import Protocol

from core.models import Candidate, Signal, Transaction
from core.settings import Settings

log = logging.getLogger("core.blocking")

STAGE_NAME = "blocking"

Key = tuple[object, ...]
KeyFn = Callable[[Transaction], Key | None]
SecondaryFn = Callable[[Transaction], Key]
AcceptFn = Callable[[Transaction, Transaction], bool]
RiskFn = Callable[[Transaction, Transaction], Decimal | None]


class BlockingError(Exception):
    """Stage 3 failed to generate candidate pairs."""


class BlockingCapabilities(Protocol):
    """Adapter flags that gate which blocking signals may run."""

    has_invoice_number: bool
    has_invoice_amount: bool


@dataclass(frozen=True, slots=True)
class BlockingStats:
    """Work counters proving the stage stays linear in row count."""

    rows_in: int
    max_bucket: int
    pair_comparisons: int
    elapsed_ms: int


@dataclass(frozen=True, slots=True)
class _SignalSpec:
    signal: Signal
    primary: KeyFn
    secondary: SecondaryFn
    accept: AcceptFn
    amount_at_risk: RiskFn


def block(
    transactions: Iterable[Transaction],
    capabilities: BlockingCapabilities,
    settings: Settings | None = None,
    *,
    run_id: str | None = None,
) -> list[Candidate]:
    """Return candidate pairs for the enabled signals, sorted stably."""
    candidates, _stats = block_with_stats(
        transactions, capabilities, settings, run_id=run_id
    )
    return candidates


def block_with_stats(
    transactions: Iterable[Transaction],
    capabilities: BlockingCapabilities,
    settings: Settings | None = None,
    *,
    run_id: str | None = None,
) -> tuple[list[Candidate], BlockingStats]:
    """Like block, also returning bucket-cap and comparison counters."""
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    cfg = Settings() if settings is None else settings
    cap = cfg.blocking_bucket_cap
    if cap < 1:
        raise BlockingError(
            f"blocking_bucket_cap must be a positive integer, got {cap}"
        )

    started = time.perf_counter()
    log.info("blocking start run_id=%s", active_run_id)

    rows = transactions if isinstance(transactions, list) else list(transactions)
    rows_in = len(rows)
    specs = _signal_specs(capabilities)
    candidates: list[Candidate] = []
    max_bucket = 0
    pair_comparisons = 0

    for spec in specs:
        groups = _group_by(rows, spec.primary)
        for members in groups.values():
            for bucket in _capped_buckets(members, spec.secondary, cap):
                size = len(bucket)
                max_bucket = max(max_bucket, size)
                if size < 2:
                    continue
                pair_comparisons += size * (size - 1) // 2
                candidates.extend(_pairs_from_bucket(bucket, spec))

    candidates.sort(
        key=lambda item: (
            item.signal.value,
            item.transaction_ids[0],
            item.transaction_ids[1],
            item.candidate_id,
        )
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stats = BlockingStats(
        rows_in=rows_in,
        max_bucket=max_bucket,
        pair_comparisons=pair_comparisons,
        elapsed_ms=elapsed_ms,
    )
    log.info(
        "blocking done rows_in=%s rows_out=%s max_bucket=%s "
        "pair_comparisons=%s elapsed_ms=%s run_id=%s",
        stats.rows_in,
        len(candidates),
        stats.max_bucket,
        stats.pair_comparisons,
        stats.elapsed_ms,
        active_run_id,
    )
    return candidates, stats


def _signal_specs(capabilities: BlockingCapabilities) -> tuple[_SignalSpec, ...]:
    use_invoice = capabilities.has_invoice_number
    specs: list[_SignalSpec] = []
    if use_invoice:
        specs.append(
            _SignalSpec(
                signal=Signal.exact_duplicate,
                primary=_key_vendor_invoice_amount,
                secondary=_secondary_date,
                accept=_accept_all,
                amount_at_risk=_min_amount,
            )
        )
    specs.append(
        _SignalSpec(
            signal=Signal.fuzzy_vendor,
            primary=_key_invoice_amount if use_invoice else _key_date_amount,
            secondary=_secondary_date,
            accept=_accept_different_vendor,
            amount_at_risk=_min_amount,
        )
    )
    if use_invoice:
        specs.append(
            _SignalSpec(
                signal=Signal.invoice_variant,
                primary=_key_vendor_amount,
                secondary=_secondary_date,
                accept=_accept_different_invoice,
                amount_at_risk=_min_amount,
            )
        )
    specs.append(
        _SignalSpec(
            signal=Signal.split_payment,
            primary=_key_vendor_invoice if use_invoice else _key_vendor_date,
            secondary=_secondary_date,
            accept=_accept_all,
            amount_at_risk=_min_amount,
        )
    )
    specs.append(
        _SignalSpec(
            signal=Signal.cross_department,
            primary=(
                _key_vendor_invoice_amount if use_invoice else _key_vendor_date_amount
            ),
            secondary=_secondary_date,
            accept=_accept_different_department,
            amount_at_risk=_min_amount,
        )
    )
    if capabilities.has_invoice_amount:
        specs.append(
            _SignalSpec(
                signal=Signal.overpayment_vs_invoice,
                primary=_key_vendor_invoice if use_invoice else _key_vendor_date,
                secondary=_secondary_date,
                accept=_accept_overpayment,
                amount_at_risk=_overpay_excess,
            )
        )
    return tuple(specs)


def _group_by(
    rows: Sequence[Transaction], key_fn: KeyFn
) -> dict[Key, list[Transaction]]:
    groups: dict[Key, list[Transaction]] = {}
    for tx in rows:
        key = key_fn(tx)
        if key is None:
            continue
        bucket = groups.get(key)
        if bucket is None:
            groups[key] = [tx]
        else:
            bucket.append(tx)
    return groups


def _capped_buckets(
    members: Sequence[Transaction],
    secondary: SecondaryFn,
    cap: int,
) -> Iterator[list[Transaction]]:
    """Yield subsets of members, each of length at most cap."""
    if len(members) <= cap:
        yield sorted(members, key=lambda tx: tx.transaction_id)
        return
    subgroups: dict[Key, list[Transaction]] = {}
    for tx in members:
        key = secondary(tx)
        bucket = subgroups.get(key)
        if bucket is None:
            subgroups[key] = [tx]
        else:
            bucket.append(tx)
    for key in sorted(subgroups, key=_sortable_key):
        group = subgroups[key]
        if len(group) <= cap:
            yield sorted(group, key=lambda tx: tx.transaction_id)
            continue
        ordered = sorted(group, key=lambda tx: tx.transaction_id)
        for start in range(0, len(ordered), cap):
            yield ordered[start : start + cap]


def _sortable_key(key: Key) -> tuple[str, ...]:
    return tuple("" if part is None else str(part) for part in key)


def _pairs_from_bucket(
    bucket: Sequence[Transaction], spec: _SignalSpec
) -> list[Candidate]:
    out: list[Candidate] = []
    for left, right in combinations(bucket, 2):
        if left.transaction_id == right.transaction_id:
            continue
        if not spec.accept(left, right):
            continue
        risk = spec.amount_at_risk(left, right)
        if risk is None:
            continue
        out.append(_candidate(left, right, spec.signal, risk))
    return out


def _candidate(
    left: Transaction,
    right: Transaction,
    signal: Signal,
    amount_at_risk: Decimal,
) -> Candidate:
    id_a, id_b = left.transaction_id, right.transaction_id
    if id_b < id_a:
        id_a, id_b = id_b, id_a
    return Candidate(
        candidate_id=f"{signal.value}:{id_a}:{id_b}",
        transaction_ids=(id_a, id_b),
        signal=signal,
        amount_at_risk=amount_at_risk,
        stage_generated=STAGE_NAME,
    )


def _vendor_key(tx: Transaction) -> str | None:
    value = tx.vendor_canonical if tx.vendor_canonical else tx.vendor_name_raw
    stripped = value.strip()
    return stripped if stripped else None


def _invoice_key(tx: Transaction) -> str | None:
    value = tx.invoice_canonical
    if value is None or value.strip() == "":
        value = tx.invoice_number_raw
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _date_key(tx: Transaction) -> str | None:
    if tx.invoice_date is None:
        return None
    return tx.invoice_date.isoformat()


def _key_vendor_invoice_amount(tx: Transaction) -> Key | None:
    vendor = _vendor_key(tx)
    invoice = _invoice_key(tx)
    if vendor is None or invoice is None:
        return None
    return (vendor, invoice, tx.amount)


def _key_vendor_date_amount(tx: Transaction) -> Key | None:
    vendor = _vendor_key(tx)
    day = _date_key(tx)
    if vendor is None or day is None:
        return None
    return (vendor, day, tx.amount)


def _key_invoice_amount(tx: Transaction) -> Key | None:
    invoice = _invoice_key(tx)
    if invoice is None:
        return None
    return (invoice, tx.amount)


def _key_date_amount(tx: Transaction) -> Key | None:
    day = _date_key(tx)
    if day is None:
        return None
    return (day, tx.amount)


def _key_vendor_amount(tx: Transaction) -> Key | None:
    vendor = _vendor_key(tx)
    if vendor is None:
        return None
    return (vendor, tx.amount)


def _key_vendor_invoice(tx: Transaction) -> Key | None:
    vendor = _vendor_key(tx)
    invoice = _invoice_key(tx)
    if vendor is None or invoice is None:
        return None
    return (vendor, invoice)


def _key_vendor_date(tx: Transaction) -> Key | None:
    vendor = _vendor_key(tx)
    day = _date_key(tx)
    if vendor is None or day is None:
        return None
    return (vendor, day)


def _secondary_date(tx: Transaction) -> Key:
    day = _date_key(tx)
    return (day if day is not None else "",)


def _accept_all(_left: Transaction, _right: Transaction) -> bool:
    return True


def _accept_different_vendor(left: Transaction, right: Transaction) -> bool:
    return _vendor_key(left) != _vendor_key(right)


def _accept_different_invoice(left: Transaction, right: Transaction) -> bool:
    return _invoice_key(left) != _invoice_key(right)


def _accept_different_department(left: Transaction, right: Transaction) -> bool:
    return left.department != right.department


def _accept_overpayment(left: Transaction, right: Transaction) -> bool:
    return _overpay_excess(left, right) is not None


def _min_amount(left: Transaction, right: Transaction) -> Decimal:
    return min(left.amount, right.amount)


def _overpay_excess(left: Transaction, right: Transaction) -> Decimal | None:
    invoices = [
        amount
        for amount in (left.invoice_amount, right.invoice_amount)
        if amount is not None
    ]
    if not invoices:
        return None
    invoice = min(invoices[0], invoices[-1])
    excess = left.amount + right.amount - invoice
    if excess > 0:
        return excess
    return None
