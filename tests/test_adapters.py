from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from adapters.base import (
    DEFAULT_CHUNKSIZE,
    Adapter,
    AdapterError,
    Capabilities,
    iter_csv_rows,
)
from adapters.generic import GenericAdapter
from adapters.la import LAAdapter
from adapters.oklahoma import OklahomaAdapter
from core.models import Transaction

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
LA_CSV = FIXTURES / "la.csv"
OK_CSV = FIXTURES / "oklahoma.csv"
GENERIC_CSV = FIXTURES / "generic.csv"
EXAMPLE_MAP = ROOT / "adapters" / "maps" / "example.yaml"
PARTIAL_MAP = FIXTURES / "generic_partial.yaml"


def _collect(adapter: Adapter, path: Path) -> list[Transaction]:
    rows = list(adapter.iter_transactions(path))
    assert all(isinstance(row, Transaction) for row in rows)
    return rows


def test_adapters_satisfy_protocol() -> None:
    assert isinstance(LAAdapter(), Adapter)
    assert isinstance(OklahomaAdapter(), Adapter)
    assert isinstance(GenericAdapter(EXAMPLE_MAP), Adapter)


def test_la_adapter_maps_checkbook_columns() -> None:
    adapter = LAAdapter()
    assert adapter.capabilities == Capabilities(
        has_invoice_number=True,
        has_po_number=True,
        has_payment_status=True,
        has_vendor_id=True,
        has_invoice_amount=True,
    )
    rows = _collect(adapter, LA_CSV)
    assert len(rows) == 2
    first = rows[0]
    assert first.transaction_id == "EFT26240000023779"
    assert first.vendor_name_raw == "LOS ANGELES LGBT CENTER"
    assert first.vendor_id == "VC0000020466"
    assert first.invoice_number_raw == "0224395020570"
    assert first.invoice_date == date(2024, 6, 11)
    assert first.payment_date == date(2024, 6, 17)
    assert first.amount == Decimal("101929.15")
    assert first.invoice_amount == Decimal("101929.15")
    assert type(first.amount) is Decimal
    assert type(first.invoice_amount) is Decimal
    assert first.po_number == "SC02CO23141827Y"
    assert first.payment_method == "EFT"
    assert first.payment_status == "PAID"
    assert first.department == "AGING"
    assert first.description == "CF23 05_NOV 23 NI 3C2"
    assert first.line_count == 1
    assert first.distinct_line_amounts == [Decimal("101929.15")]
    assert first.vendor_canonical is None
    assert first.invoice_canonical is None
    assert rows[1].transaction_id == "AD26240000120268"
    assert rows[1].payment_method == "CHECK"
    assert rows[1].amount == Decimal("76861.00")


def test_oklahoma_fixture_is_cp1252_not_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        OK_CSV.read_text(encoding="utf-8")
    text = OK_CSV.read_text(encoding="cp1252")
    assert "CAFÉ SUPPLY LLC" in text
    assert "PROTECTED INFORMATION" in text
    assert "00012345" in text
    assert "01901078" in text


def test_oklahoma_adapter_decodes_filters_and_keeps_leading_zeros() -> None:
    adapter = OklahomaAdapter()
    assert adapter.capabilities == Capabilities(
        has_invoice_number=False,
        has_po_number=True,
        has_payment_status=False,
        has_vendor_id=False,
        has_invoice_amount=False,
    )
    rows = _collect(adapter, OK_CSV)
    assert len(rows) == 2
    vendors = {row.vendor_name_raw for row in rows}
    assert vendors == {"LABXPRESS LLC", "CAFÉ SUPPLY LLC"}
    assert "PROTECTED INFORMATION" not in vendors
    by_id = {row.transaction_id: row for row in rows}
    assert set(by_id) == {"01901078", "00012345"}
    cafe = by_id["00012345"]
    assert cafe.vendor_name_raw == "CAFÉ SUPPLY LLC"
    assert cafe.amount == Decimal("100.50")
    assert type(cafe.amount) is Decimal
    assert cafe.invoice_number_raw is None
    assert cafe.invoice_amount is None
    assert cafe.vendor_id is None
    assert cafe.payment_status is None
    assert cafe.invoice_date == date(2020, 7, 1)
    assert cafe.payment_date == date(2020, 7, 15)
    assert cafe.department == "OKLAHOMA STATE UNIVERSITY"
    lab = by_id["01901078"]
    assert lab.po_number == "PO-4411"
    assert lab.amount == Decimal("8377.90")
    assert lab.description == "Collection Agencies"


