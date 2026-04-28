"""Smoke import test: every public name from models package resolves (story 02)."""

import alphamind.config.models as models_mod

_EXPECTED_PUBLIC_NAMES = {
    "AdaptiveAgentConfig",
    "AgentName",
    "AgentsConfig",
    "AgentsSection",
    "AllowedModel",
    "Alpaca",
    "AlpacaCredentials",
    "AnalystMode",
    "AnalystOutputMode",
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
    "CommandType",
    "CredibilityTier",
    "CriticalityTier",
    "DataSourcesConfig",
    "DigestConfig",
    "DiscoverySource",
    "DiscoveryVendor",
    "EmergencyInvocation",
    "EnforcementTier",
    "EscalationZones",
    "EventType",
    "ExecutionConfig",
    "ExecutionMode",
    "FailureMode",
    "FeatureFlags",
    "FinalInvocationBeforeEvent",
    "GreeksRefresh",
    "GuardrailsConfig",
    "LLMFailureConfig",
    "LoosenOnExit",
    "MainConfig",
    "Mode",
    "ModeConfig",
    "NewsDigestConfig",
    "NewsOutletsConfig",
    "OrderType",
    "OutletEntry",
    "Overlay",
    "PaperHarness",
    "Paths",
    "PendingOrdersDefault",
    "PmEmphasis",
    "PmMode",
    "PreEventActivation",
    "PreEventOverlay",
    "Profile",
    "ProfileConfig",
    "ProgressiveTier",
    "ProviderConfig",
    "QualitativeResearcherSection",
    "Regime",
    "RegimeChange",
    "RegimeConfig",
    "RetryCondition",
    "RetryPolicy",
    "RetryShapeConfig",
    "RetryStrategy",
    "RiskPriority",
    "RuleEntry",
    "RunType",
    "RunTypeConfig",
    "SchedulerConfig",
    "SectorUnderperform",
    "SessionHours",
    "SessionWindow",
    "SourceSignalSurvivalDrop",
    "StrategistAction",
    "StrategistMode",
    "StrategistOutputMode",
    "StressActivation",
    "StressOverlay",
    "StressTrigger",
    "TightenOnEntry",
    "TokenBudgetRange",
    "TransitionPolicy",
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
