from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from adapters.base import Capabilities
from adapters.la import LAAdapter
from adapters.oklahoma import OklahomaAdapter
from core.blocking import BlockingError, block, block_with_stats
from core.models import Candidate, Signal, Transaction
from core.settings import Settings

FULL_CAPABILITIES = Capabilities(
    has_invoice_number=True,
    has_po_number=True,
    has_payment_status=True,
    has_vendor_id=True,
    has_invoice_amount=True,
)

NO_INVOICE_CAPABILITIES = Capabilities(
    has_invoice_number=False,
    has_po_number=True,
    has_payment_status=False,
    has_vendor_id=False,
    has_invoice_amount=False,
)

NO_INVOICE_AMOUNT_CAPABILITIES = Capabilities(
    has_invoice_number=True,
    has_po_number=True,
    has_payment_status=True,
    has_vendor_id=True,
    has_invoice_amount=False,
)

FAT_DAY = date(2024, 2, 1)


@dataclass(frozen=True)
class PlantedCase:
    case_id: str
    signal: Signal
    left: Transaction
    right: Transaction


def _tx(
    transaction_id: str,
    *,
    vendor: str = "ACME",
    invoice: str | None = "INV-1",
    amount: str = "100.00",
    invoice_amount: str | None = None,
    invoice_date: date | None = date(2024, 1, 15),
    department: str | None = "AGING",
    vendor_canonical: str | None = None,
    invoice_canonical: str | None = None,
) -> Transaction:
    money = Decimal(amount)
    if invoice_amount is None:
        inv_amt: Decimal | None = money
    else:
        inv_amt = Decimal(invoice_amount)
    if invoice is None:
        inv_raw = None
        inv_can = invoice_canonical
    else:
        inv_raw = invoice
        inv_can = invoice if invoice_canonical is None else invoice_canonical
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor,
        vendor_canonical=vendor if vendor_canonical is None else vendor_canonical,
        invoice_number_raw=inv_raw,
        invoice_canonical=inv_can,
        invoice_date=invoice_date,
        payment_date=invoice_date,
        amount=money,
        invoice_amount=inv_amt,
        department=department,
        line_count=1,
        distinct_line_amounts=[money],
    )


def _fat_row(
    index: int | str,
    *,
    invoice_date: date = FAT_DAY,
    department: str | None = "AGING",
) -> Transaction:
    return _tx(
        f"fat-{index}",
        vendor="FATVENDOR",
        invoice="FATINV",
        amount="100.00",
        invoice_amount="100.00",
        invoice_date=invoice_date,
        department=department,
    )


