"""Public re-exports for alphamind.config.models package (story 02)."""

from pathlib import Path

from alphamind.config.models.assets import (
    AssetRole,
    AssetsConfig,
    Benchmark,
    DiscoverySource,
    DiscoveryVendor,
)
from alphamind.config.models.collector_schedule import CollectorEntry, CollectorScheduleConfig
from alphamind.config.models.data_sources import (
    BackoffStrategy,
    CategoryConfig,
    CriticalityTier,
    DataSourcesConfig,
    ProviderConfig,
    RetryShapeConfig,
)
from alphamind.config.models.guardrails import (
    BreachResponse,
    EmergencyInvocation,
    EnforcementTier,
    EscalationZones,
    GuardrailsConfig,
    ProgressiveTier,
    RuleEntry,
)
from alphamind.config.models.news_outlets import CredibilityTier, NewsOutletsConfig, OutletEntry
from alphamind.config.models.scheduler import SchedulerConfig

# ---------------------------------------------------------------------------
# .env.example helpers — kept here so monkeypatching
# `alphamind.config.models._ENV_EXAMPLE_KEYS` is observed by DataSourcesConfig's
# validator (which does a lazy in-function import of this name).
# ---------------------------------------------------------------------------

_ENV_EXAMPLE_PATH = Path(__file__).parent.parent.parent.parent.parent / ".env.example"


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


_ENV_EXAMPLE_KEYS: frozenset[str] = _load_env_example_keys()

__all__ = [
    "AssetRole",
    "AssetsConfig",
    "BackoffStrategy",
    "Benchmark",
    "BreachResponse",
    "CategoryConfig",
    "CollectorEntry",
    "CollectorScheduleConfig",
    "CredibilityTier",
    "CriticalityTier",
    "DataSourcesConfig",
    "DiscoverySource",
    "DiscoveryVendor",
    "EmergencyInvocation",
    "EnforcementTier",
    "EscalationZones",
    "GuardrailsConfig",
    "NewsOutletsConfig",
    "OutletEntry",
    "ProgressiveTier",
    "ProviderConfig",
    "RetryShapeConfig",
    "RuleEntry",
    "SchedulerConfig",
]
