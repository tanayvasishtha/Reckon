# Engineering standards

Every contributor and every agent worker follows this file. A change that violates
any of it is not done, whatever the commit message claims.

## Complexity

Blocking is where this breaks. A naive pairwise comparison over 700,000
transactions is 2.4 x 10^11 pairs and will never finish.

Blocking must be hash-bucketed. Group by a composite key such as
`(vendor_canonical, invoice_canonical, amount)` and compare only inside a
bucket. Cap bucket size at 200 members and split larger buckets by a secondary
key, so one high-volume vendor cannot blow up the run.

Every stage states its complexity in its module docstring. A stage that cannot
state its complexity is not finished.

## Memory

The full Los Angeles ledger is about 5 GB. Nothing loads a whole file.
Ingest is chunked, either `pandas.read_csv(..., chunksize=...)` or a plain
`csv` reader. Peak resident memory stays under 1 GB for any input size, and a
test asserts that ceiling on a 200,000-row file.

## Types and linting

Full type hints on every public function. `mypy --strict` is clean on `core/`,
`adapters/` and `agents/`. `ruff check` and `ruff format` are clean everywhere.
CI fails on any of the three.

## Tests

Every module ships with its tests in the same change. No live API calls in CI.
Stage 5 tests run against recorded cassettes. Coverage is not a target, but
every branch in the blocking and dismissal logic has a case, because that is
where correctness lives.

## Reproducibility

Same input, same output, every time. Sort before writing. No dependence on dict
ordering, set iteration order, or wall-clock time inside logic. A test runs the
pipeline twice on one fixture and diffs the output byte for byte.

## Money

Every monetary value is `Decimal`. Never float, not briefly, not for a
comparison. Parse with `Decimal(str(x))`. Serialise as a string in JSON.
One float in a currency path is a correctness bug.

## Errors

No bare `except`. No `except Exception: pass`. Define the exceptions the
pipeline raises. API calls retry with exponential backoff on 429 and 5xx, then
fail with a clear message rather than returning a silent default. A candidate
that fails adjudication is marked `errored` and appears in the queue. It is
never silently dropped.

## Observability

Structured logging with a run id on every line. Use `logging`, never `print`,
outside the demo script. Each stage logs rows in, rows out, and elapsed
milliseconds. The run header prints the config that produced the run: source,
row count, concurrency, models, commit sha, and whether it ran live or in
replay.

## Configuration

One `Settings` object, read from environment with defaults. No magic numbers in
function bodies. Thresholds, model names, concurrency limits and bucket caps
are all configuration.

## Idempotency

Running twice does not double-post, double-count, or duplicate a packet.
Writes are keyed on a stable id.

## Performance budget

These are tests in CI against a generated fixture, not aspirations.

- Stages 1 to 4 process 200,000 rows in under 30 seconds on a laptop.
- Stage 5 clears 300 candidates in under 90 seconds at concurrency 16.

## Concurrency

Stage 5 makes one model call per candidate and must run concurrently.
`asyncio.gather` behind a semaphore, default 16 in flight, concurrency printed
in the run header. Sort results by candidate id afterwards so runs stay
reproducible. Retries back off without collapsing the pool.

## Running on someone else's machine

Assume a reviewer clones this on macOS, has no API key, has never used `uv`,
and gives it five minutes.

- **Runs with no API key.** Recorded cassettes live in `fixtures/cassettes/`,
  keyed on a hash of the candidate payload. With no key set, the pipeline runs
  in replay mode against them: full output, deterministic, offline, free. With
  `RECKON_API_KEY` and `RECKON_API_BASE` set it runs live against any
  OpenAI-compatible endpoint. The run header states which mode it is in.
- **One command.** `git clone`, then `make demo`. Under three minutes cold,
  including install. `make demo-fast` runs a 20,000-row slice in under 30
  seconds.
- **Sample data is committed.** A 20,000-row Los Angeles slice and a 5,000-row
  Oklahoma slice, with attribution in `data/samples/`. Nobody downloads 5 GB to
  see it work. `make fetch` pulls the full ledgers.
- **Cross platform.** Developed on Windows, runs on macOS and Linux. `pathlib`
  everywhere, no backslash paths, no `os.system`, no platform-locked
  dependency. CI runs on `ubuntu-latest` and `macos-latest`.
- **Their data in three lines.** `adapters/generic.py` plus a YAML column map.
  `make demo SOURCE=path/to/your.csv MAP=path/to/map.yaml`.
- **Fails clearly.** A missing file, a bad column map, or an unreadable
  encoding produces one line saying what is wrong and what to do. Never a
  stack trace.
- **No secrets, no telemetry.** Replay mode makes no network calls.
