"""Oklahoma vendor-payments adapter.

Maps the state vendor-payments CSV onto Transaction. Time is O(n) in
input rows. Extra memory is O(chunk size), never the full file.

This source has no invoice number, only INVOICE_DT, so
has_invoice_number is false. Files are cp1252. Every column is forced
to string so voucher ids keep leading zeros. Rows whose vendor is
PROTECTED INFORMATION are dropped.
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

log = logging.getLogger("adapters.oklahoma")

ENCODING = "cp1252"
PROTECTED_VENDOR = "PROTECTED INFORMATION"

_SOURCE_COLUMNS = (
    "AGENCYNAME",
    "VENDOR_NAME",
    "VOUCHER_ID",
    "INVOICE_DT",
    "PYMNT_AMT",
    "PYMNT_DT",
    "PO_ID",
    "ITEM_DESCRIPTION",
)

_CAPABILITIES = Capabilities(
    has_invoice_number=False,
    has_po_number=True,
    has_payment_status=False,
    has_vendor_id=False,
    has_invoice_amount=False,
)


def _row_to_transaction(row: Mapping[str, str]) -> Transaction | None:
    vendor_name = require_text(row, "VENDOR_NAME")
    if vendor_name == PROTECTED_VENDOR:
        return None
    amount = parse_money(row.get("PYMNT_AMT"), field="PYMNT_AMT")
    return build_transaction(
        transaction_id=require_text(row, "VOUCHER_ID"),
        vendor_name_raw=vendor_name,
        vendor_id=None,
        invoice_number_raw=None,
        invoice_date=parse_optional_date(row.get("INVOICE_DT"), field="INVOICE_DT"),
        payment_date=parse_optional_date(row.get("PYMNT_DT"), field="PYMNT_DT"),
        amount=amount,
        invoice_amount=None,
        po_number=optional_text(row.get("PO_ID")),
        payment_method=None,
        payment_status=None,
        department=optional_text(row.get("AGENCYNAME")),
        description=optional_text(row.get("ITEM_DESCRIPTION")),
    )


class OklahomaAdapter:
    """Map Oklahoma vendor-payments CSV rows onto Transaction."""

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
                if transaction is None:
                    continue
                rows_out += 1
                yield transaction
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "adapter=oklahoma rows_in=%s rows_out=%s elapsed_ms=%s",
                rows_in,
                rows_out,
                elapsed_ms,
            )