def _planted_cases() -> list[PlantedCase]:
    cases: list[PlantedCase] = []
    day = date(2024, 1, 15)

    for i in range(1, 6):
        vendor = f"EXACT{i}"
        invoice = f"EX{i}"
        amount = f"{100 + i}.00"
        cases.append(
            PlantedCase(
                f"exact-{i}",
                Signal.exact_duplicate,
                _tx(
                    f"exact-{i}-a",
                    vendor=vendor,
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                ),
                _tx(
                    f"exact-{i}-b",
                    vendor=vendor,
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                ),
            )
        )

    for i in range(1, 6):
        invoice = f"FZ{i}"
        amount = f"{200 + i}.00"
        cases.append(
            PlantedCase(
                f"fuzzy-{i}",
                Signal.fuzzy_vendor,
                _tx(
                    f"fuzzy-{i}-a",
                    vendor=f"FUZZA{i}",
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                ),
                _tx(
                    f"fuzzy-{i}-b",
                    vendor=f"FUZZB{i}",
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                ),
            )
        )

    for i in range(1, 6):
        vendor = f"VAR{i}"
        amount = f"{300 + i}.00"
        cases.append(
            PlantedCase(
                f"variant-{i}",
                Signal.invoice_variant,
                _tx(
                    f"var-{i}-a",
                    vendor=vendor,
                    invoice=f"V{i}",
                    amount=amount,
                    invoice_date=day,
                ),
                _tx(
                    f"var-{i}-b",
                    vendor=vendor,
                    invoice=f"V{i}A",
                    amount=amount,
                    invoice_date=day,
                ),
            )
        )

    for i in range(1, 6):
        vendor = f"SPLIT{i}"
        invoice = f"SP{i}"
        cases.append(
            PlantedCase(
                f"split-{i}",
                Signal.split_payment,
                _tx(
                    f"split-{i}-a",
                    vendor=vendor,
                    invoice=invoice,
                    amount=f"{10 + i}.00",
                    invoice_amount="1000.00",
                    invoice_date=day,
                ),
                _tx(
                    f"split-{i}-b",
                    vendor=vendor,
                    invoice=invoice,
                    amount=f"{90 - i}.00",
                    invoice_amount="1000.00",
                    invoice_date=day,
                ),
            )
        )

    for i in range(1, 6):
        vendor = f"XDEPT{i}"
        invoice = f"XD{i}"
        amount = f"{400 + i}.00"
        cases.append(
            PlantedCase(
                f"xdept-{i}",
                Signal.cross_department,
                _tx(
                    f"xdept-{i}-a",
                    vendor=vendor,
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                    department="PARKS",
                ),
                _tx(
                    f"xdept-{i}-b",
                    vendor=vendor,
                    invoice=invoice,
                    amount=amount,
                    invoice_date=day,
                    department="POLICE",
                ),
            )
        )

    for i in range(1, 6):
        vendor = f"OVER{i}"
        invoice = f"OV{i}"
        inv_amt = f"{50 + i}.00"
        cases.append(
            PlantedCase(
                f"over-{i}",
                Signal.overpayment_vs_invoice,
                _tx(
                    f"over-{i}-a",
                    vendor=vendor,
                    invoice=invoice,
                    amount=f"{40 + i}.00",
                    invoice_amount=inv_amt,
                    invoice_date=day,
                ),
                _tx(
                    f"over-{i}-b",
                    vendor=vendor,
                    invoice=invoice,
                    amount=f"{30 + i}.00",
                    invoice_amount=inv_amt,
                    invoice_date=day,
                ),
            )
        )

    assert len(cases) == 30
    assert {case.signal for case in cases} == set(Signal)
    return cases


def _thirty_case_ledger() -> tuple[list[Transaction], list[PlantedCase]]:
    cases = _planted_cases()
    rows: list[Transaction] = []
    for case in cases:
        rows.append(case.left)
        rows.append(case.right)
    rows.extend(
        _tx(
            f"noise-{i}",
            vendor=f"NOISE{i}",
            invoice=f"N{i}",
            amount=f"{1000 + i}.00",
        )
        for i in range(10)
    )
    return rows, cases


def _dump(candidates: list[Candidate]) -> str:
    return "\n".join(item.model_dump_json() for item in candidates)


def _large_ledger(n: int) -> list[Transaction]:
    day = date(2024, 6, 11)
    rows: list[Transaction] = []
    construct = Transaction.model_construct
    for i in range(n):
        amount = Decimal(i)
        rows.append(
            construct(
                transaction_id=f"t{i}",
                vendor_name_raw="Vendor",
                vendor_id=None,
                vendor_canonical=f"V{i % 2048}",
                invoice_number_raw=None,
                invoice_canonical=f"I{i}",
                invoice_date=day,
                payment_date=None,
                amount=amount,
                invoice_amount=amount,
                po_number=None,
                payment_method=None,
                payment_status=None,
                department=None,
                description=None,
                line_count=1,
                distinct_line_amounts=[amount],
            )
        )
    return rows


@pytest.fixture
def thirty_cases() -> tuple[list[Transaction], list[PlantedCase]]:
    return _thirty_case_ledger()


