"""Smoke import test: every public name from models package resolves (story 02)."""

import alphamind.config.models as models_mod

_EXPECTED_PUBLIC_NAMES = {
    "Alpaca",
    "AlpacaCredentials",
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
    "ExecutionMode",
    "GuardrailsConfig",
    "MainConfig",
    "NewsOutletsConfig",
    "OutletEntry",
    "Paths",
    "Profile",
    "ProgressiveTier",
    "ProviderConfig",
    "RetryShapeConfig",
    "RuleEntry",
    "SchedulerConfig",
    "SessionHours",
    "SessionWindow",
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
