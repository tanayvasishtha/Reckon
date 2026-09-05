from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal

from .models import Money, SchemaModel

_ENV_TO_FIELD: dict[str, str] = {
    "RECKON_API_BASE": "api_base",
    "RECKON_API_KEY": "api_key",
    "RECKON_MODEL_FAST": "model_fast",
    "RECKON_MODEL_ESCALATE": "model_escalate",
    "RECKON_PRICE_FAST_IN": "price_fast_in",
    "RECKON_PRICE_FAST_OUT": "price_fast_out",
    "RECKON_PRICE_ESC_IN": "price_esc_in",
    "RECKON_PRICE_ESC_OUT": "price_esc_out",
    "RECKON_CONCURRENCY_LIMIT": "concurrency_limit",
    "RECKON_ESCALATION_CONFIDENCE_THRESHOLD": "escalation_confidence_threshold",
    "RECKON_BLOCKING_BUCKET_CAP": "blocking_bucket_cap",
}


class Settings(SchemaModel):
    api_base: str | None = None
    api_key: str | None = None
    model_fast: str | None = None
    model_escalate: str | None = None
    price_fast_in: Money = Decimal(0)
    price_fast_out: Money = Decimal(0)
    price_esc_in: Money = Decimal(0)
    price_esc_out: Money = Decimal(0)
    concurrency_limit: int = 16
    escalation_confidence_threshold: float = 0.7
    blocking_bucket_cap: int = 200

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source: Mapping[str, str] = os.environ if environ is None else environ
        payload: dict[str, str] = {}
        for env_name, field_name in _ENV_TO_FIELD.items():
            raw = source.get(env_name)
            if raw is None or raw == "":
                continue
            payload[field_name] = raw
        return cls.model_validate(payload)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    return Settings.from_env(environ)
