"""YAML-mapped adapter for any other CSV ledger.

Time is O(n) in input rows. Extra memory is O(chunk size), never the
full file. Capabilities are derived from which optional Transaction
fields the column map provides.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from adapters.base import (
    AdapterError,
    Capabilities,
    build_transaction,
    iter_csv_rows,
    optional_text,
    parse_money,
    parse_optional_date,
    parse_optional_money,
)
from core.models import Transaction

log = logging.getLogger("adapters.generic")

REQUIRED_FIELDS = ("transaction_id", "vendor_name_raw", "amount")
OPTIONAL_FIELDS = (
    "vendor_id",
    "invoice_number_raw",
    "invoice_date",
    "payment_date",
    "invoice_amount",
    "po_number",
    "payment_method",
    "payment_status",
    "department",
    "description",
)
ALLOWED_FIELDS = frozenset(REQUIRED_FIELDS + OPTIONAL_FIELDS)


def _capabilities_from_columns(columns: Mapping[str, str]) -> Capabilities:
    mapped = set(columns)
    return Capabilities(
        has_invoice_number="invoice_number_raw" in mapped,
        has_po_number="po_number" in mapped,
        has_payment_status="payment_status" in mapped,
        has_vendor_id="vendor_id" in mapped,
        has_invoice_amount="invoice_amount" in mapped,
    )


def load_column_map(path: Path) -> tuple[str, dict[str, str]]:
    """Load encoding and Transaction-field-to-CSV-column mapping from YAML."""
    if not path.is_file():
        raise AdapterError(
            f"column map not found: {path}. Provide a YAML map path. "
            "See adapters/maps/example.yaml."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AdapterError(
            f"column map {path} is not valid YAML. Fix the syntax and retry. "
            "See adapters/maps/example.yaml."
        ) from exc
    except OSError as exc:
        raise AdapterError(f"could not read column map {path}: {exc}.") from exc
    if not isinstance(raw, dict):
        raise AdapterError(
            f"column map {path} must be a mapping with 'columns'. "
            "See adapters/maps/example.yaml."
        )
    encoding_raw = raw.get("encoding", "utf-8")
    if not isinstance(encoding_raw, str) or encoding_raw.strip() == "":
        raise AdapterError(f"column map {path} encoding must be a non-empty string.")
    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, dict) or not columns_raw:
        raise AdapterError(
            f"column map {path} is missing 'columns'. See adapters/maps/example.yaml."
        )
    columns: dict[str, str] = {}
    for key, value in columns_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise AdapterError(
                f"column map {path} entries must map field names to "
                "CSV column names as strings."
            )
        field = key.strip()
        source = value.strip()
        if field == "" or source == "":
            raise AdapterError(f"column map {path} has an empty field or column name.")
        columns[field] = source
    unknown = sorted(name for name in columns if name not in ALLOWED_FIELDS)
    if unknown:
        raise AdapterError(
            f"column map {path} has unknown Transaction fields: "
            + ", ".join(unknown)
            + ". See adapters/maps/example.yaml."
        )
    missing = [name for name in REQUIRED_FIELDS if name not in columns]
    if missing:
        raise AdapterError(
            f"column map {path} is missing required fields: "
            + ", ".join(missing)
            + ". See adapters/maps/example.yaml."
        )
    return encoding_raw.strip(), columns


def _row_to_transaction(
    row: Mapping[str, str], columns: Mapping[str, str]
) -> Transaction:
    transaction_id = optional_text(row.get(columns["transaction_id"]))
    vendor_name_raw = optional_text(row.get(columns["vendor_name_raw"]))
    if transaction_id is None or vendor_name_raw is None:
        raise AdapterError(
            "required fields transaction_id and vendor_name_raw must be "
            "non-empty on every row."
        )
    amount = parse_money(row.get(columns["amount"]), field="amount")

    vendor_id: str | None = None
    invoice_number_raw: str | None = None
    invoice_date = None
    payment_date = None
    invoice_amount = None
    po_number: str | None = None
    payment_method: str | None = None
    payment_status: str | None = None
    department: str | None = None
    description: str | None = None

    if "vendor_id" in columns:
        vendor_id = optional_text(row.get(columns["vendor_id"]))
    if "invoice_number_raw" in columns:
        invoice_number_raw = optional_text(row.get(columns["invoice_number_raw"]))
    if "invoice_date" in columns:
        invoice_date = parse_optional_date(
            row.get(columns["invoice_date"]), field="invoice_date"
        )
    if "payment_date" in columns:
        payment_date = parse_optional_date(
            row.get(columns["payment_date"]), field="payment_date"
        )
    if "invoice_amount" in columns:
        invoice_amount = parse_optional_money(
            row.get(columns["invoice_amount"]), field="invoice_amount"
        )
    if "po_number" in columns:
        po_number = optional_text(row.get(columns["po_number"]))
    if "payment_method" in columns:
        payment_method = optional_text(row.get(columns["payment_method"]))
    if "payment_status" in columns:
        payment_status = optional_text(row.get(columns["payment_status"]))
    if "department" in columns:
        department = optional_text(row.get(columns["department"]))
    if "description" in columns:
        description = optional_text(row.get(columns["description"]))

    return build_transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=vendor_id,
        invoice_number_raw=invoice_number_raw,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=amount,
        invoice_amount=invoice_amount,
        po_number=po_number,
        payment_method=payment_method,
        payment_status=payment_status,
        department=department,
        description=description,
    )


class GenericAdapter:
    """Map an arbitrary CSV onto Transaction using a YAML column map."""

    def __init__(self, map_path: Path) -> None:
        self._map_path = map_path
        self._encoding, self._columns = load_column_map(map_path)
        self._capabilities = _capabilities_from_columns(self._columns)

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def iter_transactions(self, path: Path) -> Iterator[Transaction]:
        required_columns = tuple(dict.fromkeys(self._columns.values()))
        rows_in = 0
        rows_out = 0
        started = time.perf_counter()
        try:
            for row in iter_csv_rows(
                path,
                encoding=self._encoding,
                required_columns=required_columns,
            ):
                rows_in += 1
                transaction = _row_to_transaction(row, self._columns)
                rows_out += 1
                yield transaction
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.info(
                "adapter=generic rows_in=%s rows_out=%s elapsed_ms=%s map=%s",
                rows_in,
                rows_out,
                elapsed_ms,
                self._map_path,
            )
