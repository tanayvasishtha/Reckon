from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.base import Capabilities
from baseline.rules_only import run_rules_only
from core.aggregate import aggregate
from core.blocking import block
from core.dismiss import dismiss
from core.models import (
    Candidate,
    Evidence,
    ExplanationType,
    Transaction,
    Verdict,
    VerdictOutcome,
)
from core.normalize import canonical_invoice, canonical_vendor
from core.settings import Settings

MODULE_PATH = Path(__file__).resolve().parents[1] / "baseline" / "rules_only.py"

FULL_CAPABILITIES = Capabilities(
    has_invoice_number=True,
    has_po_number=True,
    has_payment_status=True,
    has_vendor_id=True,
    has_invoice_amount=True,
)

PAID_DATE = date(2024, 6, 17)


def _line(
    transaction_id: str,
    amount: Decimal | str,
    *,
    vendor_name_raw: str = "Acme LLC",
    vendor_id: str | None = "V-1",
    invoice_number_raw: str | None = "INV-100",
    invoice_date: date | None = date(2024, 6, 11),
    payment_date: date | None = PAID_DATE,
    invoice_amount: Decimal | str | None = None,
    po_number: str | None = "PO-9",
    payment_method: str | None = "EFT",
    payment_status: str | None = "PAID",
    department: str | None = "AGING",
    description: str | None = "Supplies",
) -> Transaction:
    money = amount if isinstance(amount, Decimal) else Decimal(amount)
    if invoice_amount is None:
        inv_amt: Decimal | None = money
    elif isinstance(invoice_amount, Decimal):
        inv_amt = invoice_amount
    else:
        inv_amt = Decimal(invoice_amount)
    return Transaction(
        transaction_id=transaction_id,
        vendor_name_raw=vendor_name_raw,
        vendor_id=vendor_id,
        vendor_canonical=None,
        invoice_number_raw=invoice_number_raw,
        invoice_canonical=None,
        invoice_date=invoice_date,
        payment_date=payment_date,
        amount=money,
        invoice_amount=inv_amt,
        po_number=po_number,
        payment_method=payment_method,
        payment_status=payment_status,
        department=department,
        description=description,
        line_count=1,
        distinct_line_amounts=[money],
    )


def _mixed_ledger() -> list[Transaction]:
    """Line-grain rows covering a duplicate, a decoy, a cancel, and a split."""
    rows = [
        _line("dup-a", "1250.50"),
        _line("dup-b", "1250.50"),
        _line(
            "prog-a",
            "500.00",
            invoice_number_raw="INV-P1",
            invoice_date=date(2024, 1, 15),
            payment_date=date(2024, 1, 20),
            description="Progress payment 1 of 5",
        ),
        _line(
            "prog-b",
            "500.00",
            invoice_number_raw="INV-P2",
            invoice_date=date(2024, 2, 15),
            payment_date=date(2024, 2, 20),
            description="Progress payment 2 of 5",
        ),
        _line("canc-a", "3450.00", payment_status="PAID"),
        _line(
            "canc-b",
            "-3450.00",
            payment_status="CANCELLED",
            payment_method="CANCELLATION",
            invoice_amount=Decimal("3450.00"),
        ),
        _line("split-a", "12.00"),
        _line("split-a", "30.00"),
        _line("split-a", "49.21"),
        _line("split-b", "100.00"),
        _line("split-b", "200.00"),
        _line("split-b", "1083.53"),
        _line(
            "solo-a",
            "19.25",
            vendor_name_raw="Solo Corp",
            vendor_id="V-SOLO",
            invoice_number_raw="INV-SOLO",
        ),
    ]
    return rows


def _stage5_verdict() -> Verdict:
    """A Verdict as stage 5 of the full pipeline emits it."""
    return Verdict(
        candidate_id="exact_duplicate:dup-a:dup-b",
        verdict=VerdictOutcome.escalate,
        explanation_type=ExplanationType.none,
        reasoning="Amounts match and vendors are aliases.",
        evidence=[Evidence(field="invoice_canonical", values=["100", "100"])],
        confidence=0.91,
        model_used="fast-model",
        tokens_in=120,
        tokens_out=40,
        cache_read=80,
    )


def _normalise_all(transactions: Sequence[Transaction]) -> list[Transaction]:
    result = [
        item.model_copy(
            update={
                "vendor_canonical": canonical_vendor(item.vendor_name_raw),
                "invoice_canonical": canonical_invoice(item.invoice_number_raw),
            }
        )
        for item in transactions
    ]
    result.sort(key=lambda item: item.transaction_id)
    return result


