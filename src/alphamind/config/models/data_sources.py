"""Pydantic models for data_sources.yaml."""

from enum import StrEnum

from pydantic import BaseModel, model_validator


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
        # Lazy import so monkeypatching alphamind.config.models._ENV_EXAMPLE_KEYS is observed.
        # (Top-level import would be circular: __init__.py imports this module.)
        from alphamind.config.models import _ENV_EXAMPLE_KEYS

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
