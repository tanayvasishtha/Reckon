"""Record and replay chat-completions payloads as hashed cassette files.

Cassettes live in fixtures/cassettes/ and are named by a stable SHA-256 of
the request payload. Replay mode reads them; live mode writes them.

Complexity: O(P) to canonicalise and hash a payload of P bytes. Each call
reads or writes one file. Extra memory is O(P).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASSETTE_DIR = _REPO_ROOT / "fixtures" / "cassettes"


class CassetteError(Exception):
    """A cassette could not be read or written."""


def canonical_json(payload: object) -> str:
    """Return a stable JSON encoding: sorted keys, no extra whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def cassette_key(payload: Mapping[str, object]) -> str:
    """Return the hex digest used as the cassette filename stem."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


def cassette_path(directory: Path, key: str) -> Path:
    """Return directory/key.json using pathlib only."""
    return directory / f"{key}.json"


def save_cassette(
    directory: Path,
    request: Mapping[str, object],
    response: Mapping[str, object],
) -> Path:
    """Write request and response under the hash of request. Return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    key = cassette_key(request)
    path = cassette_path(directory, key)
    document = {
        "request": json.loads(canonical_json(request)),
        "response": json.loads(canonical_json(response)),
    }
    path.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_cassette(directory: Path, request: Mapping[str, object]) -> dict[str, object]:
    """Return the recorded response object for request.

    Raises CassetteError when the file is missing, unreadable, or does not
    match the hashed request. Set RECKON_API_KEY to record a cassette, or add
    the file under fixtures/cassettes/.
    """
    key = cassette_key(request)
    path = cassette_path(directory, key)
    if not path.is_file():
        raise CassetteError(
            f"no cassette for key {key} in {directory}. "
            "Set RECKON_API_KEY to record one, or add that file."
        )
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CassetteError(
            f"cassette {path} is not valid JSON. Recreate it with a live run."
        ) from exc
    except OSError as exc:
        raise CassetteError(
            f"could not read cassette {path}: {exc}. Check file permissions."
        ) from exc
    if not isinstance(loaded, dict):
        raise CassetteError(
            f"cassette {path} must be a JSON object with request and response."
        )
    stored_request = loaded.get("request")
    stored_response = loaded.get("response")
    if not isinstance(stored_request, dict) or not isinstance(stored_response, dict):
        raise CassetteError(
            f"cassette {path} must contain request and response objects."
        )
    if canonical_json(stored_request) != canonical_json(request):
        raise CassetteError(
            f"cassette {path} does not match the hashed request payload. "
            "Recreate it with a live run."
        )
    return stored_response
