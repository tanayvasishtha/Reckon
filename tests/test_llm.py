from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest

from agents.cassettes import cassette_key, save_cassette
from agents.llm import (
    LIVE_MODE,
    REPLAY_MODE,
    ModelClient,
    ModelError,
    ReplayMissError,
    SchemaValidationError,
    build_request,
    run_header,
)
from core.models import SchemaModel
from core.settings import Settings

MESSAGES: tuple[dict[str, str], ...] = (
    {"role": "system", "content": "Return JSON."},
    {"role": "user", "content": "candidate-1"},
)


class Answer(SchemaModel):
    verdict: str
    confidence: float


def _live_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "api_key": "test-key",
        "api_base": "https://example.test/v1",
        "model_fast": "fast-model",
        "model_escalate": "esc-model",
        "concurrency_limit": 16,
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def _replay_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "model_fast": "fast-model",
        "model_escalate": "esc-model",
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def _answer_json(*, verdict: str = "dismiss", confidence: float = 0.91) -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence})


def _answer_body(
    *,
    verdict: str = "dismiss",
    confidence: float = 0.91,
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cached_tokens: int = 4,
    finish_reason: str = "stop",
) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "index": 0,
                "message": {
                    "content": _answer_json(verdict=verdict, confidence=confidence),
                    "role": "assistant",
                },
            }
        ],
        "usage": {
            "completion_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }


def _handler(
    body: Mapping[str, object],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=dict(body))

    return handler


def _fail_network(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("replay must not use the network")


def test_run_header_states_replay_by_default() -> None:
    settings = _replay_settings()
    header = run_header(settings)
    assert "mode=replay" in header
    assert "concurrency=16" in header
    assert "model_fast=fast-model" in header
    assert "model_escalate=esc-model" in header


def test_run_header_states_live_when_key_set() -> None:
    header = run_header(_live_settings(concurrency_limit=8))
    assert "mode=live" in header
    assert "concurrency=8" in header


def test_cassette_key_stable_across_runs() -> None:
    payload_a = build_request(model="fast-model", messages=MESSAGES)
    payload_b = {
        "response_format": {"type": "json_object"},
        "model": "fast-model",
        "messages": [
            {"content": "Return JSON.", "role": "system"},
            {"content": "candidate-1", "role": "user"},
        ],
    }
    key = cassette_key(payload_a)
    assert key == cassette_key(payload_b)
    assert key == cassette_key(payload_a)
    canonical = json.dumps(
        payload_a, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    assert key == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    roundtrip: object = json.loads(json.dumps(payload_a))
    assert isinstance(roundtrip, dict)
    assert key == cassette_key(roundtrip)


async def test_replay_returns_recorded_response_with_no_network(
    tmp_path: Path,
) -> None:
    def live_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_answer_body(verdict="escalate", confidence=0.41)
        )

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(live_handler),
    ) as live:
        assert live.mode == LIVE_MODE
        recorded = await live.complete(MESSAGES, Answer)

    async with ModelClient(
        _replay_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(_fail_network),
    ) as replay:
        assert replay.mode == REPLAY_MODE
        assert "mode=replay" in replay.run_header()
        result = await replay.complete(MESSAGES, Answer)

    assert result.value == recorded.value
    assert result.value.verdict == "escalate"
    assert result.value.confidence == 0.41
    assert result.model_used == "fast-model"
    assert result.tokens_in == recorded.tokens_in == 12
    assert result.tokens_out == recorded.tokens_out == 7
    assert result.cache_read == recorded.cache_read == 4


async def test_replay_reads_committed_cassette_without_transport(
    tmp_path: Path,
) -> None:
    payload = build_request(model="fast-model", messages=MESSAGES)
    save_cassette(tmp_path, payload, _answer_body(verdict="dismiss", confidence=0.8))
    async with ModelClient(_replay_settings(), cassette_dir=tmp_path) as client:
        result = await client.complete(MESSAGES, Answer)
    assert result.value.verdict == "dismiss"
    assert result.cache_read == 4


async def test_schema_validation_rejects_malformed_response(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = _answer_body()
        choices = body["choices"]
        assert isinstance(choices, list)
        first = choices[0]
        assert isinstance(first, dict)
        message = first["message"]
        assert isinstance(message, dict)
        message["content"] = json.dumps({"verdict": "dismiss"})
        return httpx.Response(200, json=body)

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            SchemaValidationError, match="errored; do not guess"
        ) as raised:
            await client.complete(MESSAGES, Answer)
    assert not isinstance(raised.value, ReplayMissError)


async def test_retry_and_backoff_on_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("agents.llm._sleep", fake_sleep)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, content=b"slow")
        return httpx.Response(200, json=_answer_body())

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.complete(MESSAGES, Answer)

    assert result.value.verdict == "dismiss"
    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


async def test_retry_honours_retry_after_on_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("agents.llm._sleep", fake_sleep)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"}, content=b"slow")
        return httpx.Response(200, json=_answer_body())

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.complete(MESSAGES, Answer)

    assert calls["n"] == 2
    assert sleeps == [0.5]


async def test_retry_on_5xx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agents.llm._sleep", _noop_sleep)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"down")
        return httpx.Response(200, json=_answer_body())

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.complete(MESSAGES, Answer)

    assert result.value.verdict == "dismiss"
    assert calls["n"] == 3