def _stages_1_to_4(
    rows: Iterable[Transaction],
    capabilities: Capabilities,
    settings: Settings | None = None,
) -> tuple[list[Candidate], list[Verdict]]:
    aggregated = aggregate(rows)
    normalised = _normalise_all(aggregated)
    candidates = block(normalised, capabilities, settings)
    residual, dismissed = dismiss(candidates, normalised)
    return residual, dismissed


def _imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_finding_schema_matches_full_pipeline_verdict() -> None:
    verdicts = run_rules_only(_mixed_ledger(), FULL_CAPABILITIES, run_id="schema")
    findings = [item for item in verdicts if item.verdict is VerdictOutcome.escalate]
    assert findings
    finding = findings[0]
    full = _stage5_verdict()

    assert type(finding) is Verdict
    assert type(full) is Verdict
    assert set(finding.model_dump()) == set(Verdict.model_fields)
    assert set(full.model_dump()) == set(Verdict.model_fields)
    assert set(json.loads(finding.model_dump_json())) == set(
        json.loads(full.model_dump_json())
    )
    assert set(json.loads(finding.model_dump_json())) == set(Verdict.model_fields)


def test_findings_carry_no_model_and_no_confidence() -> None:
    verdicts = run_rules_only(_mixed_ledger(), FULL_CAPABILITIES, run_id="none")
    findings = [item for item in verdicts if item.verdict is VerdictOutcome.escalate]
    assert findings
    for finding in findings:
        assert finding.model_used is None
        assert finding.confidence is None
        assert finding.tokens_in == 0
        assert finding.tokens_out == 0
        assert finding.cache_read == 0
        assert finding.explanation_type is ExplanationType.none
        payload = json.loads(finding.model_dump_json())
        assert payload["model_used"] is None
        assert payload["confidence"] is None


def test_module_does_not_import_a_model_client() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    roots = _imported_roots(source)
    assert "agents" not in roots
    assert "httpx" not in roots
    assert "core" in roots


def test_no_model_or_http_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rules-only must not call a model or HTTP client")

    monkeypatch.setattr("httpx.Client.request", boom)
    monkeypatch.setattr("httpx.AsyncClient.request", boom)
    monkeypatch.setattr("agents.llm.ModelClient.__init__", boom)
    monkeypatch.setattr("agents.llm.ModelClient.complete", boom)

    live = Settings.model_validate(
        {
            "api_key": "test-key",
            "api_base": "https://example.test/v1",
            "model_fast": "fast-model",
            "model_escalate": "esc-model",
        }
    )
    verdicts = run_rules_only(
        _mixed_ledger(), FULL_CAPABILITIES, live, run_id="nomodel"
    )
    assert verdicts
    assert any(item.verdict is VerdictOutcome.escalate for item in verdicts)


def test_live_settings_do_not_change_output() -> None:
    rows = _mixed_ledger()
    live = Settings.model_validate(
        {
            "api_key": "test-key",
            "api_base": "https://example.test/v1",
            "model_fast": "fast-model",
        }
    )
    offline = Settings()
    assert run_rules_only(rows, FULL_CAPABILITIES, live, run_id="k") == run_rules_only(
        rows, FULL_CAPABILITIES, offline, run_id="k"
    )


def test_difference_from_full_pipeline_is_only_adjudication() -> None:
    rows = _mixed_ledger()
    residual, dismissed = _stages_1_to_4(rows, FULL_CAPABILITIES)
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="same")

    passed_through = [
        item for item in verdicts if item.verdict is VerdictOutcome.dismiss
    ]
    findings = [item for item in verdicts if item.verdict is VerdictOutcome.escalate]

    assert passed_through == dismissed
    assert [item.candidate_id for item in findings] == [
        item.candidate_id for item in residual
    ]
    assert all(item.verdict is VerdictOutcome.escalate for item in findings)
    assert all(item.model_used is None for item in findings)
    assert {item.verdict for item in verdicts} <= {
        VerdictOutcome.dismiss,
        VerdictOutcome.escalate,
    }


def test_true_duplicate_is_a_finding() -> None:
    rows = [_line("dup-a", "1250.50"), _line("dup-b", "1250.50")]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="dup")
    by_pair = [
        item
        for item in verdicts
        if item.candidate_id.endswith(":dup-a:dup-b")
        or ":dup-a:dup-b:" in item.candidate_id
    ]
    assert by_pair
    assert all(item.verdict is VerdictOutcome.escalate for item in by_pair)
    assert all(item.explanation_type is ExplanationType.none for item in by_pair)
    ids = {item.candidate_id for item in by_pair}
    assert "exact_duplicate:dup-a:dup-b" in ids