def test_oklahoma_forces_string_dtype_cp1252_and_chunksize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    real_read_csv = pd.read_csv

    def wrapped(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_read_csv(*args, **kwargs)

    monkeypatch.setattr("adapters.base.pd.read_csv", wrapped)
    list(OklahomaAdapter().iter_transactions(OK_CSV))
    assert captured["encoding"] == "cp1252"
    assert captured["dtype"] is str
    assert captured["chunksize"] == DEFAULT_CHUNKSIZE
    assert captured["keep_default_na"] is False
    assert captured["na_filter"] is False


def test_generic_adapter_round_trips_csv_through_example_yaml() -> None:
    adapter = GenericAdapter(EXAMPLE_MAP)
    assert adapter.capabilities == Capabilities(
        has_invoice_number=True,
        has_po_number=True,
        has_payment_status=True,
        has_vendor_id=True,
        has_invoice_amount=True,
    )
    rows = _collect(adapter, GENERIC_CSV)
    assert len(rows) == 2
    first = rows[0]
    assert first.transaction_id == "G-1"
    assert first.vendor_name_raw == "Acme LLC"
    assert first.vendor_id == "V-9"
    assert first.invoice_number_raw == "INV-100"
    assert first.invoice_date == date(2024, 1, 15)
    assert first.payment_date == date(2024, 1, 20)
    assert first.amount == Decimal("1250.50")
    assert first.invoice_amount == Decimal("1250.50")
    assert type(first.amount) is Decimal
    assert first.po_number == "PO-9"
    assert first.payment_method == "ACH"
    assert first.payment_status == "paid"
    assert first.department == "Public Works"
    assert first.description == "Asphalt repair"
    assert first.line_count == 1
    second = rows[1]
    assert second.transaction_id == "G-2"
    assert second.amount == Decimal("10.00")
    assert second.invoice_amount == Decimal("12.00")
    assert second.department == "Parks"


def test_generic_capabilities_follow_omitted_map_fields() -> None:
    adapter = GenericAdapter(PARTIAL_MAP)
    assert adapter.capabilities == Capabilities(
        has_invoice_number=False,
        has_po_number=False,
        has_payment_status=False,
        has_vendor_id=False,
        has_invoice_amount=False,
    )
    rows = _collect(adapter, GENERIC_CSV)
    assert len(rows) == 2
    assert rows[0].invoice_number_raw is None
    assert rows[0].vendor_id is None
    assert rows[0].invoice_amount is None
    assert rows[0].amount == Decimal("1250.50")


def test_chunked_read_matches_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adapters.base.DEFAULT_CHUNKSIZE", 1)
    rows = _collect(LAAdapter(), LA_CSV)
    assert [row.transaction_id for row in rows] == [
        "EFT26240000023779",
        "AD26240000120268",
    ]


def test_iter_csv_rows_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    with pytest.raises(AdapterError, match="ledger not found"):
        list(iter_csv_rows(missing, encoding="utf-8", required_columns=["id"]))


def test_la_missing_column_is_adapter_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("transaction_id,vendor_name\nT1,Acme\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="missing columns"):
        list(LAAdapter().iter_transactions(path))


def test_generic_unknown_field_is_adapter_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "encoding: utf-8\n"
        "columns:\n"
        "  transaction_id: a\n"
        "  vendor_name_raw: b\n"
        "  amount: c\n"
        "  vendor_canonical: d\n",
        encoding="utf-8",
    )
    with pytest.raises(AdapterError, match="unknown Transaction fields"):
        GenericAdapter(path)


def test_generic_missing_required_field_is_adapter_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "encoding: utf-8\ncolumns:\n  transaction_id: a\n  vendor_name_raw: b\n",
        encoding="utf-8",
    )
    with pytest.raises(AdapterError, match="missing required fields"):
        GenericAdapter(path)


def test_generic_missing_map_is_adapter_error(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="column map not found"):
        GenericAdapter(tmp_path / "missing.yaml")
