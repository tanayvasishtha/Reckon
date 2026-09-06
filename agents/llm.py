"""Provider-neutral chat-completions client with live recording and replay.

Stage 5 talks to any chat-completions endpoint configured through Settings
(RECKON_API_BASE, RECKON_API_KEY, RECKON_MODEL_FAST, RECKON_MODEL_ESCALATE).
With RECKON_API_KEY set, calls go live and every request/response is recorded
as a cassette. With no key, responses are read from fixtures/cassettes/ and
no network is used. That replay path is the default for a clone with no key.

Complexity: O(1) work per call besides waiting on the endpoint. At most
Settings.concurrency_limit calls are in flight (default 16). Retries sleep
outside the semaphore so a 429 or 5xx does not stall the rest of the pool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Generic, Self, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from agents.cassettes import (
    DEFAULT_CASSETTE_DIR,
    CassetteError,
    cassette_key,
    load_cassette,
    save_cassette,
)
from core.settings import Settings

log = logging.getLogger("agents.llm")

LIVE_MODE = "live"
REPLAY_MODE = "replay"

MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SECONDS = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 120.0
USER_AGENT = "reckon-model-client/0.1"
CHAT_COMPLETIONS_PATH = "chat/completions"

T = TypeVar("T", bound=BaseModel)


class ModelError(Exception):
    """A chat-completions call failed."""


class SchemaValidationError(ModelError):
    """The model response did not match the caller-supplied schema."""


class TransientModelError(ModelError):
    """A retryable 429 or 5xx from the endpoint."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class ModelResult(Generic[T]):
    """Validated structured output plus per-call token accounting."""

    value: T
    model_used: str
    tokens_in: int
    tokens_out: int
    cache_read: int


def mode_for(settings: Settings) -> str:
    """Return live when an API key is set, otherwise replay."""
    if settings.api_key:
        return LIVE_MODE
    return REPLAY_MODE


def run_header(settings: Settings) -> str:
    """Return the run-header fragment stating mode, concurrency, and models."""
    return (
        f"mode={mode_for(settings)} "
        f"concurrency={settings.concurrency_limit} "
        f"model_fast={settings.model_fast or '-'} "
        f"model_escalate={settings.model_escalate or '-'}"
    )


def build_request(
    *, model: str, messages: Sequence[Mapping[str, str]]
) -> dict[str, object]:
    """Return the canonical chat-completions JSON body for hashing and POST."""
    normalised: list[dict[str, str]] = []
    for message in messages:
        try:
            role = message["role"]
            content = message["content"]
        except KeyError as exc:
            raise ModelError(
                "each message needs 'role' and 'content'. "
                "Fix the prompt payload and retry."
            ) from exc
        normalised.append({"role": str(role), "content": str(content)})
    return {
        "messages": normalised,
        "model": model,
        "response_format": {"type": "json_object"},
    }


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class ModelClient:
    """Async chat-completions client with a concurrency semaphore and cassettes."""

    def __init__(
        self,
        settings: Settings,
        *,
        cassette_dir: Path | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        run_id: str | None = None,
    ) -> None:
        if settings.concurrency_limit < 1:
            raise ModelError("RECKON_CONCURRENCY_LIMIT must be a positive integer.")
        self._settings = settings
        self._mode = mode_for(settings)
        self._cassette_dir = (
            DEFAULT_CASSETTE_DIR if cassette_dir is None else cassette_dir
        )
        self._transport = transport
        self._http = http_client
        self._owns_http = http_client is None
        self._run_id = uuid.uuid4().hex[:8] if run_id is None else run_id
        self._semaphore = asyncio.Semaphore(settings.concurrency_limit)
        if self._mode == LIVE_MODE and not settings.api_base and http_client is None:
            raise ModelError(
                "RECKON_API_BASE is required for live mode. "
                "Set it to the chat completions base URL."
            )
        log.info("run_id=%s %s", self._run_id, run_header(settings))

    @property
    def mode(self) -> str:
        """Return 'live' or 'replay'."""
        return self._mode

    def run_header(self) -> str:
        """Return the run-header fragment for this client's settings."""
        return run_header(self._settings)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client when this instance created it."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        schema: type[T],
        *,
        model: str | None = None,
    ) -> ModelResult[T]:
        """Call the endpoint or replay a cassette, then validate against schema.

        A response that fails schema validation raises SchemaValidationError.
        Missing fields are not filled in and invalid JSON is never coerced.
        """
        resolved_model = _resolve_model(self._settings, model)
        payload = build_request(model=resolved_model, messages=messages)
        started = time.perf_counter()
        if self._mode == REPLAY_MODE:
            body = self._replay(payload)
            result = _parse_result(body, schema, resolved_model)
            _log_call(self._run_id, self._mode, payload, result, started)
            return result
        body = await self._live(payload)
        result = _parse_result(body, schema, resolved_model)
        _log_call(self._run_id, self._mode, payload, result, started)
        return result

    def _replay(self, payload: Mapping[str, object]) -> dict[str, object]:
        try:
            return load_cassette(self._cassette_dir, payload)
        except CassetteError as exc:
            raise ModelError(str(exc)) from exc

    async def _live(self, payload: Mapping[str, object]) -> dict[str, object]:
        last_error: TransientModelError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                body = await self._post_once(payload)
                save_cassette(self._cassette_dir, payload, body)
                return body
            except TransientModelError as exc:
                last_error = exc
                if attempt >= MAX_ATTEMPTS:
                    break
                delay = _backoff_delay(attempt, exc.retry_after)
                log.warning(
                    "run_id=%s retry attempt=%s/%s sleep_s=%s error=%s",
                    self._run_id,
                    attempt,
                    MAX_ATTEMPTS,
                    delay,
                    exc,
                )
                await _sleep(delay)
        raise ModelError(
            f"chat completions failed after {MAX_ATTEMPTS} attempts: "
            f"{last_error}. Mark the candidate errored and retry when the "
            "endpoint recovers."
        ) from last_error

    async def _post_once(self, payload: Mapping[str, object]) -> dict[str, object]:
        client = self._ensure_http()
        url = _completions_url(self._settings)
        async with self._semaphore:
            try:
                response = await client.post(url, json=dict(payload))
            except httpx.TransportError as exc:
                raise ModelError(
                    f"network error talking to chat completions endpoint: {exc}. "
                    "Check RECKON_API_BASE."
                ) from exc
        return _body_from_response(response, url)

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        timeout = httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=READ_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        )
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            transport=cast(httpx.AsyncBaseTransport | None, self._transport),
        )
        self._owns_http = True
        return self._http


