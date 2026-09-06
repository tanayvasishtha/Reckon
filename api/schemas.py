from __future__ import annotations

from typing import Literal

from pydantic import field_validator

from core.models import (
    Candidate,
    Decision,
    Evidence,
    Money,
    SchemaModel,
    Signal,
    Transaction,
    Verdict,
    VerdictOutcome,
)

DataSource = Literal["pipeline", "fixture"]


class FunnelSnapshot(SchemaModel):
    rows_in: int
    transactions: int
    candidates: int
    after_dismiss: int
    after_adjudicate: int


class FixtureFinding(SchemaModel):
    candidate: Candidate
    left: Transaction
    right: Transaction
    verdict: Verdict


class FixtureFile(SchemaModel):
    funnel: FunnelSnapshot
    findings: list[FixtureFinding]


class QueueItem(SchemaModel):
    candidate_id: str
    amount_at_risk: Money
    vendor_name: str
    signal: Signal
    confidence: float
    approver: str
    department: str


class QueueResponse(SchemaModel):
    items: list[QueueItem]
    data_source: DataSource


class CandidateResponse(SchemaModel):
    candidate_id: str
    amount_at_risk: Money
    signal: Signal
    approver: str
    department: str
    left: Transaction
    right: Transaction
    reasoning: str
    evidence: list[Evidence]
    model_used: str
    confidence: float
    verdict: VerdictOutcome


class StatsResponse(SchemaModel):
    rows_in: int
    transactions: int
    candidates: int
    after_dismiss: int
    after_adjudicate: int
    dollars_at_risk: Money
    pending: int
    confirmed: int
    dismissed: int
    data_source: DataSource


class DecideRequest(SchemaModel):
    candidate_id: str
    decision: Decision
    reason: str
    approver: str = ""
    becomes_rule: bool = True

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if stripped == "":
            raise ValueError("reason is required")
        return stripped
