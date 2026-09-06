"""Emit the recovery packet for a confirmed finding.

Stage 7 of the pipeline. A Candidate, its Verdict, and a confirmed
ReviewDecision are rendered into a recovery letter, a journal entry, and
an audit trail. Templates are filled deterministically; there is no model
call.

Complexity: O(T) to index T supplied transactions by id, then O(1) to
render three fixed templates. Extra memory is O(T) for the index plus the
size of the artefacts. Callers pass the two rows named on the candidate;
this stage does not rescan a ledger.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from core.models import (
    Candidate,
    Decision,
    ReviewDecision,
    Transaction,
    Verdict,
)

log = logging.getLogger("packet.builder")

TEMPLATES = Path(__file__).resolve().parent / "templates"
_CENTS = Decimal("0.01")
_TOKEN_SPLIT = re.compile(r"[\s,./&+-]+")
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*$")
_BUSINESS_MARKERS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "co",
        "company",
        "foundation",
        "center",
        "centre",
        "council",
        "department",
        "city",
        "county",
        "state",
        "university",
        "district",
        "authority",
        "services",
        "service",
        "group",
        "associates",
        "association",
        "partners",
        "partnership",
        "hospital",
        "clinic",
        "school",
        "church",
        "ministry",
        "bureau",
        "office",
        "commission",
        "agency",
        "board",
        "trust",
        "bank",
        "industries",
        "construction",
        "paving",
        "supply",
        "supplies",
        "enterprises",
        "solutions",
        "systems",
        "technologies",
        "international",
        "national",
        "municipal",
        "pllc",
        "pc",
        "lp",
        "llp",
        "plc",
        "gmbh",
        "dba",
        "sa",
        "bv",
        "nv",
        "the",
        "of",
        "and",
    }
)
STAGE_ORDER: tuple[str, ...] = (
    "aggregate",
    "normalize",
    "blocking",
    "dismiss",
    "adjudicate",
    "review",
)


class PacketError(Exception):
    """Stage 7 cannot emit a packet from this finding."""


@dataclass(frozen=True, slots=True)
class Packet:
    """Shareable artefacts for one confirmed finding, keyed on candidate_id."""

    packet_id: str
    letter: str
    journal: str
    audit_trail: str


def build_packet(
    candidate: Candidate,
    verdict: Verdict,
    decision: ReviewDecision,
    transactions: Iterable[Transaction],
    *,
    issued_at: datetime | date,
    run_id: str | None = None,
) -> Packet:
    """Return the letter, journal entry, and audit trail for a confirmed finding."""
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info(
        "packet start run_id=%s candidate_id=%s",
        active_run_id,
        candidate.candidate_id,
    )

    _require_aligned(candidate, verdict, decision)
    if decision.decision is not Decision.confirmed:
        raise PacketError(
            "review decision is "
            f"{decision.decision.value}, not confirmed. "
            "A packet is only issued for a confirmed finding."
        )

    left, right = _pair_for(candidate, transactions)
    issued = _as_datetime(issued_at)
    issued_stamp = _format_when(issued)
    issued_date = _format_long_date(issued.date())
    values = _template_values(
        candidate,
        verdict,
        decision,
        left,
        right,
        issued_stamp=issued_stamp,
        issued_date=issued_date,
    )
    names = _person_names((left, right))
    packet = Packet(
        packet_id=candidate.candidate_id,
        letter=_redact(_render("letter.md", values), names),
        journal=_redact(_render("journal.md", values), names),
        audit_trail=_redact(_render("audit.md", values), names),
    )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "packet done rows_in=1 rows_out=1 elapsed_ms=%s run_id=%s packet_id=%s",
        elapsed_ms,
        active_run_id,
        packet.packet_id,
    )
    return packet


def _require_aligned(
    candidate: Candidate, verdict: Verdict, decision: ReviewDecision
) -> None:
    if verdict.candidate_id != candidate.candidate_id:
        raise PacketError(
            "verdict candidate_id "
            f"{verdict.candidate_id!r} does not match "
            f"{candidate.candidate_id!r}. Pass the verdict for this candidate."
        )
    if decision.candidate_id != candidate.candidate_id:
        raise PacketError(
            "review decision candidate_id "
            f"{decision.candidate_id!r} does not match "
            f"{candidate.candidate_id!r}. Pass the decision for this candidate."
        )


def _pair_for(
    candidate: Candidate, transactions: Iterable[Transaction]
) -> tuple[Transaction, Transaction]:
    index: dict[str, Transaction] = {}
    for row in transactions:
        if row.transaction_id not in index:
            index[row.transaction_id] = row
    missing = [item_id for item_id in candidate.transaction_ids if item_id not in index]
    if missing:
        needed = ", ".join(candidate.transaction_ids)
        raise PacketError(
            "missing transaction "
            f"{', '.join(missing)}. Pass both rows named on the candidate "
            f"({needed})."
        )
    first = index[candidate.transaction_ids[0]]
    second = index[candidate.transaction_ids[1]]
    ordered = tuple(sorted((first, second), key=lambda row: row.transaction_id))
    return ordered[0], ordered[1]


def _template_values(
    candidate: Candidate,
    verdict: Verdict,
    decision: ReviewDecision,
    left: Transaction,
    right: Transaction,
    *,
    issued_stamp: str,
    issued_date: str,
) -> dict[str, str]:
    ref_a = _payment_reference(left)
    ref_b = _payment_reference(right)
    return {
        "packet_id": candidate.candidate_id,
        "candidate_id": candidate.candidate_id,
        "issued_at": issued_stamp,
        "issued_date": issued_date,
        "vendor_name": _vendor_label(left, right),
        "ref_a": ref_a,
        "ref_b": ref_b,
        "date_a": _payment_date_label(left),
        "date_b": _payment_date_label(right),
        "amount_a": _format_money(left.amount),
        "amount_b": _format_money(right.amount),
        "amount_at_risk": _format_money(candidate.amount_at_risk),
        "signal": candidate.signal.value.replace("_", " "),
        "approver": decision.approver,
        "decision": decision.decision.value,
        "stage_rows": _stage_rows(
            candidate, verdict, decision, left, right, issued_stamp
        ),
    }


def _stage_rows(
    candidate: Candidate,
    verdict: Verdict,
    decision: ReviewDecision,
    left: Transaction,
    right: Transaction,
    issued_stamp: str,
) -> str:
    conclusions = dict(_conclusions(candidate, verdict, decision, left, right))
    rows = [
        f"| {issued_stamp} | {stage} | {_cell(conclusions[stage])} |"
        for stage in STAGE_ORDER
    ]
    return "\n".join(rows)


def _conclusions(
    candidate: Candidate,
    verdict: Verdict,
    decision: ReviewDecision,
    left: Transaction,
    right: Transaction,
) -> tuple[tuple[str, str], ...]:
    vendor = left.vendor_canonical or left.vendor_name_raw
    invoice = left.invoice_canonical or left.invoice_number_raw or "not recorded"
    becomes = "yes" if decision.becomes_rule else "no"
    return (
        (
            "aggregate",
            (
                "Distribution lines collapsed to transaction grain for "
                f"{left.transaction_id} and {right.transaction_id}."
            ),
        ),
        (
            "normalize",
            (f"Vendor canonicalised to {vendor}; invoice canonicalised to {invoice}."),
        ),
        (
            "blocking",
            (
                f"Candidate {candidate.candidate_id} generated on signal "
                f"{candidate.signal.value} with amount at risk "
                f"{_format_money(candidate.amount_at_risk)}. "
                f"Raised by stage {candidate.stage_generated}."
            ),
        ),
        (
            "dismiss",
            (
                "No deterministic rule dismissed the pair; "
                "it remained residual for adjudication."
            ),
        ),
        (
            "adjudicate",
            (
                f"Verdict {verdict.verdict.value} "
                f"({verdict.explanation_type.value}), "
                f"confidence {verdict.confidence:.2f}. "
                f"{verdict.reasoning}"
            ),
        ),
        (
            "review",
            (
                f"{decision.decision.value} by {decision.approver}. "
                f"Reason: {decision.reason.rstrip('.')}. "
                f"Becomes rule: {becomes}."
            ),
        ),
    )


def _render(name: str, values: Mapping[str, str]) -> str:
    path = TEMPLATES / name
    if not path.is_file():
        raise PacketError(
            f"packet template not found: {path.as_posix()}. "
            "Add it under packet/templates/."
        )
    text = path.read_text(encoding="utf-8")
    try:
        return text.format_map(values)
    except KeyError as exc:
        raise PacketError(
            f"packet template {name} is missing placeholder {exc}. "
            "Fix the template or the renderer."
        ) from exc


def _payment_reference(row: Transaction) -> str:
    invoice = row.invoice_number_raw
    if invoice is None or invoice == "":
        return row.transaction_id
    return f"{row.transaction_id} (invoice {invoice})"


def _payment_date_label(row: Transaction) -> str:
    value = row.payment_date if row.payment_date is not None else row.invoice_date
    if value is None:
        return "date not recorded"
    return _format_long_date(value)


def _vendor_label(left: Transaction, right: Transaction) -> str:
    names: list[str] = []
    for row in (left, right):
        masked = _mask_personal_name(row.vendor_name_raw)
        if masked not in names:
            names.append(masked)
    return " / ".join(names)


def _format_money(amount: Decimal) -> str:
    quantized = amount.quantize(_CENTS)
    sign = "-" if quantized < 0 else ""
    unsigned = -quantized if quantized < 0 else quantized
    text = format(unsigned, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
    else:
        whole, frac = text, ""
    frac = (frac + "00")[:2]
    digits = whole.lstrip("+") or "0"
    groups: list[str] = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    grouped = ",".join(reversed(groups))
    return f"{sign}${grouped}.{frac}"


def _format_long_date(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _format_when(value: datetime) -> str:
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _as_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _normalize_token(token: str) -> str:
    return token.lower().replace(".", "").replace(",", "")


def _looks_like_person(name: str) -> bool:
    tokens = [token for token in _TOKEN_SPLIT.split(name.strip()) if token]
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    markers = {_normalize_token(token) for token in tokens}
    if markers & _BUSINESS_MARKERS:
        return False
    return all(_NAME_TOKEN.fullmatch(token) is not None for token in tokens)


def _mask_personal_name(name: str) -> str:
    if not _looks_like_person(name):
        return name
    tokens = [token for token in _TOKEN_SPLIT.split(name.strip()) if token]
    initials = ". ".join(token[0].upper() for token in tokens) + "."
    return f"Individual · {initials}"


def _person_names(rows: Sequence[Transaction]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for raw in (row.vendor_name_raw, row.vendor_canonical):
            if raw is None or raw == "":
                continue
            if not _looks_like_person(raw):
                continue
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(raw)
    found.sort(key=len, reverse=True)
    return found


def _redact(text: str, names: Sequence[str]) -> str:
    redacted = text
    for raw in names:
        masked = _mask_personal_name(raw)
        redacted = re.compile(re.escape(raw), re.IGNORECASE).sub(masked, redacted)
    return redacted
