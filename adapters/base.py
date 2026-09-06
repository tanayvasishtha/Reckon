"""Shared adapter contract and chunked CSV ingest.

Complexity: O(n) in input rows. Extra memory is O(chunk size), never the
full file. Downstream stages see only Transaction rows plus Capabilities.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd  # type: ignore[import-untyped]

from core.models import SchemaModel, Transaction

log = logging.getLogger("adapters")

DEFAULT_CHUNKSIZE = 50_000

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d")


class AdapterError(Exception):
    """A ledger file or column map cannot be read into Transaction rows."""


class Capabilities(SchemaModel):
    has_invoice_number: bool
    has_po_number: bool
    has_payment_status: bool
    has_vendor_id: bool
    has_invoice_amount: bool


@runtime_checkable
class Adapter(Protocol):
    @property
    def capabilities(self) -> Capabilities:
        """Optional Transaction fields this source can populate."""

    def iter_transactions(self, path: Path) -> Iterator[Transaction]:
        """Yield Transaction rows from a CSV path without loading it whole."""


def optional_text(value: object) -> str | None:
    """Return a stripped string, or None when the cell is empty."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def require_text(row: Mapping[str, str], column: str) -> str:
    """Return a required non-empty cell, or raise AdapterError."""
    value = optional_text(row.get(column))
    if value is None:
        raise AdapterError(
            f"required column {column!r} is empty. "
            "Fill it or drop the row before ingest."
        )
    return value


def parse_optional_date(value: object, *, field: str) -> date | None:
    """Parse an ISO-8601 or US date, or None when the cell is empty."""
    text = optional_text(value)
    if text is None:
        return None
    if "T" in text:
        try:
            return datetime.fromisoformat(text).date()
        except ValueError as exc:
            raise AdapterError(
                f"could not parse {field} date {text!r}. Use ISO-8601 or MM/DD/YYYY."
            ) from exc
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parts = time.strptime(text, fmt)
        except ValueError:
            continue
        return date(parts.tm_year, parts.tm_mon, parts.tm_mday)
    raise AdapterError(
        f"could not parse {field} date {text!r}. Use ISO-8601 or MM/DD/YYYY."
    )


def parse_money(value: object, *, field: str) -> Decimal:
    """Parse a monetary cell as Decimal. Never converts through float."""
    text = optional_text(value)
    if text is None:
        raise AdapterError(f"{field} is empty. Every kept row needs a monetary amount.")
    normalised = text.replace(",", "").replace("$", "")
    try:
        return Decimal(normalised)
    except InvalidOperation as exc:
        raise AdapterError(
            f"could not parse {field} amount {text!r} as a decimal."
        ) from exc


def parse_optional_money(value: object, *, field: str) -> Decimal | None:
    """Parse a monetary cell, or None when the cell is empty."""
    if optional_text(value) is None:
        return None
    return parse_money(value, field=field)


def build_transaction(
    *,
    transaction_id: str,
    vendor_name_raw: str,
    amount: Decimal,
    vendor_id: str | None = None,
    invoice_number_raw: str | None = None,
    invoice_date: date | None = None,
    payment_date: date | None = None,
    invoice_amount: Decimal | None = None,
    po_number: str | None = None,
    payment_method: str | None = None,
    payment_status: str | None = None,
    department: str | None = None,
    description: str | None = None,
) -> Transaction:
    """Build a line-grain Transaction. Canonical fields stay unset."""
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=vendor_id,
        vendor_canonical=None,
        invoice_number_raw=invoice_number_raw,
        invoice_canonical=None,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=amount,
        invoice_amount=invoice_amount,
        po_number=po_number,
        payment_method=payment_method,
        payment_status=payment_status,
        department=department,
        description=description,
        line_count=1,
        distinct_line_amounts=[amount],
    )


def _require_columns(columns: Iterable[object], required: Sequence[str]) -> None:
    present = {str(name) for name in columns}
    missing = [name for name in required if name not in present]
    if missing:
        raise AdapterError(
            "CSV is missing columns: "
            + ", ".join(missing)
            + ". Check the file header or the column map."
        )


def _stringify_row(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise AdapterError("internal error: expected a CSV row mapping")
    row: dict[str, str] = {}
    for key, value in raw.items():
        row[str(key)] = "" if value is None else str(value)
    return row


def _is_blank_row(row: Mapping[str, str]) -> bool:
    return not any(value.strip() for value in row.values())


def iter_csv_rows(
    path: Path,
    *,
    encoding: str,
    required_columns: Sequence[str],
    chunksize: int | None = None,
) -> Iterator[dict[str, str]]:
    """Yield CSV rows as string mappings, reading `chunksize` rows at a time."""
    if not path.is_file():
        raise AdapterError(
            f"ledger not found: {path}. Provide a path to an existing CSV file."
        )
    size = DEFAULT_CHUNKSIZE if chunksize is None else chunksize
    if size < 1:
        raise AdapterError("chunksize must be a positive integer")
    log.debug("csv_open path=%s encoding=%s chunksize=%s", path, encoding, size)
    checked_header = False
    try:
        reader = pd.read_csv(
            path,
            encoding=encoding,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            chunksize=size,
        )
        for chunk in reader:
            if not checked_header:
                _require_columns(chunk.columns, required_columns)
                checked_header = True
            records = chunk.to_dict(orient="records")
            if not isinstance(records, list):
                raise AdapterError("internal error: expected a list of CSV rows")
            for raw in records:
                row = _stringify_row(raw)
                if _is_blank_row(row):
                    continue
                yield row
    except AdapterError:
        raise
    except UnicodeDecodeError as exc:
        raise AdapterError(
            f"could not decode {path} as {encoding}. "
            "Set the encoding this file actually uses."
        ) from exc
    except (
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
        OSError,
    ) as exc:
        raise AdapterError(
            f"could not read CSV {path}: {exc}. Check that the file is a readable CSV."
        ) from exc
