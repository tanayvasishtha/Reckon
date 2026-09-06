from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api.main import (
    FIXTURE_PATH,
    Store,
    approver_for,
    create_app,
    looks_like_person,
    mask_personal_name,
)
from api.schemas import FixtureFile
from core.models import Decision, VerdictOutcome

# Labelled-run funnel from RESULTS.md: 1742 blocked, 1731 residual,
# 871 escalate + 3 errored = 874 queued survivors.
_PIPELINE_CANDIDATES = 1742
_PIPELINE_AFTER_DISMISS = 1731
_PIPELINE_AFTER_ADJUDICATE = 874


@pytest.fixture(autouse=True)
def _hide_generated_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    missing = tmp_path / "absent" / "queue.json"
    monkeypatch.setattr("api.main.GENERATED_QUEUE_PATH", missing)
    return missing


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _queue_ids(client: TestClient) -> list[str]:
    payload = client.get("/queue").json()
    return [item["candidate_id"] for item in payload["items"]]


def _assert_money_strings(data: object) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in {"amount_at_risk", "amount", "invoice_amount"} or key.endswith(
                "_line_amounts"
            ):
                if isinstance(value, list):
                    for item in value:
                        assert isinstance(item, str), f"{key} item is {type(item)}"
                        Decimal(item)
                elif value is not None:
                    assert isinstance(value, str), f"{key} is {type(value)}"
                    Decimal(value)
            else:
                _assert_money_strings(value)
        return
    if isinstance(data, list):
        for item in data:
            _assert_money_strings(item)


def test_fixture_is_committed_and_valid() -> None:
    assert FIXTURE_PATH.is_file()
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture = FixtureFile.model_validate(raw)
    assert len(fixture.findings) == 8
    assert fixture.funnel.after_adjudicate == 8
    for finding in fixture.findings:
        left_id, right_id = finding.candidate.transaction_ids
        assert finding.left.transaction_id == left_id
        assert finding.right.transaction_id == right_id
        assert finding.verdict.candidate_id == finding.candidate.candidate_id
        assert finding.verdict.verdict in {
            VerdictOutcome.escalate,
            VerdictOutcome.errored,
        }


def test_queue_lists_escalated_findings_sorted_by_amount(client: TestClient) -> None:
    response = client.get("/queue")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 8
    amounts = [Decimal(item["amount_at_risk"]) for item in items]
    assert amounts == sorted(amounts, reverse=True)
    ids = [item["candidate_id"] for item in items]
    assert ids[0] == "cand-001"
    assert "cand-008" in ids
    _assert_money_strings(response.json())


def test_queue_masks_personal_names(client: TestClient) -> None:
    items = client.get("/queue").json()["items"]
    vendors = {item["candidate_id"]: item["vendor_name"] for item in items}
    assert "MARIA" not in json.dumps(items)
    assert "SANTOS" not in json.dumps(items)
    assert vendors["cand-007"].startswith("Individual")
    assert vendors["cand-001"] == "ACME PAVING INC"


def test_queue_derives_approver_from_department(client: TestClient) -> None:
    items = {
        item["candidate_id"]: item for item in client.get("/queue").json()["items"]
    }
    assert items["cand-001"]["approver"] == "Elena Voss"
    assert items["cand-001"]["department"] == "Public Works"
    assert items["cand-005"]["approver"] == "Priya Nair"
    assert items["cand-005"]["department"] == "Aging"


def test_candidate_returns_full_finding(client: TestClient) -> None:
    response = client.get("/candidate/cand-001")
    assert response.status_code == 200
    payload = response.json()
    _assert_money_strings(payload)
    assert payload["candidate_id"] == "cand-001"
    assert payload["signal"] == "exact_duplicate"
    assert payload["verdict"] == "escalate"
    assert payload["model_used"] == "fast"
    assert payload["confidence"] == 0.91
    assert payload["approver"] == "Elena Voss"
    assert payload["left"]["transaction_id"] == "EFT26240000044112"
    assert payload["right"]["transaction_id"] == "AD26240000118804"
    assert "{" not in payload["reasoning"]
    assert payload["reasoning"].startswith("Both disbursements")
    assert payload["evidence"][0]["field"] == "invoice_canonical"
    assert payload["amount_at_risk"] == "47250.00"
    assert isinstance(payload["amount_at_risk"], str)
    assert isinstance(payload["left"]["amount"], str)


