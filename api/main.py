"""Review-queue HTTP API served from demo output or a committed fixture.

Complexity: O(N) to load N findings once, then O(P log P) to sort the
pending queue of P items on each list request. Lookups and writes are
keyed on candidate_id. Extra memory is the loaded snapshot plus one
ReviewDecision per decided id. The pipeline is never executed at startup.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.schemas import (
    CandidateResponse,
    DataSource,
    DecideRequest,
    FixtureFile,
    FixtureFinding,
    QueueItem,
    QueueResponse,
    StatsResponse,
)
from core.models import (
    Decision,
    ReviewDecision,
    Transaction,
    VerdictOutcome,
)

log = logging.getLogger("api")

_REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = Path(__file__).resolve().parent / "fixture.json"
GENERATED_QUEUE_PATH = _REPO_ROOT / "data" / "generated" / "queue.json"

_IN_QUEUE = frozenset({VerdictOutcome.escalate, VerdictOutcome.errored})

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

_APPROVERS: dict[str, str] = {
    "AGING": "Priya Nair",
    "PUBLIC WORKS": "Elena Voss",
    "TRANSPORTATION": "Marcus Hale",
    "RECREATION AND PARKS": "Jonah Pell",
    "SANITATION": "Amina Cole",
    "WATER AND POWER": "Chris Lang",
    "HOUSING": "Leah Okonkwo",
    "LIBRARY": "Noah Berg",
    "HARBOR": "Ivy Moreau",
    "CITY ATTORNEY": "Sam Ortega",
    "GENERAL SERVICES": "Owen Park",
    "BUILDING AND SAFETY": "Tara Singh",
    "FINANCE": "Helen Cho",
    "FIRE": "Riley Cho",
    "POLICE": "Dana Brooks",
    "AIRPORTS": "Ben Calder",
}

_FALLBACK_APPROVER = "Alex Rivera"
_FALLBACK_DEPARTMENT = "Unassigned"
_SMALL_WORDS = frozenset({"and", "of", "the"})
_TOKEN_SPLIT = re.compile(r"[\s,./&+-]+")
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]*$")


def _department_key(department: str | None) -> str:
    if department is None:
        return ""
    return " ".join(department.upper().split())


def title_department(department: str | None) -> str:
    key = _department_key(department)
    if key == "":
        return _FALLBACK_DEPARTMENT
    parts = key.lower().split()
    titled: list[str] = []
    for index, part in enumerate(parts):
        if index > 0 and part in _SMALL_WORDS:
            titled.append(part)
        else:
            titled.append(part.title())
    return " ".join(titled)


def approver_for(department: str | None) -> str:
    key = _department_key(department)
    if key == "":
        return _FALLBACK_APPROVER
    named = _APPROVERS.get(key)
    if named is not None:
        return named
    roster = ("Alex Rivera", "Jordan Quinn", "Morgan Ellis", "Casey Walsh")
    index = sum(ord(char) for char in key) % len(roster)
    return roster[index]


def _normalize_token(token: str) -> str:
    return token.lower().replace(".", "").replace(",", "")


def looks_like_person(name: str) -> bool:
    tokens = [token for token in _TOKEN_SPLIT.split(name.strip()) if token]
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    markers = {_normalize_token(token) for token in tokens}
    if markers & _BUSINESS_MARKERS:
        return False
    return all(_NAME_TOKEN.fullmatch(token) is not None for token in tokens)


def mask_personal_name(name: str) -> str:
    if not looks_like_person(name):
        return name
    tokens = [token for token in _TOKEN_SPLIT.split(name.strip()) if token]
    initials = ". ".join(token[0].upper() for token in tokens) + "."
    return f"Individual · {initials}"


def mask_transaction(transaction: Transaction) -> Transaction:
    canonical = transaction.vendor_canonical
    masked_canonical = mask_personal_name(canonical) if canonical is not None else None
    return transaction.model_copy(
        update={
            "vendor_name_raw": mask_personal_name(transaction.vendor_name_raw),
            "vendor_canonical": masked_canonical,
        }
    )


def json_response(model: BaseModel, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=json.loads(model.model_dump_json()),
        status_code=status_code,
    )


def _read_fixture_file(path: Path) -> FixtureFile:
    if not path.is_file():
        raise FileNotFoundError(
            f"queue fixture not found: {path.as_posix()}. "
            "Commit api/fixture.json so the UI can run without the pipeline."
        )
    return FixtureFile.model_validate_json(path.read_text(encoding="utf-8"))


def _try_load_generated(path: Path) -> FixtureFile | None:
    if not path.is_file():
        return None
    try:
        return FixtureFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(
            "generated queue unreadable path=%s err=%s; using fixture",
            path.as_posix(),
            exc,
        )
        return None


class Store:
    """In-memory queue loaded from demo output or the committed fixture."""

    def __init__(self, fixture: FixtureFile, *, data_source: DataSource) -> None:
        self.funnel = fixture.funnel
        self.data_source = data_source
        self.findings: dict[str, FixtureFinding] = {}
        for finding in fixture.findings:
            self.findings[finding.candidate.candidate_id] = finding
        self.decisions: dict[str, ReviewDecision] = {}

    @classmethod
    def load(cls, path: Path, *, data_source: DataSource = "fixture") -> Store:
        payload = _read_fixture_file(path)
        store = cls(payload, data_source=data_source)
        log.info(
            "loaded queue source=%s findings=%s path=%s",
            store.data_source,
            len(store.findings),
            path.as_posix(),
        )
        return store

    @classmethod
    def load_default(cls) -> Store:
        generated = _try_load_generated(GENERATED_QUEUE_PATH)
        if generated is not None:
            store = cls(generated, data_source="pipeline")
            log.info(
                "loaded queue source=%s findings=%s path=%s",
                store.data_source,
                len(store.findings),
                GENERATED_QUEUE_PATH.as_posix(),
            )
            return store
        return cls.load(FIXTURE_PATH, data_source="fixture")

    def _pending_ids(self) -> list[str]:
        pending: list[str] = []
        for candidate_id, finding in self.findings.items():
            if candidate_id in self.decisions:
                continue
            if finding.verdict.verdict not in _IN_QUEUE:
                continue
            pending.append(candidate_id)
        pending.sort(
            key=lambda candidate_id: (
                -self.findings[candidate_id].candidate.amount_at_risk,
                candidate_id,
            )
        )
        return pending

    def _queue_item(self, finding: FixtureFinding) -> QueueItem:
        masked = mask_transaction(finding.left)
        department = title_department(finding.left.department)
        return QueueItem(
            candidate_id=finding.candidate.candidate_id,
            amount_at_risk=finding.candidate.amount_at_risk,
            vendor_name=masked.vendor_name_raw,
            signal=finding.candidate.signal,
            confidence=finding.verdict.confidence,
            approver=approver_for(finding.left.department),
            department=department,
        )

    def queue(self) -> list[QueueItem]:
        return [
            self._queue_item(self.findings[item_id]) for item_id in self._pending_ids()
        ]

    def candidate(self, candidate_id: str) -> CandidateResponse | None:
        finding = self.findings.get(candidate_id)
        if finding is None:
            return None
        left = mask_transaction(finding.left)
        right = mask_transaction(finding.right)
        department = title_department(finding.left.department)
        return CandidateResponse(
            candidate_id=finding.candidate.candidate_id,
            amount_at_risk=finding.candidate.amount_at_risk,
            signal=finding.candidate.signal,
            approver=approver_for(finding.left.department),
            department=department,
            left=left,
            right=right,
            reasoning=finding.verdict.reasoning,
            evidence=list(finding.verdict.evidence),
            model_used=finding.verdict.model_used,
            confidence=finding.verdict.confidence,
            verdict=finding.verdict.verdict,
        )

    def decide(self, request: DecideRequest) -> ReviewDecision:
        finding = self.findings.get(request.candidate_id)
        if finding is None:
            raise KeyError(request.candidate_id)
        if request.candidate_id in self.decisions:
            raise ValueError(request.candidate_id)
        approver = request.approver.strip()
        if approver == "":
            approver = approver_for(finding.left.department)
        recorded = ReviewDecision(
            candidate_id=request.candidate_id,
            approver=approver,
            decision=request.decision,
            reason=request.reason,
            becomes_rule=request.becomes_rule,
        )
        self.decisions[request.candidate_id] = recorded
        log.info(
            "decision candidate_id=%s decision=%s approver=%s",
            recorded.candidate_id,
            recorded.decision.value,
            recorded.approver,
        )
        return recorded

    def stats(self) -> StatsResponse:
        pending_ids = self._pending_ids()
        dollars = Decimal(0)
        for candidate_id in pending_ids:
            dollars += self.findings[candidate_id].candidate.amount_at_risk
        confirmed = 0
        dismissed = 0
        for recorded in self.decisions.values():
            if recorded.decision is Decision.confirmed:
                confirmed += 1
            elif recorded.decision is Decision.dismissed:
                dismissed += 1
        return StatsResponse(
            rows_in=self.funnel.rows_in,
            transactions=self.funnel.transactions,
            candidates=self.funnel.candidates,
            after_dismiss=self.funnel.after_dismiss,
            after_adjudicate=self.funnel.after_adjudicate,
            dollars_at_risk=dollars,
            pending=len(pending_ids),
            confirmed=confirmed,
            dismissed=dismissed,
            data_source=self.data_source,
        )


def create_app(fixture_path: Path | None = None) -> FastAPI:
    if fixture_path is None:
        store = Store.load_default()
    else:
        store = Store.load(fixture_path, data_source="fixture")
    app = FastAPI(title="Reckon review queue")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/queue")
    def get_queue() -> JSONResponse:
        return json_response(
            QueueResponse(items=store.queue(), data_source=store.data_source)
        )

    @app.get("/candidate/{candidate_id}")
    def get_candidate(candidate_id: str) -> JSONResponse:
        payload = store.candidate(candidate_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        return json_response(payload)

    @app.post("/decide")
    def post_decide(body: DecideRequest) -> JSONResponse:
        try:
            recorded = store.decide(body)
        except KeyError:
            raise HTTPException(status_code=404, detail="candidate not found") from None
        except ValueError:
            raise HTTPException(
                status_code=409, detail="candidate already decided"
            ) from None
        return json_response(recorded)

    @app.get("/stats")
    def get_stats() -> JSONResponse:
        return json_response(store.stats())

    return app


app = create_app()
