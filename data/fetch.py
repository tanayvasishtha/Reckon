"""Stream public payment ledgers to disk with on-disk caching.

Complexity: O(n) in downloaded bytes. Extra memory is O(chunk size), or
O(longest CSV record) when a row cap is applied. The full response is
never buffered.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import httpx

log = logging.getLogger("data.fetch")

LA_SOURCE = "la"
OKLAHOMA_SOURCE = "oklahoma"
SOURCES = (LA_SOURCE, OKLAHOMA_SOURCE)

LA_BASE_URL = "https://controllerdata.lacity.org/resource/pggv-e4fn.csv"
LA_FISCAL_YEAR = "2024"
OKLAHOMA_URL = (
    "https://data.ok.gov/dataset/d58f70a8-1885-418b-8d6c-02bb38f5c1c2"
    "/resource/449f837d-d576-49af-8f6d-3a69e5289964"
    "/download/vendors_public_2021q1.csv"
)

LA_FILENAME = "la.csv"
OKLAHOMA_FILENAME = "oklahoma.csv"

CHUNK_SIZE_BYTES = 64 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 60.0
USER_AGENT = "reckon-data-fetch/0.1"

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_GENERATED_DIR = PACKAGE_DIR / "generated"
DEFAULT_SAMPLES_DIR = PACKAGE_DIR / "samples"

LA_SAMPLE_ROWS = 20_000
OKLAHOMA_SAMPLE_ROWS = 5_000
LA_SAMPLE_NAME = "la_sample.csv"
OKLAHOMA_SAMPLE_NAME = "ok_sample.csv"

_sleep = time.sleep


class FetchError(Exception):
    """A ledger download failed or was given invalid arguments."""


class TransientFetchError(FetchError):
    """A retryable network or HTTP failure."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def la_download_url(limit: int) -> str:
    """Return the verified Socrata CSV URL for one fiscal year page."""
    if limit < 1:
        raise FetchError("--limit must be a positive integer")
    return f"{LA_BASE_URL}?$where=fiscal_year='{LA_FISCAL_YEAR}'&$limit={limit}"


def oklahoma_download_url() -> str:
    """Return the direct Oklahoma vendor-payments CSV URL."""
    return OKLAHOMA_URL


def destination_for(source: str, dest_dir: Path) -> Path:
    """Return the on-disk path for a named source inside dest_dir."""
    if source == LA_SOURCE:
        return dest_dir / LA_FILENAME
    if source == OKLAHOMA_SOURCE:
        return dest_dir / OKLAHOMA_FILENAME
    raise FetchError(f"unknown source {source!r}; choose 'la' or 'oklahoma'")


