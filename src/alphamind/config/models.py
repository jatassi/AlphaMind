"""Pydantic configuration models for AlphaMind YAML config files (story 03a)."""

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, field_validator, model_validator

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# .env.example lives at repo root: src/alphamind/config/ -> src/alphamind/ -> src/ -> root
_ENV_EXAMPLE_PATH = Path(__file__).parent.parent.parent.parent / ".env.example"

# 5-field cron: each field is digits/commas/dashes/slashes/stars, or day-of-week abbrevs
_CRON_FIELD_RE = re.compile(
    r"^[0-9,\-*/]+$"
    r"|^[a-zA-Z]{3}(-[a-zA-Z]{3})?(,[a-zA-Z]{3}(-[a-zA-Z]{3})?)*$"
)


def _load_env_example_keys() -> frozenset[str]:
    keys: set[str] = set()
    if not _ENV_EXAMPLE_PATH.exists():
        return frozenset()
    for line in _ENV_EXAMPLE_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, _ = line.partition("=")
            keys.add(key.strip())
    return frozenset(keys)


# Cached at import time; .env.example is a static deployment artifact.
_ENV_EXAMPLE_KEYS: frozenset[str] = _load_env_example_keys()


def _validate_cron(cron: str) -> str:
    fields = cron.split()
    if len(fields) != 5:
        msg = f"Cron expression must have exactly 5 fields, got {len(fields)}: {cron!r}"
        raise ValueError(msg)
    for field in fields:
        if not _CRON_FIELD_RE.match(field):
            msg = f"Invalid cron field {field!r} in expression {cron!r}"
            raise ValueError(msg)
    return cron


# ---------------------------------------------------------------------------
# news_outlets.yaml
# ---------------------------------------------------------------------------


class CredibilityTier(StrEnum):
    tier_1 = "tier_1"
    tier_2 = "tier_2"
    tier_3 = "tier_3"


class OutletEntry(BaseModel):
    tier: CredibilityTier


class NewsOutletsConfig(BaseModel):
    outlets: dict[str, OutletEntry]


# ---------------------------------------------------------------------------
# collector_schedule.yaml
# ---------------------------------------------------------------------------


class CollectorEntry(BaseModel):
    cron: str

    @field_validator("cron")
    @classmethod
    def cron_is_valid(cls, v: str) -> str:
        return _validate_cron(v)


class CollectorScheduleConfig(BaseModel):
    timezone: str
    collectors: dict[str, CollectorEntry]


# ---------------------------------------------------------------------------
# data_sources.yaml
# ---------------------------------------------------------------------------


class CriticalityTier(StrEnum):
    critical = "critical"
    important = "important"
    optional = "optional"


class BackoffStrategy(StrEnum):
    exponential = "exponential"
    none = "none"


class RetryShapeConfig(BaseModel):
    attempts: int
    backoff: BackoffStrategy
    failover: bool


class ProviderConfig(BaseModel):
    api_key_env: str | None = None
    rate_limit_per_minute: int | None = None
    rate_limit_per_day: int | None = None
    retry_shape: str

    @model_validator(mode="after")
    def exactly_one_rate_limit(self) -> "ProviderConfig":
        has_minute = self.rate_limit_per_minute is not None
        has_day = self.rate_limit_per_day is not None
        if not (has_minute ^ has_day):
            raise ValueError(
                "Provider must specify exactly one of rate_limit_per_minute or rate_limit_per_day"
            )
        return self


class CategoryConfig(BaseModel):
    tier: CriticalityTier
    freshness_max_seconds: int | None = None
    primary: str
    failover: list[str] = []


class DataSourcesConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    retry_shapes: dict[str, RetryShapeConfig]
    categories: dict[str, CategoryConfig]

    @model_validator(mode="after")
    def provider_retry_shapes_exist(self) -> "DataSourcesConfig":
        for name, provider in self.providers.items():
            if provider.retry_shape not in self.retry_shapes:
                raise ValueError(
                    f"Provider {name!r} references unknown retry_shape "
                    f"{provider.retry_shape!r}; defined shapes: "
                    f"{sorted(self.retry_shapes)}"
                )
        return self

    @model_validator(mode="after")
    def category_providers_exist(self) -> "DataSourcesConfig":
        for cat_name, cat in self.categories.items():
            if cat.primary not in self.providers:
                raise ValueError(
                    f"Category {cat_name!r} primary {cat.primary!r} is not in providers"
                )
            for fp in cat.failover:
                if fp not in self.providers:
                    raise ValueError(f"Category {cat_name!r} failover {fp!r} is not in providers")
        return self

    @model_validator(mode="after")
    def api_key_envs_in_env_example(self) -> "DataSourcesConfig":
        # Skip when .env.example is absent (e.g. CI without the file)
        if not _ENV_EXAMPLE_KEYS:
            return self
        for name, provider in self.providers.items():
            if provider.api_key_env is not None and provider.api_key_env not in _ENV_EXAMPLE_KEYS:
                raise ValueError(
                    f"Provider {name!r} api_key_env {provider.api_key_env!r} is not "
                    f"defined in .env.example"
                )
        return self
