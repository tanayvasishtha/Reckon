"""Los Angeles Checkbook adapter.

Maps Checkbook L.A. distribution-line CSV columns onto Transaction.
Time is O(n) in input rows. Extra memory is O(chunk size), never the
full file. All optional-field capabilities are true.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from adapters.base import (
    Capabilities,
    build_transaction,
    iter_csv_rows,
    optional_text,
    parse_money,
    parse_optional_date,
    require_text,
)
from core.models import Transaction

log = logging.getLogger("adapters.la")

ENCODING = "utf-8"

_SOURCE_COLUMNS = (
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

_CAPABILITIES = Capabilities(
    has_invoice_number=True,
    has_po_number=True,
    has_payment_status=True,
    has_vendor_id=True,
    has_invoice_amount=True,
)


def _row_to_transaction(row: Mapping[str, str]) -> Transaction:
    amount = parse_money(row.get("dollar_amount"), field="dollar_amount")
    return build_transaction(
        transaction_id=require_text(row, "transaction_id"),
        vendor_name_raw=require_text(row, "vendor_name"),
        vendor_id=optional_text(row.get("vendor_id")),
        invoice_number_raw=optional_text(row.get("inv_num")),
        invoice_date=parse_optional_date(row.get("inv_date"), field="inv_date"),
        payment_date=parse_optional_date(
            row.get("transaction_date"), field="transaction_date"
        ),
        amount=amount,
        invoice_amount=amount,
        po_number=optional_text(row.get("po_num")),
        payment_method=optional_text(row.get("payment_method")),
        payment_status=optional_text(row.get("payment_status")),
        department=optional_text(row.get("department_name")),
        description=optional_text(row.get("description")),
    )


class LAAdapter:
    """Map Checkbook L.A. CSV rows onto Transaction."""

    @property
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    def iter_transactions(self, path: Path) -> Iterator[Transaction]:
        rows_in = 0
        rows_out = 0
        started = time.perf_counter()
        try:
            for row in iter_csv_rows(
                path, encoding=ENCODING, required_columns=_SOURCE_COLUMNS
            ):
                rows_in += 1
                transaction = _row_to_transaction(row)
                rows_out += 1
                yield transaction
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "adapter=la rows_in=%s rows_out=%s elapsed_ms=%s",
                rows_in,
                rows_out,
                elapsed_ms,
            )