def test_candidate_masks_personal_names_in_both_rows(client: TestClient) -> None:
    payload = client.get("/candidate/cand-007").json()
    blob = json.dumps(payload)
    assert "MARIA SANTOS" not in blob
    assert "maria santos" not in blob
    assert payload["left"]["vendor_name_raw"].startswith("Individual")
    assert payload["right"]["vendor_name_raw"].startswith("Individual")


def test_candidate_unknown_is_404(client: TestClient) -> None:
    response = client.get("/candidate/cand-missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "candidate not found"


def test_errored_finding_is_in_the_queue(client: TestClient) -> None:
    payload = client.get("/candidate/cand-008").json()
    assert payload["verdict"] == "errored"
    assert "cand-008" in _queue_ids(client)


def test_stats_returns_funnel_counts(client: TestClient) -> None:
    response = client.get("/stats")
    assert response.status_code == 200
    stats = response.json()
    _assert_money_strings(stats)
    assert stats["rows_in"] == 20000
    assert stats["transactions"] == 14208
    assert stats["candidates"] == 486
    assert stats["after_dismiss"] == 61
    assert stats["after_adjudicate"] == 8
    assert stats["pending"] == 8
    assert stats["confirmed"] == 0
    assert stats["dismissed"] == 0
    assert stats["dollars_at_risk"] == "117034.25"
    assert isinstance(stats["dollars_at_risk"], str)
    assert stats["data_source"] == "fixture"


def test_queue_reports_fixture_data_source(client: TestClient) -> None:
    payload = client.get("/queue").json()
    assert payload["data_source"] == "fixture"
    assert len(payload["items"]) == 8


def test_stats_uses_generated_pipeline_funnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    raw["funnel"] = {
        "rows_in": 20000,
        "transactions": 14208,
        "candidates": _PIPELINE_CANDIDATES,
        "after_dismiss": _PIPELINE_AFTER_DISMISS,
        "after_adjudicate": _PIPELINE_AFTER_ADJUDICATE,
    }
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("api.main.GENERATED_QUEUE_PATH", path)
    local = TestClient(create_app())
    stats = local.get("/stats").json()
    _assert_money_strings(stats)
    assert stats["candidates"] == _PIPELINE_CANDIDATES
    assert stats["after_dismiss"] == _PIPELINE_AFTER_DISMISS
    assert stats["after_adjudicate"] == _PIPELINE_AFTER_ADJUDICATE
    assert stats["data_source"] == "pipeline"
    assert stats["pending"] == 8
    queue = local.get("/queue").json()
    assert queue["data_source"] == "pipeline"


def test_unreadable_generated_queue_falls_back_to_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr("api.main.GENERATED_QUEUE_PATH", path)
    local = TestClient(create_app())
    stats = local.get("/stats").json()
    assert stats["candidates"] == 486
    assert stats["after_dismiss"] == 61
    assert stats["after_adjudicate"] == 8
    assert stats["data_source"] == "fixture"


def test_startup_does_not_run_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    spies = [
        Mock(name="aggregate"),
        Mock(name="block"),
        Mock(name="dismiss"),
        Mock(name="adjudicate"),
    ]
    monkeypatch.setattr("core.aggregate.aggregate", spies[0])
    monkeypatch.setattr("core.blocking.block", spies[1])
    monkeypatch.setattr("core.dismiss.dismiss", spies[2])
    monkeypatch.setattr("agents.adjudicate.adjudicate", spies[3])
    local = TestClient(create_app())
    stats = local.get("/stats").json()
    assert stats["data_source"] == "fixture"
    assert stats["candidates"] == 486
    for spy in spies:
        spy.assert_not_called()


def test_decide_records_and_leaves_the_queue(client: TestClient) -> None:
    before = client.get("/stats").json()
    response = client.post(
        "/decide",
        json={
            "candidate_id": "cand-001",
            "approver": "Elena Voss",
            "decision": "confirmed",
            "reason": "Same invoice paid twice.",
            "becomes_rule": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == "cand-001"
    assert body["decision"] == Decision.confirmed.value
    assert body["reason"] == "Same invoice paid twice."
    assert body["approver"] == "Elena Voss"
    assert body["becomes_rule"] is True
    assert "cand-001" not in _queue_ids(client)
    after = client.get("/stats").json()
    assert after["pending"] == before["pending"] - 1
    assert after["confirmed"] == 1
    assert after["dismissed"] == 0
    assert after["after_adjudicate"] == 8
    expected = Decimal(before["dollars_at_risk"]) - Decimal("47250.00")
    assert Decimal(after["dollars_at_risk"]) == expected


def test_decide_fills_approver_from_department(client: TestClient) -> None:
    response = client.post(
        "/decide",
        json={
            "candidate_id": "cand-003",
            "decision": "dismissed",
            "reason": "33091A is a reissued invoice after a voided cheque.",
        },
    )
    assert response.status_code == 200
    assert response.json()["approver"] == "Marcus Hale"
    assert response.json()["decision"] == "dismissed"
    stats = client.get("/stats").json()
    assert stats["dismissed"] == 1


def test_decide_unknown_is_404(client: TestClient) -> None:
    response = client.post(
        "/decide",
        json={
            "candidate_id": "cand-missing",
            "decision": "confirmed",
            "reason": "not in the fixture",
        },
    )
    assert response.status_code == 404


def test_decide_twice_is_409(client: TestClient) -> None:
    body = {
        "candidate_id": "cand-006",
        "decision": "confirmed",
        "reason": "Overpayment of 2418.75 against invoice MF-77821.",
        "becomes_rule": True,
    }
    first = client.post("/decide", json=body)
    second = client.post("/decide", json=body)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "candidate already decided"


def test_decide_blank_reason_is_422(client: TestClient) -> None:
    response = client.post(
        "/decide",
        json={
            "candidate_id": "cand-001",
            "decision": "confirmed",
            "reason": "   ",
        },
    )
    assert response.status_code == 422
    assert "cand-001" in _queue_ids(client)


def test_decide_rejects_unknown_decision(client: TestClient) -> None:
    response = client.post(
        "/decide",
        json={
            "candidate_id": "cand-001",
            "decision": "maybe",
            "reason": "not a valid decision",
        },
    )
    assert response.status_code == 422


def test_model_dismissed_findings_are_not_queued(tmp_path: Path) -> None:
    raw: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    sample = json.loads(json.dumps(raw["findings"][0]))
    sample["candidate"]["candidate_id"] = "cand-dismissed"
    sample["verdict"]["candidate_id"] = "cand-dismissed"
    sample["verdict"]["verdict"] = "dismiss"
    sample["verdict"]["explanation_type"] = "recurring"
    raw["findings"].append(sample)
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    local = TestClient(create_app(path))
    assert "cand-dismissed" not in _queue_ids(local)
    assert "cand-001" in _queue_ids(local)


def test_missing_fixture_fails_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(FileNotFoundError, match="queue fixture not found"):
        Store.load(missing)


def test_mask_personal_name_leaves_businesses() -> None:
    assert mask_personal_name("ACME PAVING INC") == "ACME PAVING INC"
    assert mask_personal_name("ONEGENERATION") == "ONEGENERATION"
    assert looks_like_person("MARIA SANTOS")
    assert mask_personal_name("MARIA SANTOS") == "Individual · M. S."
    assert mask_personal_name("SMITH, JOHN") == "Individual · S. J."


def test_approver_map_covers_fixture_departments() -> None:
    assert approver_for("PUBLIC WORKS") == "Elena Voss"
    assert approver_for("AGING") == "Priya Nair"
    assert approver_for(None) == "Alex Rivera"