def test_cancelled_pair_is_dismissed_not_a_finding() -> None:
    rows = [
        _line("canc-a", "3450.00", payment_status="PAID"),
        _line(
            "canc-b",
            "-3450.00",
            payment_status="CANCELLED",
            payment_method="CANCELLATION",
            invoice_amount=Decimal("3450.00"),
        ),
    ]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="canc")
    related = [
        item
        for item in verdicts
        if "canc-a" in item.candidate_id and "canc-b" in item.candidate_id
    ]
    assert related
    assert all(item.verdict is VerdictOutcome.dismiss for item in related)
    assert all(item.explanation_type is ExplanationType.cancelled for item in related)
    assert all(item.model_used == "deterministic" for item in related)
    assert all(item.confidence == 1.0 for item in related)


def test_line_split_is_dismissed() -> None:
    rows = [
        _line("split-a", "12.00"),
        _line("split-a", "30.00"),
        _line("split-a", "49.21"),
        _line("split-b", "100.00"),
        _line("split-b", "200.00"),
        _line("split-b", "1083.53"),
    ]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="split")
    related = [
        item
        for item in verdicts
        if "split-a" in item.candidate_id and "split-b" in item.candidate_id
    ]
    assert related
    assert any(item.explanation_type is ExplanationType.line_split for item in related)
    assert all(item.verdict is VerdictOutcome.dismiss for item in related)


def test_progress_payment_decoy_is_a_finding() -> None:
    rows = [
        _line(
            "prog-a",
            "500.00",
            invoice_number_raw="INV-P1",
            invoice_date=date(2024, 1, 15),
            payment_date=date(2024, 1, 20),
            description="Progress payment 1 of 5",
        ),
        _line(
            "prog-b",
            "500.00",
            invoice_number_raw="INV-P2",
            invoice_date=date(2024, 2, 15),
            payment_date=date(2024, 2, 20),
            description="Progress payment 2 of 5",
        ),
    ]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="prog")
    related = [
        item
        for item in verdicts
        if "prog-a" in item.candidate_id and "prog-b" in item.candidate_id
    ]
    assert related
    assert all(item.verdict is VerdictOutcome.escalate for item in related)
    assert all(item.explanation_type is ExplanationType.none for item in related)
    assert all(item.model_used is None for item in related)
    descriptions = [
        value
        for item in related
        for evidence in item.evidence
        if evidence.field == "description"
        for value in evidence.values
    ]
    assert "Progress payment 1 of 5" in descriptions
    assert "Progress payment 2 of 5" in descriptions


def test_different_po_decoy_is_a_finding() -> None:
    rows = [
        _line("po-a", "88.00", po_number="PO-1000"),
        _line("po-b", "88.00", po_number="PO-1001"),
    ]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="po")
    related = [
        item
        for item in verdicts
        if "po-a" in item.candidate_id and "po-b" in item.candidate_id
    ]
    assert related
    assert all(item.verdict is VerdictOutcome.escalate for item in related)


def test_unrelated_payment_is_not_a_candidate() -> None:
    rows = [
        _line("dup-a", "1250.50"),
        _line("dup-b", "1250.50"),
        _line(
            "solo-a",
            "19.25",
            vendor_name_raw="Solo Corp",
            vendor_id="V-SOLO",
            invoice_number_raw="INV-SOLO",
        ),
    ]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="solo")
    assert all("solo-a" not in item.candidate_id for item in verdicts)


def test_output_is_sorted_and_reproducible() -> None:
    rows = _mixed_ledger()
    first = run_rules_only(rows, FULL_CAPABILITIES, run_id="rep")
    second = run_rules_only(list(reversed(rows)), FULL_CAPABILITIES, run_id="rep")
    assert [item.candidate_id for item in first] == sorted(
        item.candidate_id for item in first
    )
    assert [item.model_dump_json() for item in first] == [
        item.model_dump_json() for item in second
    ]
    assert first == second


def test_empty_input_returns_empty() -> None:
    assert run_rules_only([], FULL_CAPABILITIES, run_id="empty") == []


def test_finding_evidence_serialises_money_as_strings() -> None:
    rows = [_line("dup-a", "1250.50"), _line("dup-b", "1250.50")]
    verdicts = run_rules_only(rows, FULL_CAPABILITIES, run_id="money")
    finding = next(item for item in verdicts if item.verdict is VerdictOutcome.escalate)
    payload = json.loads(finding.model_dump_json())
    for evidence in payload["evidence"]:
        assert all(isinstance(value, str) for value in evidence["values"])
    amounts = next(
        item["values"] for item in payload["evidence"] if item["field"] == "amount"
    )
    assert amounts == ["1250.50", "1250.50"]
