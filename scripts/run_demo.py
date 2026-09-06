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
import logging
import subprocess
import sys
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
from core.aggregate import AggregateError, aggregate
from core.blocking import BlockingError, block
from core.dismiss import dismiss
from core.models import Candidate, Transaction, Verdict, VerdictOutcome
from core.normalize import canonical_invoice, canonical_vendor
from core.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parent.parent
LA_SAMPLE = ROOT / "data" / "samples" / "la_sample.csv"
OK_SAMPLE = ROOT / "data" / "samples" / "ok_sample.csv"

NAMED_LA = frozenset({"", "la", "los-angeles", "los_angeles"})
NAMED_OK = frozenset({"oklahoma", "ok"})

FAST_ROW_LIMIT = 20_000

FUNNEL_ROWS_IN = "rows in"
FUNNEL_TRANSACTIONS = "transactions after aggregation"
FUNNEL_CANDIDATES = "candidates after blocking"
FUNNEL_AFTER_DISMISS = "survivors after deterministic dismissal"
FUNNEL_AFTER_ADJUDICATE = "survivors after adjudication"
FUNNEL_DOLLARS = "dollars at risk"


class DemoError(Exception):
    """The demo cannot start or finish; the message is safe to print as one line."""


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    label: str
    path: Path
    adapter: Adapter
    encoding: str


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
        emit(out, FUNNEL_TRANSACTIONS, len(aggregated))

        normalised = normalise_transactions(aggregated)
        candidates = block(normalised, resolved.adapter.capabilities, cfg)
        emit(out, FUNNEL_CANDIDATES, len(candidates))

        residual, _dismissed = dismiss(candidates, normalised)
        emit(out, FUNNEL_AFTER_DISMISS, len(residual))

        verdicts = asyncio.run(adjudicate(residual, normalised, cfg))
        surviving = survivors_after_adjudication(residual, verdicts)
        emit(out, FUNNEL_AFTER_ADJUDICATE, len(surviving))
        emit(out, FUNNEL_DOLLARS, format_money(dollars_at_risk(surviving)))
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
