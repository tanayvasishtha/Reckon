from __future__ import annotations

import pytest

from core.normalize import canonical_invoice, canonical_vendor

VENDOR_CASES: list[tuple[str | None, str | None]] = [
    ("W W GRAINGER INC", "WWGRAINGER"),
    ("W. W. GRAINGER INC.", "WWGRAINGER"),
    ("W. W. GRAINGER, INC.", "WWGRAINGER"),
    ("W.W. GRAINGER INC.", "WWGRAINGER"),
    ("WW GRAINGER INC", "WWGRAINGER"),
    ("  W  W   GRAINGER  INC  ", "WWGRAINGER"),
    ("ww grainger inc", "WWGRAINGER"),
    ("MC MASTER-CARR SUPPLY COMPANY", "MCMASTERCARRSUPPLY"),
    ("MCMASTER - CARR SUPPLY", "MCMASTERCARRSUPPLY"),
    ("MCMASTER CARR SUPPLY CO", "MCMASTERCARRSUPPLY"),
    ("MCMASTER-CARR SUPPLY COMPANY", "MCMASTERCARRSUPPLY"),
    ("A T & T", "ATT"),
    ("AT & T", "ATT"),
    ("AT&T", "ATT"),
    ("AT&T CORP", "ATT"),
    ("AT&T CORPORATION", "ATT"),
    ("AT&T***", "ATT"),
    ("AT&T   ", "ATT"),
    ("B & H PHOTO AND VIDEO", "BHPHOTOVIDEO"),
    ("B & H PHOTO VIDEO", "BHPHOTOVIDEO"),
    ("B & H PHOTO VIDEO INC", "BHPHOTOVIDEO"),
    ("B & H PHOTO-VIDEO INC.", "BHPHOTOVIDEO"),
    ("B&H PHOTO VIDEO", "BHPHOTOVIDEO"),
    ("JOHNSON CONTROLS INC", "JOHNSONCONTROLS"),
    ("JOHNSON CONTROLS SECURITY", "JOHNSONCONTROLSSECURITY"),
    ("AMERICAN EXPRESS", "AMERICANEXPRESS"),
    ("AMERICAN EXPRESS TRAVEL", "AMERICANEXPRESSTRAVEL"),
    ("THE ACME COMPANY", "ACME"),
    ("Acme LLC", "ACME"),
    ("Acme Ltd.", "ACME"),
    ("Acme LP", "ACME"),
    ("Acme LLP", "ACME"),
    ("VANCE *FRIED", "VANCEFRIED"),
    (None, None),
    ("", None),
    ("   ***  ", None),
]

INVOICE_CASES: list[tuple[str | None, str | None]] = [
    ("INV-0042", "42"),
    ("42", "42"),
    ("INV42", "42"),
    ("inv-0042", "42"),
    ("INV0042", "42"),
    ("INV-42", "42"),
    ("0042", "42"),
    ("INV-00-42", "42"),
    ("INV 0042", "42"),
    ("INV/0042", "42"),
    (None, None),
    ("", None),
    ("   ", None),
    ("INV", None),
    ("000", "0"),
    ("42-A", "42A"),
    ("INVOICE-0042", "INVOICE0042"),
]

GRAINGER_GROUP = (
    "W W GRAINGER INC",
    "W. W. GRAINGER INC.",
    "W. W. GRAINGER, INC.",
    "W.W. GRAINGER INC.",
)
MCMASTER_GROUP = (
    "MC MASTER-CARR SUPPLY COMPANY",
    "MCMASTER - CARR SUPPLY",
    "MCMASTER CARR SUPPLY CO",
    "MCMASTER-CARR SUPPLY COMPANY",
)
ATT_GROUP = (
    "A T & T",
    "AT & T",
    "AT&T",
    "AT&T CORP",
    "AT&T CORPORATION",
)
BH_GROUP = (
    "B & H PHOTO AND VIDEO",
    "B & H PHOTO VIDEO",
    "B & H PHOTO VIDEO INC",
    "B & H PHOTO-VIDEO INC.",
)


def _vendor_case_id(raw: str | None) -> str:
    if raw is None:
        return "vendor-none"
    if raw == "":
        return "vendor-empty"
    return raw.strip() or "vendor-whitespace"


def _invoice_case_id(raw: str | None) -> str:
    if raw is None:
        return "invoice-none"
    if raw == "":
        return "invoice-empty"
    return raw.strip() or "invoice-whitespace"


def test_vendor_table_covers_contract() -> None:
    assert len(VENDOR_CASES) >= 25
    raw_names = {raw for raw, _expected in VENDOR_CASES}
    for group in (GRAINGER_GROUP, MCMASTER_GROUP, ATT_GROUP, BH_GROUP):
        assert set(group) <= raw_names
    assert "JOHNSON CONTROLS INC" in raw_names
    assert "JOHNSON CONTROLS SECURITY" in raw_names
    assert "AMERICAN EXPRESS" in raw_names
    assert "AMERICAN EXPRESS TRAVEL" in raw_names


def test_invoice_table_covers_contract() -> None:
    assert len(INVOICE_CASES) >= 10
    raw_numbers = {raw for raw, _expected in INVOICE_CASES}
    assert {"INV-0042", "42", "INV42"} <= raw_numbers


@pytest.mark.parametrize(
    ("raw", "expected"),
    VENDOR_CASES,
    ids=[_vendor_case_id(raw) for raw, _expected in VENDOR_CASES],
)
def test_canonical_vendor_table(raw: str | None, expected: str | None) -> None:
    assert canonical_vendor(raw) == expected
    if expected is None:
        assert canonical_vendor(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    INVOICE_CASES,
    ids=[_invoice_case_id(raw) for raw, _expected in INVOICE_CASES],
)
def test_canonical_invoice_table(raw: str | None, expected: str | None) -> None:
    assert canonical_invoice(raw) == expected
    if expected is None:
        assert canonical_invoice(raw) is None


@pytest.mark.parametrize(
    "group",
    [GRAINGER_GROUP, MCMASTER_GROUP, ATT_GROUP, BH_GROUP],
    ids=["grainger", "mcmaster", "att", "bh-photo"],
)
def test_vendor_collapse_groups_share_one_key(group: tuple[str, ...]) -> None:
    keys = {canonical_vendor(name) for name in group}
    assert len(keys) == 1
    assert None not in keys


def test_distinct_vendors_do_not_collapse() -> None:
    assert canonical_vendor("JOHNSON CONTROLS INC") != canonical_vendor(
        "JOHNSON CONTROLS SECURITY"
    )
    assert canonical_vendor("AMERICAN EXPRESS") != canonical_vendor(
        "AMERICAN EXPRESS TRAVEL"
    )


def test_invoice_prefix_and_zero_variants_match() -> None:
    assert canonical_invoice("INV-0042") == canonical_invoice("42")
    assert canonical_invoice("INV42") == canonical_invoice("42")
