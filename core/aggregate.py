"""Aggregate distribution lines onto one Transaction per payment.

Stage 1 of the pipeline. Real ledgers store one row per distribution line,
so a single invoice can appear as hundreds of rows. This stage groups by
transaction_id and emits one Transaction per group.

Amount is the Decimal sum of the group. line_count is the number of rows.
distinct_line_amounts is the sorted unique line amounts; later stages use
that list (and its length) to tell a genuine multi-line invoice from a
duplicate payment.

Identity fields such as vendor and invoice number take the first non-empty
value. If the group disagrees, the field is left empty and the disagreement
is logged so a later stage can see it.

Complexity: O(R) in the number of input rows. Each row is visited once and
hashed into its transaction_id bucket in amortized O(1). After the scan,
the U unique transaction ids are sorted in O(U log U) so output is
reproducible; U <= R and the row scan dominates. Extra memory is O(U), not
O(R): only an accumulator is kept per transaction_id, never the raw lines.
The input iterable is consumed incrementally and is never materialised.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import Transaction

log = logging.getLogger("core.aggregate")

_IDENTITY_FIELDS: tuple[str, ...] = (
    "vendor_name_raw",
    "vendor_id",
    "vendor_canonical",
    "invoice_number_raw",
    "invoice_canonical",
    "invoice_date",
    "payment_date",
    "invoice_amount",
    "po_number",
    "payment_method",
    "payment_status",
    "department",
    "description",
)


class AggregateError(Exception):
    """Stage 1 failed to collapse distribution lines onto transaction grain."""


@dataclass(slots=True)
class _Identity:
    value: object | None = None
    disagreed: bool = False

    def observe(self, incoming: object | None) -> None:
        if self.disagreed:
            return
        if _is_empty(incoming):
            return
        if _is_empty(self.value):
            self.value = incoming
            return
        if self.value != incoming:
            # Do not silently pick a value when the group is inconsistent.
            self.disagreed = True
            self.value = None


@dataclass(slots=True)
class _Group:
    transaction_id: str
    amount: Decimal
    line_count: int
    amounts: set[Decimal] = field(default_factory=set)
    identities: dict[str, _Identity] = field(default_factory=dict)

    def disagreed_fields(self) -> list[str]:
        return sorted(name for name, slot in self.identities.items() if slot.disagreed)


def aggregate(
    rows: Iterable[Transaction],
    *,
    run_id: str | None = None,
) -> list[Transaction]:
    """Collapse distribution lines onto one Transaction per transaction_id."""
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info("aggregate start run_id=%s", active_run_id)

    groups: dict[str, _Group] = {}
    rows_in = 0
    for row in rows:
        rows_in += 1
        _add_row(groups, row)

    result: list[Transaction] = []
    for key in sorted(groups):
        group = groups[key]
        disagreed = group.disagreed_fields()
        if disagreed:
            log.warning(
                "aggregate field_disagreement transaction_id=%s fields=%s run_id=%s",
                group.transaction_id,
                ",".join(disagreed),
                active_run_id,
            )
        result.append(_to_transaction(group))

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "aggregate done rows_in=%s rows_out=%s elapsed_ms=%s run_id=%s",
        rows_in,
        len(result),
        elapsed_ms,
        active_run_id,
    )
    return result


def write_jsonl(transactions: Iterable[Transaction], dest: Path) -> None:
    """Write transactions as UTF-8 JSONL with LF newlines, sorted by id."""
    ordered = sorted(transactions, key=lambda tx: tx.transaction_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="\n") as handle:
        for tx in ordered:
            handle.write(tx.model_dump_json())
            handle.write("\n")


def _add_row(groups: dict[str, _Group], row: Transaction) -> None:
    transaction_id = row.transaction_id
    if transaction_id.strip() == "":
        raise AggregateError("transaction_id is required")
    group = groups.get(transaction_id)
    if group is None:
        group = _Group(
            transaction_id=transaction_id,
            amount=Decimal(0),
            line_count=0,
        )
        groups[transaction_id] = group
    group.amount += row.amount
    group.line_count += 1
    group.amounts.add(row.amount)
    for name in _IDENTITY_FIELDS:
        slot = group.identities.get(name)
        if slot is None:
            slot = _Identity()
            group.identities[name] = slot
        slot.observe(getattr(row, name))


def _to_transaction(group: _Group) -> Transaction:
    vendor_name_raw = _as_str(group, "vendor_name_raw") or ""
    return Transaction(
        transaction_id=group.transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=_as_str(group, "vendor_id"),
        vendor_canonical=_as_str(group, "vendor_canonical"),
        invoice_number_raw=_as_str(group, "invoice_number_raw"),
        invoice_canonical=_as_str(group, "invoice_canonical"),
        invoice_date=_as_date(group, "invoice_date"),
        payment_date=_as_date(group, "payment_date"),
        amount=group.amount,
        invoice_amount=_as_decimal(group, "invoice_amount"),
        po_number=_as_str(group, "po_number"),
        payment_method=_as_str(group, "payment_method"),
        payment_status=_as_str(group, "payment_status"),
        department=_as_str(group, "department"),
        description=_as_str(group, "description"),
        line_count=group.line_count,
        distinct_line_amounts=sorted(group.amounts),
    )


def _is_empty(value: object | None) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _slot_value(group: _Group, name: str) -> object | None:
    slot = group.identities.get(name)
    if slot is None:
        return None
    return slot.value


def _as_str(group: _Group, name: str) -> str | None:
    value = _slot_value(group, name)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise AggregateError(f"{name} expected str, got {type(value).__name__}")


def _as_date(group: _Group, name: str) -> date | None:
    value = _slot_value(group, name)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    raise AggregateError(f"{name} expected date, got {type(value).__name__}")


def _as_decimal(group: _Group, name: str) -> Decimal | None:
    value = _slot_value(group, name)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    raise AggregateError(f"{name} expected Decimal, got {type(value).__name__}")