def _build_client() -> httpx.Client:
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=READ_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )
    return httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/csv,application/octet-stream,*/*",
        },
    )


def _is_cached(dest: Path) -> bool:
    return dest.is_file() and dest.stat().st_size > 0


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return min(parsed, MAX_BACKOFF_SECONDS)


def _raise_for_status(response: httpx.Response, url: str) -> None:
    status = response.status_code
    if status == 429 or status >= 500:
        raise TransientFetchError(
            f"HTTP {status} for {url}",
            retry_after=_retry_after_seconds(response),
        )
    if status != 200:
        raise FetchError(f"HTTP {status} for {url}. Check the source URL and re-run.")


def _iter_csv_records(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Yield complete CSV records, including quoted embedded newlines."""
    buffer = bytearray()
    in_quotes = False
    quote = 0x22
    newline = 0x0A
    i = 0
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        start = 0
        while i < len(buffer):
            b = buffer[i]
            if b == quote:
                if in_quotes:
                    if i + 1 >= len(buffer):
                        break
                    if buffer[i + 1] == quote:
                        i += 2
                        continue
                    in_quotes = False
                    i += 1
                    continue
                in_quotes = True
                i += 1
                continue
            if b == newline and not in_quotes:
                yield bytes(buffer[start : i + 1])
                start = i + 1
                i += 1
                continue
            i += 1
        if start:
            del buffer[:start]
            i -= start
        if len(buffer) > MAX_LINE_BYTES:
            raise FetchError(f"CSV record exceeded {MAX_LINE_BYTES} bytes; aborting")
    if buffer:
        yield bytes(buffer)


def _write_stream(
    chunks: Iterable[bytes],
    out: Path,
    max_data_rows: int | None,
) -> tuple[int, int | None]:
    bytes_written = 0
    data_rows = 0
    header_done = False
    with out.open("wb") as handle:
        if max_data_rows is None:
            for chunk in chunks:
                if not chunk:
                    continue
                handle.write(chunk)
                bytes_written += len(chunk)
            return bytes_written, None
        for record in _iter_csv_records(chunks):
            handle.write(record)
            bytes_written += len(record)
            if not header_done:
                header_done = True
                continue
            data_rows += 1
            if data_rows >= max_data_rows:
                break
    return bytes_written, data_rows


def _attempt_download(
    client: httpx.Client,
    url: str,
    part: Path,
    max_data_rows: int | None,
) -> tuple[int, int | None]:
    try:
        with client.stream("GET", url) as response:
            _raise_for_status(response, url)
            return _write_stream(
                response.iter_bytes(chunk_size=CHUNK_SIZE_BYTES),
                part,
                max_data_rows,
            )
    except httpx.TransportError as exc:
        raise TransientFetchError(f"network error for {url}: {exc}") from exc


def download_url(
    url: str,
    dest: Path,
    *,
    force: bool = False,
    client: httpx.Client | None = None,
    max_data_rows: int | None = None,
) -> Path:
    """Stream url onto dest unless dest already exists.

    max_data_rows, when set, copies the header plus that many data rows and
    then closes the connection. Used to materialize committed samples.
    """
    if max_data_rows is not None and max_data_rows < 1:
        raise FetchError("max_data_rows must be a positive integer")
    if _is_cached(dest) and not force:
        log.info("fetch skip cache_hit dest=%s", dest)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if part.exists():
        part.unlink()

    own_client = client is None
    if client is None:
        client = _build_client()

    run_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    log.info("fetch start url=%s dest=%s run_id=%s", url, dest, run_id)

    last_error: TransientFetchError | None = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                bytes_written, rows_written = _attempt_download(
                    client, url, part, max_data_rows
                )
                if bytes_written == 0:
                    raise FetchError(
                        f"download from {url} was empty. "
                        "Check the source URL and re-run."
                    )
                part.replace(dest)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                log.info(
                    "fetch done dest=%s bytes=%s rows=%s elapsed_ms=%s run_id=%s",
                    dest,
                    bytes_written,
                    rows_written,
                    elapsed_ms,
                    run_id,
                )
                return dest
            except TransientFetchError as exc:
                last_error = exc
                if part.exists():
                    part.unlink()
                if attempt >= MAX_ATTEMPTS:
                    break
                if exc.retry_after is not None:
                    delay = exc.retry_after
                else:
                    delay = INITIAL_BACKOFF_SECONDS * (
                        BACKOFF_MULTIPLIER ** (attempt - 1)
                    )
                    delay = min(delay, MAX_BACKOFF_SECONDS)
                log.warning(
                    "fetch retry attempt=%s/%s sleep_s=%s error=%s run_id=%s",
                    attempt,
                    MAX_ATTEMPTS,
                    delay,
                    exc,
                    run_id,
                )
                _sleep(delay)
            except FetchError:
                if part.exists():
                    part.unlink()
                raise
    finally:
        if own_client:
            client.close()
        if part.exists():
            part.unlink()

    raise FetchError(
        f"download failed after {MAX_ATTEMPTS} attempts: {last_error}. "
        "Re-run once the source recovers."
    ) from last_error


def _read_file_chunks(src: Path) -> Iterator[bytes]:
    with src.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE_BYTES)
            if not chunk:
                return
            yield chunk


def slice_csv_rows(src: Path, dest: Path, n_data_rows: int) -> int:
    """Copy header plus n_data_rows from src to dest. Returns rows written."""
    if n_data_rows < 1:
        raise FetchError("n_data_rows must be a positive integer")
    if not src.is_file():
        raise FetchError(f"missing CSV {src}. Pass a real file path.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    records = _iter_csv_records(_read_file_chunks(src))
    try:
        header = next(records)
    except StopIteration as exc:
        raise FetchError(f"{src} is empty") from exc
    written = 0
    with dest.open("wb") as outgoing:
        outgoing.write(header)
        for record in records:
            outgoing.write(record)
            written += 1
            if written >= n_data_rows:
                break
    return written


def fetch(
    source: str,
    *,
    dest_dir: Path,
    limit: int | None = None,
    force: bool = False,
    client: httpx.Client | None = None,
) -> Path:
    """Download one named source into dest_dir. Returns the target path."""
    if source == LA_SOURCE:
        if limit is None:
            raise FetchError("source 'la' requires --limit (Socrata $limit)")
        url = la_download_url(limit)
    elif source == OKLAHOMA_SOURCE:
        if limit is not None:
            raise FetchError("--limit is only valid for source 'la'")
        url = oklahoma_download_url()
    else:
        raise FetchError(f"unknown source {source!r}; choose 'la' or 'oklahoma'")

    dest = destination_for(source, dest_dir)
    return download_url(url, dest, force=force, client=client)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m data.fetch",
        description="Download public Los Angeles and Oklahoma payment ledgers.",
    )
    parser.add_argument("source", choices=SOURCES)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Socrata row cap; required for source 'la'.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download even if the target file already exists.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry. Returns a process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = _parse_args(argv)
    try:
        dest = fetch(
            args.source,
            dest_dir=DEFAULT_GENERATED_DIR,
            limit=args.limit,
            force=args.force,
        )
    except FetchError as exc:
        log.error("%s", exc)
        return 1
    log.info("wrote %s", dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
