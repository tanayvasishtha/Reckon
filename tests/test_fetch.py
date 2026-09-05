from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from data.fetch import (
    DEFAULT_SAMPLES_DIR,
    FetchError,
    _iter_csv_records,
    download_url,
    fetch,
    la_download_url,
    main,
    oklahoma_download_url,
    slice_csv_rows,
)

LA_CSV = b"fiscal_year,vendor_name,dollar_amount\n2024,ACME,10.00\n2024,BETA,2.50\n"
OK_CSV = "vendor,amount\nCAFÉ,1.00\n".encode("cp1252")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_la_url_matches_verified_socrata_form() -> None:
    url = la_download_url(20000)
    assert url.startswith("https://controllerdata.lacity.org/resource/pggv-e4fn.csv?")
    assert "fiscal_year='2024'" in url
    assert "$limit=20000" in url


def test_oklahoma_url_is_direct_csv() -> None:
    url = oklahoma_download_url()
    assert url.endswith("/download/vendors_public_2021q1.csv")
    assert "data.ok.gov" in url


def test_fetch_la_streams_to_dest(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=LA_CSV)

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=100,
        client=_client(handler),
    )
    assert dest == tmp_path / "la.csv"
    assert dest.read_bytes() == LA_CSV
    assert "$limit=100" in seen[0]
    assert "fiscal_year='2024'" in seen[0]
    assert not dest.with_name("la.csv.part").exists()


def test_fetch_oklahoma_preserves_cp1252_bytes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("vendors_public_2021q1.csv")
        return httpx.Response(200, content=OK_CSV)

    dest = fetch(
        "oklahoma",
        dest_dir=tmp_path,
        client=_client(handler),
    )
    assert dest.read_bytes() == OK_CSV
    assert dest.read_bytes().decode("cp1252") == "vendor,amount\nCAFÉ,1.00\n"


def test_skip_existing_does_not_hit_handler(tmp_path: Path) -> None:
    target = tmp_path / "la.csv"
    target.write_bytes(b"cached\n")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b"nope")

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=100,
        client=_client(handler),
    )
    assert dest.read_bytes() == b"cached\n"
    assert calls["n"] == 0


def test_empty_existing_file_is_not_a_cache_hit(tmp_path: Path) -> None:
    (tmp_path / "la.csv").write_bytes(b"")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=LA_CSV)

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=5,
        client=_client(handler),
    )
    assert dest.read_bytes() == LA_CSV


def test_force_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / "la.csv").write_bytes(b"old\n")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=LA_CSV)

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=5,
        force=True,
        client=_client(handler),
    )
    assert dest.read_bytes() == LA_CSV


def test_retry_on_429_then_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("data.fetch._sleep", sleeps.append)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"}, content=b"slow")
        return httpx.Response(200, content=LA_CSV)

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=5,
        client=_client(handler),
    )
    assert dest.read_bytes() == LA_CSV
    assert calls["n"] == 2
    assert sleeps == [0.5]


def test_retry_on_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch._sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"down")
        return httpx.Response(200, content=LA_CSV)

    dest = fetch(
        "la",
        dest_dir=tmp_path,
        limit=5,
        client=_client(handler),
    )
    assert dest.read_bytes() == LA_CSV
    assert calls["n"] == 3


def test_404_does_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch._sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, content=b"missing")

    with pytest.raises(FetchError, match="HTTP 404"):
        fetch("la", dest_dir=tmp_path, limit=5, client=_client(handler))
    assert calls["n"] == 1
    assert not (tmp_path / "la.csv").exists()
    assert not (tmp_path / "la.csv.part").exists()


def test_exhausted_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch._sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, content=b"bad gateway")

    with pytest.raises(FetchError, match="after 5 attempts"):
        fetch("la", dest_dir=tmp_path, limit=5, client=_client(handler))
    assert calls["n"] == 5
    assert not (tmp_path / "la.csv").exists()
    assert not (tmp_path / "la.csv.part").exists()


def test_la_requires_limit(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="requires --limit"):
        fetch("la", dest_dir=tmp_path)


def test_oklahoma_rejects_limit(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="only valid for source 'la'"):
        fetch("oklahoma", dest_dir=tmp_path, limit=10)


def test_unknown_source(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="unknown source"):
        fetch("nyc", dest_dir=tmp_path)


def test_invalid_limit() -> None:
    with pytest.raises(FetchError, match="positive integer"):
        la_download_url(0)


