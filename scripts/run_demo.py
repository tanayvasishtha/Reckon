"""Run the pipeline on a sample ledger and print the demo funnel.

Complexity: O(R + C) plus stage 5's concurrent model calls for C residual
candidates. This script only orchestrates existing stages; it does not add
pairwise work. Extra memory is the peak of those stages. Row counting is
O(R) and does not materialise the ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from adapters.base import Adapter, AdapterError
from adapters.generic import GenericAdapter, load_column_map
from adapters.la import ENCODING as LA_ENCODING
from adapters.la import LAAdapter
from adapters.oklahoma import ENCODING as OK_ENCODING
from adapters.oklahoma import OklahomaAdapter
from agents.adjudicate import AdjudicationError, adjudicate
from agents.llm import LIVE_MODE, REPLAY_MODE, mode_for
from api.schemas import FixtureFile, FixtureFinding, FunnelSnapshot
from core.aggregate import AggregateError, aggregate
from core.blocking import BlockingError, block
from core.dismiss import dismiss
from core.models import Candidate, Transaction, Verdict, VerdictOutcome
from core.normalize import canonical_invoice, canonical_vendor
from core.settings import Settings, load_settings

try:
    from agents.adjudicate import is_unmatched_recording, summarise_adjudication
except ImportError:
    is_unmatched_recording = None
    summarise_adjudication = None

ROOT = Path(__file__).resolve().parent.parent
LA_SAMPLE = ROOT / "data" / "samples" / "la_sample.csv"
OK_SAMPLE = ROOT / "data" / "samples" / "ok_sample.csv"
GENERATED_QUEUE_PATH = ROOT / "data" / "generated" / "queue.json"

_IN_QUEUE = frozenset({VerdictOutcome.escalate, VerdictOutcome.errored})

NAMED_LA = frozenset({"", "la", "los-angeles", "los_angeles"})
NAMED_OK = frozenset({"oklahoma", "ok"})

FAST_ROW_LIMIT = 20_000

FUNNEL_ROWS_IN = "rows in"
FUNNEL_TRANSACTIONS = "transactions after aggregation"
FUNNEL_CANDIDATES = "candidates after blocking"
FUNNEL_AFTER_DISMISS = "survivors after deterministic dismissal"
FUNNEL_AFTER_ADJUDICATE = "survivors after adjudication"
FUNNEL_DOLLARS = "dollars at risk"
FUNNEL_NO_RECORDING = "no recording"
PASSED_THROUGH = "passed through untouched"


class DemoError(Exception):
    """The demo cannot start or finish; the message is safe to print as one line."""


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    label: str
    path: Path
    adapter: Adapter
    encoding: str


@dataclass(frozen=True, slots=True)
class AdjudicationFunnel:
    """Four-way Stage 5 counts. errored is genuine failures only."""

    dismissed: int
    escalated: int
    errored: int
    unmatched_recordings: int
    passed_through: bool

    @property
    def rows_in(self) -> int:
        return (
            self.dismissed + self.escalated + self.errored + self.unmatched_recordings
        )


def display_path(path: Path, root: Path | None = None) -> str:
    """Return a forward-slash path, relative to the repo when possible."""
    base = ROOT if root is None else root
    resolved = path.resolve()
    try:
        return resolved.relative_to(base.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def one_line(exc: BaseException) -> str:
    """Collapse an exception message into a single line."""
    text = str(exc).replace("\r", " ").replace("\n", " ").replace("\\", "/").strip()
    while "  " in text:
        text = text.replace("  ", " ")
    return text


def current_commit_sha(repo: Path | None = None) -> str:
    """Return HEAD from the local repo, or 'unknown' when git is unavailable."""
    cwd = ROOT if repo is None else repo
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "unknown"
    sha = completed.stdout.strip()
    if completed.returncode != 0 or sha == "":
        return "unknown"
    return sha


def resolve_source(source: str, map_path: str) -> ResolvedSource:
    """Pick an adapter for a named sample or a generic CSV plus YAML map."""
    key = source.strip().lower()
    if key in NAMED_LA:
        return _named_source(LA_SAMPLE, LAAdapter(), LA_ENCODING)
    if key in NAMED_OK:
        return _named_source(OK_SAMPLE, OklahomaAdapter(), OK_ENCODING)

    looks_like_path = (
        "/" in source
        or "\\" in source
        or key.endswith(".csv")
        or Path(source).is_file()
    )
    if not looks_like_path:
        raise DemoError(
            f"unknown SOURCE {source!r}. "
            "Use la, oklahoma, or a CSV path with MAP=path/to/map.yaml."
        )
    if map_path.strip() == "":
        raise DemoError(
            "MAP is required for a custom CSV. "
            "Provide MAP=path/to/map.yaml. See adapters/maps/example.yaml."
        )
    csv_path = Path(source)
    yaml_path = Path(map_path)
    if not yaml_path.is_file():
        raise DemoError(
            f"column map not found: {display_path(yaml_path)}. "
            "Provide a YAML map path. See adapters/maps/example.yaml."
        )
    if not csv_path.is_file():
        raise DemoError(
            f"ledger not found: {display_path(csv_path)}. "
            "Provide a path to an existing CSV file."
        )
    try:
        encoding, _columns = load_column_map(yaml_path)
        adapter = GenericAdapter(yaml_path)
    except AdapterError as exc:
        raise DemoError(one_line(exc)) from exc
    return ResolvedSource(
        label=display_path(csv_path),
        path=csv_path,
        adapter=adapter,
        encoding=encoding,
    )


def _named_source(path: Path, adapter: Adapter, encoding: str) -> ResolvedSource:
    if not path.is_file():
        raise DemoError(
            f"ledger not found: {display_path(path)}. "
            "Provide a path to an existing CSV file."
        )
    return ResolvedSource(
        label=display_path(path),
        path=path,
        adapter=adapter,
        encoding=encoding,
    )


def count_csv_rows(path: Path, encoding: str, *, limit: int | None = None) -> int:
    """Count non-blank data rows without loading the ledger into memory."""
    try:
        with path.open(encoding=encoding, newline="") as handle:
            reader = csv.reader(handle)
            try:
                next(reader)
            except StopIteration:
                return 0
            n = 0
            for row in reader:
                if not any(cell.strip() for cell in row):
                    continue
                n += 1
                if limit is not None and n >= limit:
                    return n
            return n
    except UnicodeDecodeError as exc:
        raise DemoError(
            f"could not decode {display_path(path)} as {encoding}. "
            "Set the encoding this file actually uses."
        ) from exc
    except OSError as exc:
        raise DemoError(
            f"could not read {display_path(path)}: {exc}. "
            "Check the path and file permissions."
        ) from exc


def load_rows(
    adapter: Adapter, path: Path, *, limit: int | None = None
) -> list[Transaction]:
    """Yield Transaction rows from the adapter, stopping at limit if set."""
    rows: list[Transaction] = []
    try:
        for transaction in adapter.iter_transactions(path):
            rows.append(transaction)
            if limit is not None and len(rows) >= limit:
                break
    except AdapterError as exc:
        raise DemoError(one_line(exc)) from exc
    except UnicodeDecodeError as exc:
        raise DemoError(
            f"could not decode {display_path(path)} as the adapter encoding. "
            "Set the encoding this file actually uses."
        ) from exc
    return rows


def normalise_transactions(transactions: Sequence[Transaction]) -> list[Transaction]:
    """Set vendor_canonical and invoice_canonical on each aggregated row."""
    result = [_normalise_one(item) for item in transactions]
    result.sort(key=lambda item: item.transaction_id)
    return result


def _normalise_one(transaction: Transaction) -> Transaction:
    return transaction.model_copy(
        update={
            "vendor_canonical": canonical_vendor(transaction.vendor_name_raw),
            "invoice_canonical": canonical_invoice(transaction.invoice_number_raw),
        }
    )


def survivors_after_adjudication(
    residual: Sequence[Candidate], verdicts: Sequence[Verdict]
) -> list[Candidate]:
    """Return residual candidates that the model did not dismiss."""
    dismissed_ids = {
        item.candidate_id for item in verdicts if item.verdict is VerdictOutcome.dismiss
    }
    return [item for item in residual if item.candidate_id not in dismissed_ids]


def build_generated_queue(
    *,
    rows_in: int,
    transaction_count: int,
    candidate_count: int,
    residual_count: int,
    surviving: Sequence[Candidate],
    verdicts: Sequence[Verdict],
    ledger: Sequence[Transaction],
) -> FixtureFile:
    """Build a FixtureFile of residual escalate/errored findings, sorted by id."""
    tx_by_id = {row.transaction_id: row for row in ledger}
    verdict_by_id = {item.candidate_id: item for item in verdicts}
    findings: list[FixtureFinding] = []
    for candidate in surviving:
        verdict = verdict_by_id.get(candidate.candidate_id)
        if verdict is None:
            raise DemoError(
                f"queue snapshot missing verdict for {candidate.candidate_id}"
            )
        if verdict.verdict not in _IN_QUEUE:
            raise DemoError(
                f"queue snapshot unexpected verdict {verdict.verdict.value} "
                f"for {candidate.candidate_id}"
            )
        left_id, right_id = candidate.transaction_ids
        left = tx_by_id.get(left_id)
        right = tx_by_id.get(right_id)
        if left is None or right is None:
            missing = left_id if left is None else right_id
            raise DemoError(
                f"queue snapshot missing transaction {missing} "
                f"for {candidate.candidate_id}"
            )
        findings.append(
            FixtureFinding(
                candidate=candidate,
                left=left,
                right=right,
                verdict=verdict,
            )
        )
    findings.sort(key=lambda item: item.candidate.candidate_id)
    return FixtureFile(
        funnel=FunnelSnapshot(
            rows_in=rows_in,
            transactions=transaction_count,
            candidates=candidate_count,
            after_dismiss=residual_count,
            after_adjudicate=len(findings),
        ),
        findings=findings,
    )


def write_generated_queue(path: Path, fixture: FixtureFile) -> None:
    """Write FixtureFile JSON with sorted keys. Fail with one-line DemoError."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads(fixture.model_dump_json())
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        path.write_text(serialized, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise DemoError(
            f"could not write {display_path(path)}: {one_line(exc)}"
        ) from exc


def dollars_at_risk(candidates: Iterable[Candidate]) -> Decimal:
    """Sum amount_at_risk as Decimal. Never converts through float."""
    total = Decimal(0)
    for item in candidates:
        total += item.amount_at_risk
    return total


def format_money(value: Decimal) -> str:
    """Serialise money as a decimal string, never a float."""
    return format(value, "f")


def emit(out: TextIO, label: str, value: object) -> None:
    """Print one labelled funnel or header line and flush for the camera."""
    print(f"{label}: {value}", file=out, flush=True)


def _fallback_is_unmatched_recording(verdict: Verdict) -> bool:
    """Unmatched iff reasoning starts with 'no recording:' or contains 'no cassette'."""
    reasoning = verdict.reasoning
    return reasoning.startswith("no recording:") or "no cassette" in reasoning


def _count_adjudication_locally(verdicts: Sequence[Verdict]) -> AdjudicationFunnel:
    unmatched_fn = (
        is_unmatched_recording
        if is_unmatched_recording is not None
        else _fallback_is_unmatched_recording
    )
    dismissed = 0
    escalated = 0
    genuine_errored = 0
    unmatched = 0
    for item in verdicts:
        if unmatched_fn(item):
            unmatched += 1
            continue
        if item.verdict is VerdictOutcome.dismiss:
            dismissed += 1
        elif item.verdict is VerdictOutcome.escalate:
            escalated += 1
        elif item.verdict is VerdictOutcome.errored:
            genuine_errored += 1
    return AdjudicationFunnel(
        dismissed=dismissed,
        escalated=escalated,
        errored=genuine_errored,
        unmatched_recordings=unmatched,
        passed_through=dismissed == 0 and genuine_errored == 0 and unmatched == 0,
    )


def adjudication_funnel(verdicts: Sequence[Verdict]) -> AdjudicationFunnel:
    """Return dismissed, escalated, genuine errored, unmatched. Sum equals len(verdicts)."""
    if summarise_adjudication is None or is_unmatched_recording is None:
        return _count_adjudication_locally(verdicts)
    summary = summarise_adjudication(verdicts)
    unmatched = sum(1 for item in verdicts if is_unmatched_recording(item))
    dismissed = int(summary.dismissed)
    escalated = int(summary.escalated)
    errored = int(summary.errored)
    return AdjudicationFunnel(
        dismissed=dismissed,
        escalated=escalated,
        errored=errored,
        unmatched_recordings=unmatched,
        passed_through=dismissed == 0 and errored == 0 and unmatched == 0,
    )


def print_aggregate_funnel(out: TextIO, rows_in: int, rows_out: int) -> None:
    """Print aggregate in/out and collapsed count, or pass-through when unchanged."""
    emit(out, "aggregate in", rows_in)
    emit(out, "aggregate out", rows_out)
    if rows_in == rows_out:
        emit(out, "aggregate", PASSED_THROUGH)
    else:
        emit(out, "aggregate collapsed", rows_in - rows_out)
    emit(out, FUNNEL_TRANSACTIONS, rows_out)


def print_blocking_funnel(
    out: TextIO, rows_in: int, candidates: Sequence[Candidate]
) -> None:
    """Print blocking in/out and a count for each candidate.signal."""
    emit(out, "blocking in", rows_in)
    emit(out, "blocking out", len(candidates))
    counts = Counter(item.signal.value for item in candidates)
    for signal_name in sorted(counts):
        emit(out, f"blocking {signal_name}", counts[signal_name])
    emit(out, FUNNEL_CANDIDATES, len(candidates))


def print_dismissal_funnel(
    out: TextIO,
    rows_in: int,
    residual: Sequence[Candidate],
    dismissed: Sequence[Verdict],
) -> None:
    """Print dismissal in/out and dismissed counts by explanation_type."""
    emit(out, "dismissal in", rows_in)
    emit(out, "dismissal out", len(residual))
    counts = Counter(item.explanation_type.value for item in dismissed)
    for name in sorted(counts):
        emit(out, f"dismissal {name}", counts[name])
    if len(dismissed) == 0:
        emit(out, "dismissal", PASSED_THROUGH)
    emit(out, FUNNEL_AFTER_DISMISS, len(residual))


def print_adjudication_funnel(
    out: TextIO,
    *,
    rows_in: int,
    rows_out: int,
    verdicts: Sequence[Verdict],
) -> None:
    """Print Stage 5 in/out, four-way counts, unmatched recordings, and survivors."""
    funnel = adjudication_funnel(verdicts)
    emit(out, "adjudication in", rows_in)
    emit(out, "adjudication out", rows_out)
    emit(out, "adjudication dismissed", funnel.dismissed)
    emit(out, "adjudication escalated", funnel.escalated)
    emit(out, "adjudication errored", funnel.errored)
    emit(out, "adjudication unmatched_recordings", funnel.unmatched_recordings)
    if funnel.passed_through:
        emit(out, "adjudication", PASSED_THROUGH)
    emit(
        out,
        FUNNEL_NO_RECORDING,
        f"{funnel.unmatched_recordings} candidates had no matching recording",
    )
    emit(out, FUNNEL_AFTER_ADJUDICATE, rows_out)


def print_header(
    out: TextIO,
    *,
    source: str,
    row_count: int,
    concurrency: int,
    mode: str,
    commit: str,
) -> None:
    """Print the run header before any funnel numbers."""
    emit(out, "source", source)
    emit(out, "rows", row_count)
    emit(out, "concurrency", concurrency)
    emit(out, "mode", mode)
    emit(out, "commit", commit)


def run_demo(
    *,
    source: str = "",
    map_path: str = "",
    fast: bool = False,
    settings: Settings | None = None,
    output: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    repo: Path | None = None,
) -> None:
    """Run stages 1 to 5 and print the header then the funnel as it happens."""
    out = sys.stdout if output is None else output
    cfg = settings if settings is not None else load_settings(environ)
    resolved = resolve_source(source, map_path)
    limit = FAST_ROW_LIMIT if fast else None
    row_count = count_csv_rows(resolved.path, resolved.encoding, limit=limit)
    mode = mode_for(cfg)
    if mode not in {LIVE_MODE, REPLAY_MODE}:
        mode = REPLAY_MODE
    print_header(
        out,
        source=resolved.label,
        row_count=row_count,
        concurrency=cfg.concurrency_limit,
        mode=mode,
        commit=current_commit_sha(repo),
    )

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        rows = load_rows(resolved.adapter, resolved.path, limit=limit)
        emit(out, FUNNEL_ROWS_IN, len(rows))

        aggregated = aggregate(rows)
        print_aggregate_funnel(out, len(rows), len(aggregated))

        normalised = normalise_transactions(aggregated)
        candidates = block(normalised, resolved.adapter.capabilities, cfg)
        print_blocking_funnel(out, len(aggregated), candidates)

        residual, dismissed = dismiss(candidates, normalised)
        print_dismissal_funnel(out, len(candidates), residual, dismissed)

        verdicts = asyncio.run(adjudicate(residual, normalised, cfg))
        surviving = survivors_after_adjudication(residual, verdicts)
        print_adjudication_funnel(
            out,
            rows_in=len(residual),
            rows_out=len(surviving),
            verdicts=verdicts,
        )
        emit(out, FUNNEL_DOLLARS, format_money(dollars_at_risk(surviving)))
        snapshot = build_generated_queue(
            rows_in=len(rows),
            transaction_count=len(aggregated),
            candidate_count=len(candidates),
            residual_count=len(residual),
            surviving=surviving,
            verdicts=verdicts,
            ledger=normalised,
        )
        write_generated_queue(GENERATED_QUEUE_PATH, snapshot)
    except (
        AdapterError,
        AggregateError,
        BlockingError,
        AdjudicationError,
        DemoError,
    ) as exc:
        raise DemoError(one_line(exc)) from exc
    finally:
        logging.disable(previous_disable)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DemoError(message)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _ArgumentParser(
        prog="scripts.run_demo",
        description="Run the Reckon pipeline and print the demo funnel.",
    )
    parser.add_argument(
        "--source",
        default="",
        help="la (default), oklahoma, or a path to a CSV file",
    )
    parser.add_argument(
        "--map",
        default="",
        dest="map_path",
        help="YAML column map; required when SOURCE is a CSV path",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=f"limit the run to {FAST_ROW_LIMIT} rows",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry. Returns 0 on success. Failures are one line on stderr."""
    try:
        args = parse_args(argv)
        run_demo(source=args.source, map_path=args.map_path, fast=args.fast)
    except DemoError as exc:
        print(one_line(exc), file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(one_line(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
