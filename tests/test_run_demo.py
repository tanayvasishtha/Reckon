from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from adapters.base import AdapterError
from api.schemas import FixtureFile
from core.models import (
    Candidate,
    ExplanationType,
    Signal,
    Verdict,
    VerdictOutcome,
)
from scripts import run_demo
from scripts.run_demo import (
    FAST_ROW_LIMIT,
    DemoError,
    dollars_at_risk,
    format_money,
    main,
    one_line,
    resolve_source,
    survivors_after_adjudication,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LA_CSV = FIXTURES / "la.csv"
OK_CSV = FIXTURES / "oklahoma.csv"
GENERIC_CSV = FIXTURES / "generic.csv"
EXAMPLE_MAP = ROOT / "adapters" / "maps" / "example.yaml"
PARTIAL_MAP = FIXTURES / "generic_partial.yaml"


@pytest.fixture(autouse=True)
def _replay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECKON_API_KEY", raising=False)
    monkeypatch.delenv("RECKON_API_BASE", raising=False)


@pytest.fixture(autouse=True)
def generated_queue_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "data" / "generated" / "queue.json"
    monkeypatch.setattr(run_demo, "GENERATED_QUEUE_PATH", path)
    return path


@pytest.fixture
def tiny_la(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_demo, "LA_SAMPLE", LA_CSV)
    return LA_CSV


@pytest.fixture
def tiny_ok(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(run_demo, "OK_SAMPLE", OK_CSV)
    return OK_CSV


def _run(
    *,
    source: str = "",
    map_path: str = "",
    fast: bool = False,
) -> str:
    buf = StringIO()
    run_demo.run_demo(source=source, map_path=map_path, fast=fast, output=buf)
    return buf.getvalue()


def _labels(text: str) -> list[str]:
    return [line.split(":", 1)[0] for line in text.splitlines() if ":" in line]


def _value(text: str, label: str) -> str:
    prefix = f"{label}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"missing {label!r} in:\n{text}")


def test_demo_writes_generated_queue(tiny_la: Path, generated_queue_path: Path) -> None:
    text = _run()
    assert generated_queue_path.is_file()
    raw_text = generated_queue_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert raw_text == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fixture = FixtureFile.model_validate(payload)
    assert fixture.funnel.candidates == int(_value(text, "candidates after blocking"))
    assert fixture.funnel.rows_in == int(_value(text, "rows in"))
    assert fixture.funnel.transactions == int(
        _value(text, "transactions after aggregation")
    )
    assert fixture.funnel.after_dismiss == int(
        _value(text, "survivors after deterministic dismissal")
    )
    assert fixture.funnel.after_adjudicate == int(
        _value(text, "survivors after adjudication")
    )
    ids = [item.candidate.candidate_id for item in fixture.findings]
    assert ids == sorted(ids)
    for finding in fixture.findings:
        assert finding.verdict.verdict in {
            VerdictOutcome.escalate,
            VerdictOutcome.errored,
        }
        left_id, right_id = finding.candidate.transaction_ids
        assert finding.left.transaction_id == left_id
        assert finding.right.transaction_id == right_id
        assert isinstance(payload["funnel"]["candidates"], int)
    money_blob = json.dumps(payload)
    assert "e+" not in money_blob.lower()


def test_demo_queue_findings_match_survivors(
    tmp_path: Path, generated_queue_path: Path
) -> None:
    csv_path = tmp_path / "dups.csv"
    csv_path.write_text(
        "txn_id,vendor_name,amount,invoice_number\n"
        "a,Acme LLC,1250.50,INV-1\n"
        "b,Acme LLC,1250.50,INV-1\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "map.yaml"
    map_path.write_text(
        "encoding: utf-8\n"
        "columns:\n"
        "  transaction_id: txn_id\n"
        "  vendor_name_raw: vendor_name\n"
        "  amount: amount\n"
        "  invoice_number_raw: invoice_number\n",
        encoding="utf-8",
    )
    text = _run(source=str(csv_path), map_path=str(map_path))
    fixture = FixtureFile.model_validate_json(
        generated_queue_path.read_text(encoding="utf-8")
    )
    blocking = int(_value(text, "candidates after blocking"))
    residual = int(_value(text, "survivors after deterministic dismissal"))
    surviving = int(_value(text, "survivors after adjudication"))
    assert fixture.funnel.candidates == blocking
    assert fixture.funnel.after_dismiss == residual
    assert fixture.funnel.after_adjudicate == surviving
    assert len(fixture.findings) == surviving
    assert surviving >= 1
    raw = json.loads(generated_queue_path.read_text(encoding="utf-8"))
    for finding in raw["findings"]:
        assert isinstance(finding["candidate"]["amount_at_risk"], str)
        assert isinstance(finding["left"]["amount"], str)
        assert isinstance(finding["right"]["amount"], str)
        Decimal(finding["candidate"]["amount_at_risk"])


def test_write_generated_queue_failure_is_demo_error(
    tmp_path: Path, tiny_la: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("file", encoding="utf-8")
    monkeypatch.setattr(run_demo, "GENERATED_QUEUE_PATH", blocked / "queue.json")
    with pytest.raises(DemoError, match="could not write"):
        run_demo.run_demo(output=StringIO())


def test_header_prints_before_funnel(tiny_la: Path) -> None:
    text = _run()
    labels = _labels(text)
    assert labels[:5] == ["source", "rows", "concurrency", "mode", "commit"]
    assert labels[5:] == [
        "rows in",
        "transactions after aggregation",
        "candidates after blocking",
        "survivors after deterministic dismissal",
        "survivors after adjudication",
        "dollars at risk",
    ]
    assert _value(text, "source") == "tests/fixtures/la.csv"
    assert _value(text, "rows") == "2"
    assert _value(text, "concurrency") == "16"
    assert _value(text, "mode") == "replay"
    assert len(_value(text, "commit")) == 40
    assert _value(text, "rows in") == "2"
    assert _value(text, "transactions after aggregation") == "2"


def test_funnel_prints_as_stages_complete(
    tiny_la: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = StringIO()
    seen_before_block = ""
    real_block = run_demo.block

    def wrapped(*args: object, **kwargs: object) -> object:
        nonlocal seen_before_block
        seen_before_block = buf.getvalue()
        return real_block(*args, **kwargs)

    monkeypatch.setattr(run_demo, "block", wrapped)
    run_demo.run_demo(output=buf)
    assert "source:" in seen_before_block
    assert "rows in:" in seen_before_block
    assert "transactions after aggregation:" in seen_before_block
    assert "candidates after blocking:" not in seen_before_block
    assert "candidates after blocking:" in buf.getvalue()


def test_replay_is_default_without_api_key(tiny_la: Path) -> None:
    text = _run()
    assert _value(text, "mode") == "replay"


def test_live_mode_when_key_set(tiny_la: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_API_KEY", "sk-test")
    monkeypatch.setenv("RECKON_API_BASE", "https://example.test/v1")
    text = _run()
    assert _value(text, "mode") == "live"


def test_oklahoma_named_source(tiny_ok: Path) -> None:
    text = _run(source="oklahoma")
    assert _value(text, "source") == "tests/fixtures/oklahoma.csv"
    assert _value(text, "mode") == "replay"
    assert _value(text, "rows") == "4"
    assert _value(text, "rows in") == "2"


def test_generic_source_and_map() -> None:
    text = _run(source=str(GENERIC_CSV), map_path=str(EXAMPLE_MAP))
    assert "tests/fixtures/generic.csv" in _value(text, "source")
    assert _value(text, "rows") == "2"
    assert _value(text, "rows in") == "2"
    assert _value(text, "mode") == "replay"


def test_fast_limits_rows(tiny_la: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_demo, "FAST_ROW_LIMIT", 1)
    text = _run(fast=True)
    assert _value(text, "rows") == "1"
    assert _value(text, "rows in") == "1"
    assert _value(text, "transactions after aggregation") == "1"


def test_custom_csv_requires_map(tmp_path: Path) -> None:
    csv_path = tmp_path / "ledger.csv"
    csv_path.write_text("txn_id,vendor_name,amount\n1,Acme,10\n", encoding="utf-8")
    with pytest.raises(DemoError, match="MAP is required"):
        run_demo.run_demo(source=str(csv_path), map_path="", output=StringIO())


def test_missing_file_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.csv"
    code = main(["--source", str(missing), "--map", str(EXAMPLE_MAP)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert "ledger not found" in captured.err
    assert "existing CSV" in captured.err


def test_bad_column_map_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = tmp_path / "ledger.csv"
    csv_path.write_text("txn_id,vendor_name,amount\n1,Acme,10\n", encoding="utf-8")
    map_path = tmp_path / "bad.yaml"
    map_path.write_text(
        "encoding: utf-8\ncolumns:\n  transaction_id: txn_id\n  vendor_name_raw: vendor_name\n",
        encoding="utf-8",
    )
    code = main(["--source", str(csv_path), "--map", str(map_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert "missing required fields" in captured.err
    assert "example.yaml" in captured.err


def test_unreadable_encoding_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = tmp_path / "latin1.csv"
    csv_path.write_bytes(b"txn_id,vendor_name,amount\n1,caf\xff,10\n")
    map_path = tmp_path / "map.yaml"
    map_path.write_text(
        "encoding: utf-8\n"
        "columns:\n"
        "  transaction_id: txn_id\n"
        "  vendor_name_raw: vendor_name\n"
        "  amount: amount\n",
        encoding="utf-8",
    )
    code = main(["--source", str(csv_path), "--map", str(map_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert "could not decode" in captured.err
    assert "encoding" in captured.err


def test_unknown_source_is_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--source", "not-a-city"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.count("\n") == 1
    assert "Traceback" not in captured.err
    assert "unknown SOURCE" in captured.err


def test_missing_file_subprocess_has_no_traceback() -> None:
    env = os.environ.copy()
    env.pop("RECKON_API_KEY", None)
    env.pop("RECKON_API_BASE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.run_demo",
            "--source",
            "missing.csv",
            "--map",
            str(EXAMPLE_MAP),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert result.stderr.count("\n") == 1
    assert "ledger not found" in result.stderr


def test_main_success_exit_code(
    tiny_la: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "mode: replay" in captured.out
    assert "dollars at risk:" in captured.out


def test_duplicate_rows_report_decimal_dollars(tmp_path: Path) -> None:
    csv_path = tmp_path / "dups.csv"
    csv_path.write_text(
        "txn_id,vendor_name,amount,invoice_number\n"
        "a,Acme LLC,1250.50,INV-1\n"
        "b,Acme LLC,1250.50,INV-1\n",
        encoding="utf-8",
    )
    map_path = tmp_path / "map.yaml"
    map_path.write_text(
        "encoding: utf-8\n"
        "columns:\n"
        "  transaction_id: txn_id\n"
        "  vendor_name_raw: vendor_name\n"
        "  amount: amount\n"
        "  invoice_number_raw: invoice_number\n",
        encoding="utf-8",
    )
    text = _run(source=str(csv_path), map_path=str(map_path))
    assert int(_value(text, "candidates after blocking")) >= 1
    assert int(_value(text, "survivors after deterministic dismissal")) >= 1
    assert int(_value(text, "survivors after adjudication")) >= 1
    risk = _value(text, "dollars at risk")
    parsed = Decimal(risk)
    assert parsed > 0
    assert format_money(parsed) == risk
    assert "e" not in risk.lower()


def test_dollars_at_risk_never_uses_float() -> None:
    left = Candidate(
        candidate_id="exact_duplicate:a:b",
        transaction_ids=("a", "b"),
        signal=Signal.exact_duplicate,
        amount_at_risk=Decimal("10.10"),
        stage_generated="blocking",
    )
    right = Candidate(
        candidate_id="exact_duplicate:c:d",
        transaction_ids=("c", "d"),
        signal=Signal.exact_duplicate,
        amount_at_risk=Decimal("0.05"),
        stage_generated="blocking",
    )
    total = dollars_at_risk([left, right])
    assert total == Decimal("10.15")
    assert type(total) is Decimal
    assert format_money(total) == "10.15"


def test_survivors_drop_model_dismissals() -> None:
    residual = [
        Candidate(
            candidate_id="keep",
            transaction_ids=("a", "b"),
            signal=Signal.exact_duplicate,
            amount_at_risk=Decimal("1.00"),
            stage_generated="blocking",
        ),
        Candidate(
            candidate_id="drop",
            transaction_ids=("c", "d"),
            signal=Signal.fuzzy_vendor,
            amount_at_risk=Decimal("2.00"),
            stage_generated="blocking",
        ),
        Candidate(
            candidate_id="error",
            transaction_ids=("e", "f"),
            signal=Signal.split_payment,
            amount_at_risk=Decimal("3.00"),
            stage_generated="blocking",
        ),
    ]
    verdicts = [
        Verdict(
            candidate_id="keep",
            verdict=VerdictOutcome.escalate,
            explanation_type=ExplanationType.none,
            reasoning="looks recoverable",
            evidence=[],
            confidence=0.9,
            model_used="fast",
            tokens_in=0,
            tokens_out=0,
            cache_read=0,
        ),
        Verdict(
            candidate_id="drop",
            verdict=VerdictOutcome.dismiss,
            explanation_type=ExplanationType.recurring,
            reasoning="monthly",
            evidence=[],
            confidence=0.9,
            model_used="fast",
            tokens_in=0,
            tokens_out=0,
            cache_read=0,
        ),
        Verdict(
            candidate_id="error",
            verdict=VerdictOutcome.errored,
            explanation_type=ExplanationType.none,
            reasoning="no cassette",
            evidence=[],
            confidence=0.0,
            model_used="fast",
            tokens_in=0,
            tokens_out=0,
            cache_read=0,
        ),
    ]
    kept = survivors_after_adjudication(residual, verdicts)
    assert [item.candidate_id for item in kept] == ["keep", "error"]
    assert dollars_at_risk(kept) == Decimal("4.00")


def test_resolve_named_sources(tiny_la: Path, tiny_ok: Path) -> None:
    la = resolve_source("", "")
    assert la.path == LA_CSV
    assert la.encoding == "utf-8"
    ok = resolve_source("oklahoma", "")
    assert ok.path == OK_CSV
    assert ok.encoding == "cp1252"


def test_resolve_generic_partial_map() -> None:
    resolved = resolve_source(str(GENERIC_CSV), str(PARTIAL_MAP))
    assert resolved.adapter.capabilities.has_invoice_number is False


def test_one_line_flattens_multiline() -> None:
    assert one_line(AdapterError("a\nb\\c")) == "a b/c"


def test_makefile_demo_targets() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "demo-fast:" in text
    assert "scripts.run_demo" in text
    assert "--fast" in text
    assert "--source" in text
    assert "--map" in text
    assert "uv run python -u -m scripts.run_demo" in text
    assert FAST_ROW_LIMIT == 20_000
