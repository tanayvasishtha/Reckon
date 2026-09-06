from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.settings import Settings, load_settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.api_base is None
    assert settings.api_key is None
    assert settings.model_fast == "glm-4-7-flash"
    assert settings.model_escalate == "glm-4-7-flash"
    assert settings.price_fast_in == Decimal(0)
    assert settings.price_fast_out == Decimal(0)
    assert settings.price_esc_in == Decimal(0)
    assert settings.price_esc_out == Decimal(0)
    assert settings.concurrency_limit == 16
    assert settings.escalation_confidence_threshold == 0.7
    assert settings.blocking_bucket_cap == 200


def test_from_env_reads_known_variables_and_ignores_others() -> None:
    settings = Settings.from_env(
        {
            "PATH": "/usr/bin",
            "RECKON_API_BASE": "https://api.example.com/v1",
            "RECKON_API_KEY": "sk-test",
            "RECKON_MODEL_FAST": "fast-model",
            "RECKON_MODEL_ESCALATE": "escalate-model",
            "RECKON_PRICE_FAST_IN": "1.25",
            "RECKON_PRICE_FAST_OUT": "5.00",
            "RECKON_PRICE_ESC_IN": "2.00",
            "RECKON_PRICE_ESC_OUT": "10.00",
            "RECKON_CONCURRENCY_LIMIT": "8",
            "RECKON_ESCALATION_CONFIDENCE_THRESHOLD": "0.55",
            "RECKON_BLOCKING_BUCKET_CAP": "50",
        }
    )
    assert settings.api_base == "https://api.example.com/v1"
    assert settings.api_key == "sk-test"
    assert settings.model_fast == "fast-model"
    assert settings.model_escalate == "escalate-model"
    assert settings.price_fast_in == Decimal("1.25")
    assert settings.price_fast_out == Decimal("5.00")
    assert settings.price_esc_in == Decimal("2.00")
    assert settings.price_esc_out == Decimal("10.00")
    assert settings.concurrency_limit == 8
    assert settings.escalation_confidence_threshold == 0.55
    assert settings.blocking_bucket_cap == 50


def test_from_env_defaults_model_names_when_unset() -> None:
    settings = Settings.from_env(
        {
            "PATH": "/usr/bin",
            "RECKON_API_BASE": "https://api.example.com/v1",
        }
    )
    assert settings.model_fast == "glm-4-7-flash"
    assert settings.model_escalate == "glm-4-7-flash"


def test_from_env_model_names_override_defaults() -> None:
    settings = Settings.from_env(
        {
            "RECKON_MODEL_FAST": "fast-model",
            "RECKON_MODEL_ESCALATE": "escalate-model",
        }
    )
    assert settings.model_fast == "fast-model"
    assert settings.model_escalate == "escalate-model"


def test_empty_env_values_use_defaults() -> None:
    settings = Settings.from_env(
        {
            "RECKON_API_KEY": "",
            "RECKON_PRICE_FAST_IN": "",
            "RECKON_CONCURRENCY_LIMIT": "",
        }
    )
    assert settings.api_key is None
    assert settings.price_fast_in == Decimal(0)
    assert settings.concurrency_limit == 16


def test_load_settings_uses_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RECKON_MODEL_FAST", "env-fast")
    settings = load_settings()
    assert settings.model_fast == "env-fast"


def test_price_float_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="float is not allowed for monetary values"
    ):
        Settings(price_fast_in=1.5)


def test_settings_prices_serialise_to_json_strings() -> None:
    settings = Settings(
        price_fast_in=Decimal("1.25"),
        price_fast_out=Decimal("5.00"),
        price_esc_in=Decimal("2.00"),
        price_esc_out=Decimal("10.00"),
    )
    payload = json.loads(settings.model_dump_json())
    assert payload["price_fast_in"] == "1.25"
    assert payload["price_fast_out"] == "5.00"
    assert payload["price_esc_in"] == "2.00"
    assert payload["price_esc_out"] == "10.00"
    restored = Settings.model_validate_json(settings.model_dump_json())
    assert restored == settings