async def test_concurrency_never_exceeds_semaphore(tmp_path: Path) -> None:
    limit = 3
    current = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(200, json=_answer_body())
        finally:
            current -= 1

    async with ModelClient(
        _live_settings(concurrency_limit=limit),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        results = await asyncio.gather(
            *[
                client.complete(
                    (
                        {"role": "system", "content": "Return JSON."},
                        {"role": "user", "content": f"candidate-{index}"},
                    ),
                    Answer,
                )
                for index in range(9)
            ]
        )

    assert peak <= limit
    assert peak == limit
    assert len(results) == 9
    assert all(item.value.verdict == "dismiss" for item in results)


async def test_backoff_does_not_collapse_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_backoff = asyncio.Event()
    fast_started = asyncio.Event()
    order: list[str] = []

    async def fake_sleep(_seconds: float) -> None:
        order.append("backoff")
        in_backoff.set()
        await asyncio.wait_for(fast_started.wait(), timeout=2)

    monkeypatch.setattr("agents.llm._sleep", fake_sleep)
    slow_attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload: object = json.loads(request.content)
        assert isinstance(payload, dict)
        messages = payload["messages"]
        assert isinstance(messages, list)
        last = messages[-1]
        assert isinstance(last, dict)
        content = last["content"]
        if content == "slow":
            slow_attempts["n"] += 1
            if slow_attempts["n"] == 1:
                return httpx.Response(429, content=b"slow")
            return httpx.Response(200, json=_answer_body(verdict="escalate"))
        order.append("fast")
        fast_started.set()
        return httpx.Response(200, json=_answer_body(verdict="dismiss"))

    slow_messages = (
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "slow"},
    )
    fast_messages = (
        {"role": "system", "content": "Return JSON."},
        {"role": "user", "content": "fast"},
    )
    async with ModelClient(
        _live_settings(concurrency_limit=1),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        slow_task = asyncio.create_task(client.complete(slow_messages, Answer))
        await asyncio.wait_for(in_backoff.wait(), timeout=2)
        fast_result = await client.complete(fast_messages, Answer)
        slow_result = await slow_task

    assert fast_result.value.verdict == "dismiss"
    assert slow_result.value.verdict == "escalate"
    assert order == ["backoff", "fast"]


async def test_missing_cassette_fails_clearly(tmp_path: Path) -> None:
    async with ModelClient(_replay_settings(), cassette_dir=tmp_path) as client:
        with pytest.raises(ReplayMissError, match="no cassette") as raised:
            await client.complete(MESSAGES, Answer)
    assert isinstance(raised.value, ModelError)
    assert type(raised.value) is ReplayMissError


def test_replay_miss_error_is_model_error() -> None:
    error = ReplayMissError("no cassette")
    assert isinstance(error, ModelError)
    assert not isinstance(SchemaValidationError("bad schema"), ReplayMissError)


async def test_replay_schema_validation_is_not_replay_miss(tmp_path: Path) -> None:
    payload = build_request(model="fast-model", messages=MESSAGES)
    body = _answer_body()
    choices = body["choices"]
    assert isinstance(choices, list)
    first = choices[0]
    assert isinstance(first, dict)
    message = first["message"]
    assert isinstance(message, dict)
    message["content"] = json.dumps({"verdict": "dismiss"})
    save_cassette(tmp_path, payload, body)
    async with ModelClient(
        _replay_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(_fail_network),
    ) as client:
        with pytest.raises(
            SchemaValidationError, match="errored; do not guess"
        ) as raised:
            await client.complete(MESSAGES, Answer)
    assert not isinstance(raised.value, ReplayMissError)


async def test_replay_payload_mismatch_raises_replay_miss(tmp_path: Path) -> None:
    payload = build_request(model="fast-model", messages=MESSAGES)
    path = save_cassette(tmp_path, payload, _answer_body())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["request"]["messages"][0]["content"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")
    async with ModelClient(
        _replay_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(_fail_network),
    ) as client:
        with pytest.raises(ReplayMissError, match="does not match") as raised:
            await client.complete(MESSAGES, Answer)
    assert isinstance(raised.value, ModelError)


async def test_replay_unreadable_cassette_raises_replay_miss(tmp_path: Path) -> None:
    payload = build_request(model="fast-model", messages=MESSAGES)
    path = save_cassette(tmp_path, payload, _answer_body())
    path.write_text("{not-json", encoding="utf-8")
    async with ModelClient(
        _replay_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(_fail_network),
    ) as client:
        with pytest.raises(ReplayMissError, match="not valid JSON") as raised:
            await client.complete(MESSAGES, Answer)
    assert isinstance(raised.value, ModelError)


async def test_live_without_base_url_fails_clearly() -> None:
    with pytest.raises(ModelError, match="RECKON_API_BASE"):
        ModelClient(Settings(api_key="test-key", model_fast="fast-model"))


async def test_http_400_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agents.llm._sleep", _noop_sleep)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, content=b"bad request")

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ModelError, match="HTTP 400") as raised:
            await client.complete(MESSAGES, Answer)
    assert calls["n"] == 1
    assert not isinstance(raised.value, ReplayMissError)


async def test_live_transport_error_is_not_replay_miss(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ModelError, match="network error") as raised:
            await client.complete(MESSAGES, Answer)
    assert not isinstance(raised.value, ReplayMissError)
    assert type(raised.value) is ModelError


async def test_cache_read_from_alternate_usage_field(tmp_path: Path) -> None:
    body = _answer_body(cached_tokens=0)
    usage = body["usage"]
    assert isinstance(usage, dict)
    usage["prompt_tokens_details"] = {}
    usage["cache_read_input_tokens"] = 9

    async with ModelClient(
        _live_settings(),
        cassette_dir=tmp_path,
        transport=httpx.MockTransport(_handler(body)),
    ) as client:
        result = await client.complete(MESSAGES, Answer)
    assert result.cache_read == 9


async def _noop_sleep(_seconds: float) -> None:
    return None