def _resolve_model(settings: Settings, model: str | None) -> str:
    resolved = model if model is not None else settings.model_fast
    if resolved is None or resolved == "":
        raise ModelError("no model name. Pass model= or set RECKON_MODEL_FAST.")
    return resolved


def _completions_url(settings: Settings) -> str:
    base = settings.api_base
    if base is None or base == "":
        raise ModelError(
            "RECKON_API_BASE is required for live mode. "
            "Set it to the chat completions base URL."
        )
    return f"{base.rstrip('/')}/{CHAT_COMPLETIONS_PATH}"


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, MAX_BACKOFF_SECONDS)
    delay = INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1))
    return min(delay, MAX_BACKOFF_SECONDS)


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


def _body_from_response(response: httpx.Response, url: str) -> dict[str, object]:
    status = response.status_code
    if status == 429 or status >= 500:
        raise TransientModelError(
            f"HTTP {status} for {url}",
            retry_after=_retry_after_seconds(response),
        )
    if status != 200:
        preview = response.text[:200]
        raise ModelError(
            f"HTTP {status} from chat completions endpoint: {preview}. "
            "Check RECKON_API_BASE, RECKON_API_KEY, and the model name."
        )
    try:
        loaded: object = response.json()
    except json.JSONDecodeError as exc:
        raise ModelError(
            "chat completions endpoint returned non-JSON. "
            "Check RECKON_API_BASE points at a chat completions API."
        ) from exc
    if not isinstance(loaded, dict):
        raise ModelError("chat completions endpoint returned a non-object JSON body.")
    return loaded


def _parse_result(
    body: Mapping[str, object], schema: type[T], model_used: str
) -> ModelResult[T]:
    content = _message_content(body)
    value = _validate_schema(content, schema)
    tokens_in, tokens_out, cache_read = _token_counts(body)
    return ModelResult(
        value=value,
        model_used=model_used,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read=cache_read,
    )


def _message_content(body: Mapping[str, object]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError("chat completions response has no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise ModelError("chat completions choice is not an object.")
    if first.get("finish_reason") == "length":
        raise ModelError(
            "response truncated before a complete JSON object. "
            "Do not lower max output length; mark the candidate errored."
        )
    message = first.get("message")
    if not isinstance(message, dict):
        raise ModelError("chat completions choice has no message.")
    content = message.get("content")
    if not isinstance(content, str) or content.strip() == "":
        raise ModelError("chat completions message content is empty.")
    return content


def _validate_schema(content: str, schema: type[T]) -> T:
    try:
        data: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(
            f"model response is not valid JSON: {exc}. "
            "Mark the candidate errored; do not guess."
        ) from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise SchemaValidationError(
            f"model response failed schema validation: {exc}. "
            "Mark the candidate errored; do not guess."
        ) from exc


def _token_counts(body: Mapping[str, object]) -> tuple[int, int, int]:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0
    tokens_in = _as_int(usage.get("prompt_tokens"))
    tokens_out = _as_int(usage.get("completion_tokens"))
    return tokens_in, tokens_out, _cache_read(usage)


def _cache_read(usage: Mapping[str, object]) -> int:
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _as_int(details.get("cached_tokens"))
        if cached:
            return cached
    for key in (
        "cache_read_input_tokens",
        "cached_tokens",
        "prompt_cache_hit_tokens",
    ):
        cached = _as_int(usage.get(key))
        if cached:
            return cached
    return 0


def _as_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _log_call(
    run_id: str,
    mode: str,
    payload: Mapping[str, object],
    result: ModelResult[T],
    started: float,
) -> None:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "run_id=%s mode=%s model=%s cassette=%s tokens_in=%s tokens_out=%s "
        "cache_read=%s elapsed_ms=%s",
        run_id,
        mode,
        result.model_used,
        cassette_key(payload)[:12],
        result.tokens_in,
        result.tokens_out,
        result.cache_read,
        elapsed_ms,
    )