def test_recall_is_perfect_on_thirty_cases(
    thirty_cases: tuple[list[Transaction], list[PlantedCase]],
) -> None:
    rows, cases = thirty_cases
    assert len(cases) == 30
    candidates = block(rows, FULL_CAPABILITIES, Settings())
    found = {(item.transaction_ids, item.signal) for item in candidates}
    missing = [
        case.case_id
        for case in cases
        if (
            tuple(sorted((case.left.transaction_id, case.right.transaction_id))),
            case.signal,
        )
        not in found
    ]
    assert missing == []
    assert {item.stage_generated for item in candidates} == {"blocking"}
    assert all(type(item.amount_at_risk) is Decimal for item in candidates)


def test_no_bucket_exceeds_the_cap() -> None:
    cap = 20
    n = 500
    settings = Settings(blocking_bucket_cap=cap)
    rows = [_fat_row(i) for i in range(n)]
    candidates, stats = block_with_stats(rows, FULL_CAPABILITIES, settings)
    assert stats.max_bucket <= cap
    exact = [item for item in candidates if item.signal is Signal.exact_duplicate]
    expected_pairs = (n // cap) * (cap * (cap - 1) // 2)
    assert len(exact) == expected_pairs
    degree: dict[str, int] = {}
    for item in exact:
        for tx_id in item.transaction_ids:
            degree[tx_id] = degree.get(tx_id, 0) + 1
    assert max(degree.values()) == cap - 1


def test_default_bucket_cap_is_enforced() -> None:
    rows = [_fat_row(i) for i in range(250)]
    _candidates, stats = block_with_stats(rows, FULL_CAPABILITIES)
    assert stats.max_bucket <= 200
    assert stats.max_bucket == 200


def test_oversized_bucket_splits_by_secondary_key() -> None:
    cap = 200
    n_each = 125
    day_a = date(2024, 1, 1)
    day_b = date(2024, 6, 1)
    rows = [_fat_row(f"a{i:03d}", invoice_date=day_a) for i in range(n_each)]
    rows.extend(_fat_row(f"b{i:03d}", invoice_date=day_b) for i in range(n_each))
    candidates, stats = block_with_stats(
        rows, FULL_CAPABILITIES, Settings(blocking_bucket_cap=cap)
    )
    exact = [item for item in candidates if item.signal is Signal.exact_duplicate]
    expected = 2 * (n_each * (n_each - 1) // 2)
    assert len(exact) == expected
    assert stats.max_bucket == n_each
    assert stats.max_bucket <= cap


def test_capabilities_gating_disables_invoice_signals(
    thirty_cases: tuple[list[Transaction], list[PlantedCase]],
) -> None:
    rows, _cases = thirty_cases
    with_invoice = {item.signal for item in block(rows, FULL_CAPABILITIES, Settings())}
    assert Signal.exact_duplicate in with_invoice
    assert Signal.invoice_variant in with_invoice

    gated = block(rows, NO_INVOICE_CAPABILITIES, Settings())
    signals = {item.signal for item in gated}
    assert Signal.exact_duplicate not in signals
    assert Signal.invoice_variant not in signals
    assert Signal.overpayment_vs_invoice not in signals


def test_capabilities_gating_disables_overpayment(
    thirty_cases: tuple[list[Transaction], list[PlantedCase]],
) -> None:
    rows, _cases = thirty_cases
    full = block(rows, FULL_CAPABILITIES, Settings())
    assert any(item.signal is Signal.overpayment_vs_invoice for item in full)
    gated = block(rows, NO_INVOICE_AMOUNT_CAPABILITIES, Settings())
    signals = {item.signal for item in gated}
    assert Signal.overpayment_vs_invoice not in signals
    assert Signal.exact_duplicate in signals
    assert Signal.invoice_variant in signals


def test_adapter_capabilities_are_respected(
    thirty_cases: tuple[list[Transaction], list[PlantedCase]],
) -> None:
    rows, _cases = thirty_cases
    oklahoma = block(rows, OklahomaAdapter().capabilities, Settings())
    la_signals = {
        item.signal for item in block(rows, LAAdapter().capabilities, Settings())
    }
    ok_signals = {item.signal for item in oklahoma}
    assert Signal.exact_duplicate in la_signals
    assert Signal.invoice_variant in la_signals
    assert Signal.overpayment_vs_invoice in la_signals
    assert Signal.exact_duplicate not in ok_signals
    assert Signal.invoice_variant not in ok_signals
    assert Signal.overpayment_vs_invoice not in ok_signals


def test_no_invoice_falls_back_to_vendor_date_amount() -> None:
    day = date(2024, 3, 1)
    left = _tx(
        "ok-a",
        vendor="OKLA",
        invoice="SHOULD-IGNORE",
        amount="55.00",
        invoice_date=day,
        department="HEALTH",
    )
    right = _tx(
        "ok-b",
        vendor="OKLA",
        invoice="OTHER-INVOICE",
        amount="55.00",
        invoice_date=day,
        department="HEALTH",
    )
    candidates = block([left, right], NO_INVOICE_CAPABILITIES, Settings())
    pairs = {item.transaction_ids for item in candidates}
    assert ("ok-a", "ok-b") in pairs
    signals = {item.signal for item in candidates}
    assert Signal.exact_duplicate not in signals
    assert Signal.invoice_variant not in signals
    assert Signal.overpayment_vs_invoice not in signals
    assert Signal.split_payment in signals


def test_comparisons_are_linear_in_rows() -> None:
    cap = 20
    settings = Settings(blocking_bucket_cap=cap)
    counts: list[tuple[int, int]] = []
    for n in (400, 800, 1600):
        rows = [_fat_row(i) for i in range(n)]
        _candidates, stats = block_with_stats(rows, FULL_CAPABILITIES, settings)
        assert stats.max_bucket <= cap
        assert stats.pair_comparisons <= n * (cap - 1) * 6
        counts.append((n, stats.pair_comparisons))
    slack = cap * cap * 6
    assert counts[1][1] <= counts[0][1] * 2 + slack
    assert counts[2][1] <= counts[1][1] * 2 + slack
    quadratic_guess = counts[0][1] * 4
    assert counts[1][1] < quadratic_guess


def test_module_states_linear_complexity() -> None:
    from core import blocking

    doc = blocking.__doc__
    assert doc is not None
    assert "O(R)" in doc
    assert "O(R^2)" in doc


def test_two_hundred_thousand_rows_complete_under_30_seconds() -> None:
    rows = _large_ledger(200_000)
    started = time.perf_counter()
    _candidates, stats = block_with_stats(rows, FULL_CAPABILITIES, Settings())
    elapsed = time.perf_counter() - started
    assert stats.rows_in == 200_000
    assert stats.max_bucket <= 200
    assert elapsed < 30


def test_two_runs_are_byte_identical(
    thirty_cases: tuple[list[Transaction], list[PlantedCase]],
) -> None:
    rows, _cases = thirty_cases
    first = _dump(block(rows, FULL_CAPABILITIES, Settings()))
    second = _dump(block(list(reversed(rows)), FULL_CAPABILITIES, Settings()))
    assert first == second
    assert first
    payload = json.loads(first.split("\n", 1)[0])
    assert isinstance(payload["amount_at_risk"], str)


def test_empty_input_returns_empty_list() -> None:
    assert block([], FULL_CAPABILITIES, Settings()) == []


def test_invalid_bucket_cap_raises() -> None:
    with pytest.raises(BlockingError, match="blocking_bucket_cap"):
        block([], FULL_CAPABILITIES, Settings(blocking_bucket_cap=0))


def test_same_transaction_id_is_not_paired() -> None:
    rows = [
        _tx("same-id", vendor="ACME", invoice="DUP", amount="1.00"),
        _tx("same-id", vendor="ACME", invoice="DUP", amount="1.00"),
    ]
    assert block(rows, FULL_CAPABILITIES, Settings()) == []


def test_rows_missing_invoice_skip_invoice_keys() -> None:
    rows = [
        _tx(
            "m1",
            vendor="MISSING",
            invoice=None,
            invoice_canonical=None,
            amount="9.00",
        ),
        _tx(
            "m2",
            vendor="MISSING",
            invoice=None,
            invoice_canonical=None,
            amount="9.00",
        ),
    ]
    candidates = block(rows, FULL_CAPABILITIES, Settings())
    signals = {item.signal for item in candidates}
    assert Signal.exact_duplicate not in signals
    assert Signal.invoice_variant not in signals
    assert Signal.split_payment not in signals


def test_blank_vendor_is_not_bucketed() -> None:
    rows = [
        _tx("b1", vendor="   ", vendor_canonical="", invoice="B", amount="3.00"),
        _tx("b2", vendor="   ", vendor_canonical="", invoice="B", amount="3.00"),
    ]
    candidates = block(rows, FULL_CAPABILITIES, Settings())
    assert all(item.signal is not Signal.exact_duplicate for item in candidates)


def test_cross_department_requires_different_departments() -> None:
    rows = [
        _tx("d1", vendor="DEPTV", invoice="D1", amount="12.00", department="PARKS"),
        _tx("d2", vendor="DEPTV", invoice="D1", amount="12.00", department="PARKS"),
    ]
    candidates = block(rows, FULL_CAPABILITIES, Settings())
    assert all(item.signal is not Signal.cross_department for item in candidates)
    assert any(item.signal is Signal.exact_duplicate for item in candidates)


def test_overpayment_requires_excess() -> None:
    rows = [
        _tx(
            "p1",
            vendor="PAY",
            invoice="P1",
            amount="10.00",
            invoice_amount="50.00",
        ),
        _tx(
            "p2",
            vendor="PAY",
            invoice="P1",
            amount="20.00",
            invoice_amount="50.00",
        ),
    ]
    candidates = block(rows, FULL_CAPABILITIES, Settings())
    assert all(item.signal is not Signal.overpayment_vs_invoice for item in candidates)
    assert any(item.signal is Signal.split_payment for item in candidates)


def test_amount_at_risk_is_decimal_min_or_excess() -> None:
    exact = block(
        [
            _tx("e1", vendor="E", invoice="E1", amount="15.50"),
            _tx("e2", vendor="E", invoice="E1", amount="15.50"),
        ],
        FULL_CAPABILITIES,
        Settings(),
    )
    exact_hit = next(item for item in exact if item.signal is Signal.exact_duplicate)
    assert exact_hit.amount_at_risk == Decimal("15.50")
    assert type(exact_hit.amount_at_risk) is Decimal

    over = block(
        [
            _tx(
                "o1",
                vendor="O",
                invoice="O1",
                amount="40.00",
                invoice_amount="50.00",
            ),
            _tx(
                "o2",
                vendor="O",
                invoice="O1",
                amount="30.00",
                invoice_amount="50.00",
            ),
        ],
        FULL_CAPABILITIES,
        Settings(),
    )
    over_hit = next(
        item for item in over if item.signal is Signal.overpayment_vs_invoice
    )
    assert over_hit.amount_at_risk == Decimal("20.00")
    assert type(over_hit.amount_at_risk) is Decimal


def test_transaction_ids_are_sorted() -> None:
    candidates = block(
        [
            _tx("z-last", vendor="S", invoice="S1", amount="8.00"),
            _tx("a-first", vendor="S", invoice="S1", amount="8.00"),
        ],
        FULL_CAPABILITIES,
        Settings(),
    )
    exact = next(item for item in candidates if item.signal is Signal.exact_duplicate)
    assert exact.transaction_ids == ("a-first", "z-last")
    assert exact.candidate_id == "exact_duplicate:a-first:z-last"
