"""Smoke import test: every public name from models package resolves (story 02)."""

import alphamind.config.models as models_mod

_EXPECTED_PUBLIC_NAMES = {
    "AdaptiveAgentConfig",
    "AgentName",
    "AgentsConfig",
    "AllowedModel",
    "Alpaca",
    "AlpacaCredentials",
    "AntiPatternSpike",
    "AssetRole",
    "AssetsConfig",
    "BackoffStrategy",
    "BaseAgentConfig",
    "Benchmark",
    "BreachResponse",
    "CategoryConfig",
    "CitationChainShift",
    "CollectorEntry",
    "CollectorScheduleConfig",
    "CredibilityTier",
    "CriticalityTier",
    "DataSourcesConfig",
    "DigestConfig",
    "DiscoverySource",
    "DiscoveryVendor",
    "EmergencyInvocation",
    "EnforcementTier",
    "EscalationZones",
    "ExecutionConfig",
    "ExecutionMode",
    "FailureMode",
    "FeatureFlags",
    "GreeksRefresh",
    "GuardrailsConfig",
    "LLMFailureConfig",
    "MainConfig",
    "NewsOutletsConfig",
    "OrderType",
    "OutletEntry",
    "PaperHarness",
    "Paths",
    "Profile",
    "ProfileConfig",
    "ProgressiveTier",
    "ProviderConfig",
    "RegimeChange",
    "RetryCondition",
    "RetryPolicy",
    "RetryShapeConfig",
    "RetryStrategy",
    "RiskPriority",
    "RuleEntry",
    "SchedulerConfig",
    "SectorUnderperform",
    "SessionHours",
    "SessionWindow",
    "SourceSignalSurvivalDrop",
    "TokenBudgetRange",
    "ValidationSuperseded",
    "ValidationWindowEnd",
    "VenueConfig",
}


def test_all_public_names_importable() -> None:
    for name in _EXPECTED_PUBLIC_NAMES:
        assert hasattr(models_mod, name), f"{name!r} not importable from alphamind.config.models"


def test_all_exports_in_dunder_all() -> None:
    assert set(models_mod.__all__) == _EXPECTED_PUBLIC_NAMES


def test_models_is_package() -> None:
    assert hasattr(models_mod, "__path__"), "models must be a package, not a module"


def test_env_example_keys_in_package_namespace() -> None:
    assert hasattr(models_mod, "_ENV_EXAMPLE_KEYS"), (
        "_ENV_EXAMPLE_KEYS must be in the package namespace for monkeypatch"
    )
