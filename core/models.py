from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer
from pydantic_core import PydanticCustomError


def _parse_money(value: object) -> Decimal:
    if isinstance(value, float):
        raise PydanticCustomError(
            "float_money", "float is not allowed for monetary values"
        )
    if isinstance(value, bool):
        raise PydanticCustomError(
            "bool_money", "bool is not allowed for monetary values"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise PydanticCustomError(
        "money_type",
        "unsupported monetary type: {type_name}",
        {"type_name": type(value).__name__},
    )


def _money_to_json(value: Decimal) -> str:
    return str(value)


Money = Annotated[
    Decimal,
    BeforeValidator(_parse_money),
    PlainSerializer(_money_to_json, return_type=str, when_used="json"),
]


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Signal(StrEnum):
    exact_duplicate = "exact_duplicate"
    fuzzy_vendor = "fuzzy_vendor"
    invoice_variant = "invoice_variant"
    split_payment = "split_payment"
    cross_department = "cross_department"
    overpayment_vs_invoice = "overpayment_vs_invoice"


class VerdictOutcome(StrEnum):
    dismiss = "dismiss"
    escalate = "escalate"
    errored = "errored"


class ExplanationType(StrEnum):
    cancelled = "cancelled"
    line_split = "line_split"
    progress_payment = "progress_payment"
    recurring = "recurring"
    different_po = "different_po"
    partial_pair = "partial_pair"
    none = "none"


class Decision(StrEnum):
    confirmed = "confirmed"
    dismissed = "dismissed"


class Evidence(SchemaModel):
    field: str
    values: list[str]


class Transaction(SchemaModel):
    transaction_id: str
    vendor_name_raw: str
    vendor_id: str | None = None
    vendor_canonical: str | None = None
    invoice_number_raw: str | None = None
    invoice_canonical: str | None = None
    invoice_date: date | None = None
    payment_date: date | None = None
    amount: Money
    invoice_amount: Money | None = None
    po_number: str | None = None
    payment_method: str | None = None
    payment_status: str | None = None
    department: str | None = None
    description: str | None = None
    line_count: int
    distinct_line_amounts: list[Money]


class Candidate(SchemaModel):
    candidate_id: str
    transaction_ids: tuple[str, str]
    signal: Signal
    amount_at_risk: Money
    stage_generated: str


class Verdict(SchemaModel):
    candidate_id: str
    verdict: VerdictOutcome
    explanation_type: ExplanationType
    reasoning: str
    evidence: list[Evidence]
    confidence: float
    model_used: str
    tokens_in: int
    tokens_out: int
    cache_read: int


class ReviewDecision(SchemaModel):
    candidate_id: str
    approver: str
    decision: Decision
    reason: str
    becomes_rule: bool


class TrueDuplicate(SchemaModel):
    case_id: str
    transaction_ids: list[str]
    type: str
    amount: Money


class Decoy(SchemaModel):
    case_id: str
    transaction_ids: list[str]
    reason: str
    why_rules_cannot_tell: str


class GroundTruth(SchemaModel):
    true_duplicates: list[TrueDuplicate]
    decoys: list[Decoy]