def test_empty_download_is_an_error(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with pytest.raises(FetchError, match="empty"):
        fetch("la", dest_dir=tmp_path, limit=5, client=_client(handler))
    assert not (tmp_path / "la.csv").exists()


def test_download_url_row_cap(tmp_path: Path) -> None:
    body = b"h1,h2\n1,a\n2,b\n3,c\n4,d\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    dest = tmp_path / "slice.csv"
    download_url(
        "https://example.test/ledger.csv",
        dest,
        client=_client(handler),
        max_data_rows=2,
    )
    assert dest.read_bytes() == b"h1,h2\n1,a\n2,b\n"


def test_slice_csv_rows(tmp_path: Path) -> None:
    src = tmp_path / "full.csv"
    src.write_bytes(b"h\n1\n2\n3\n4\n")
    dest = tmp_path / "out.csv"
    written = slice_csv_rows(src, dest, 3)
    assert written == 3
    assert dest.read_bytes() == b"h\n1\n2\n3\n"


def test_slice_csv_rows_short_file(tmp_path: Path) -> None:
    src = tmp_path / "full.csv"
    src.write_bytes(b"h\n1\n")
    dest = tmp_path / "out.csv"
    written = slice_csv_rows(src, dest, 50)
    assert written == 1
    assert dest.read_bytes() == b"h\n1\n"


def test_row_cap_keeps_quoted_newlines(tmp_path: Path) -> None:
    body = b'h1,h2\n"a\nb",1\nc,2\nd,3\n'

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    dest = tmp_path / "slice.csv"
    download_url(
        "https://example.test/ledger.csv",
        dest,
        client=_client(handler),
        max_data_rows=2,
    )
    assert dest.read_bytes() == b'h1,h2\n"a\nb",1\nc,2\n'


def test_slice_preserves_quoted_newlines(tmp_path: Path) -> None:
    src = tmp_path / "full.csv"
    src.write_bytes(b'h\n"x\ny"\nz\nw\n')
    dest = tmp_path / "out.csv"
    written = slice_csv_rows(src, dest, 1)
    assert written == 1
    assert dest.read_bytes() == b'h\n"x\ny"\n'


def test_csv_records_span_chunks() -> None:
    chunks = [b'h,i\n"a', b'\nb",1\n', b"c,2\n"]
    assert list(_iter_csv_records(chunks)) == [b"h,i\n", b'"a\nb",1\n', b"c,2\n"]


def _count_csv_records(path: Path, encoding: str) -> tuple[int, int]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = sum(1 for _ in reader)
    return len(header), rows


def test_committed_samples_exist() -> None:
    la = DEFAULT_SAMPLES_DIR / "la_sample.csv"
    ok = DEFAULT_SAMPLES_DIR / "ok_sample.csv"
    attribution = DEFAULT_SAMPLES_DIR / "ATTRIBUTION.md"
    assert la.is_file()
    assert ok.is_file()
    assert attribution.is_file()
    text = attribution.read_text(encoding="utf-8")
    assert "Checkbook L.A., City of Los Angeles Controller, CC BY 4.0" in text
    assert (
        "State of Oklahoma Vendor Payments, Office of Management and "
        "Enterprise Services, CC BY"
    ) in text
    la_cols, la_rows = _count_csv_records(la, "utf-8")
    _ok_cols, ok_rows = _count_csv_records(ok, "cp1252")
    assert la_cols == 61
    assert la_rows == 20_000
    assert ok_rows == 5_000


def test_cli_cache_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch.DEFAULT_GENERATED_DIR", tmp_path)
    (tmp_path / "la.csv").write_bytes(LA_CSV)
    assert main(["la", "--limit", "100"]) == 0


def test_cli_writes_oklahoma(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch.DEFAULT_GENERATED_DIR", tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=OK_CSV)

    monkeypatch.setattr("data.fetch._build_client", lambda: _client(handler))
    assert main(["oklahoma"]) == 0
    assert (tmp_path / "oklahoma.csv").read_bytes() == OK_CSV


def test_cli_error_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch.DEFAULT_GENERATED_DIR", tmp_path)
    assert main(["la"]) == 1


def test_cli_force_redownload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("data.fetch.DEFAULT_GENERATED_DIR", tmp_path)
    (tmp_path / "la.csv").write_bytes(b"stale\n")
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=LA_CSV)

    monkeypatch.setattr("data.fetch._build_client", lambda: _client(handler))
    assert main(["la", "--limit", "100", "--force"]) == 0
    assert (tmp_path / "la.csv").read_bytes() == LA_CSV
    assert calls["n"] == 1
