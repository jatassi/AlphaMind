"""Tests for src/alphamind/config/models.py — configuration scaffolding (story 03a)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def load_yaml(path: Path) -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# news_outlets.yaml
# ---------------------------------------------------------------------------


def test_news_outlets_yaml_parses_cleanly() -> None:
    from alphamind.config.models import NewsOutletsConfig

    data = load_yaml(CONFIG_DIR / "news_outlets.yaml")
    config = NewsOutletsConfig.model_validate(data)
    assert len(config.outlets) > 0


def test_news_outlets_rejects_unknown_tier() -> None:
    from alphamind.config.models import NewsOutletsConfig

    with pytest.raises(ValidationError):
        NewsOutletsConfig.model_validate({"outlets": {"FakeNews": {"tier": "tier_4"}}})


# ---------------------------------------------------------------------------
# collector_schedule.yaml
# ---------------------------------------------------------------------------


def test_collector_schedule_yaml_parses_cleanly() -> None:
    from alphamind.config.models import CollectorScheduleConfig

    data = load_yaml(CONFIG_DIR / "collector_schedule.yaml")
    config = CollectorScheduleConfig.model_validate(data)
    assert len(config.collectors) > 0


def test_collector_schedule_rejects_malformed_cron() -> None:
    from alphamind.config.models import CollectorScheduleConfig

    with pytest.raises(ValidationError):
        CollectorScheduleConfig.model_validate(
            {"timezone": "US/Eastern", "collectors": {"bad.job": {"cron": "not-a-cron"}}}
        )


# ---------------------------------------------------------------------------
# data_sources.yaml
# ---------------------------------------------------------------------------


def test_data_sources_yaml_parses_cleanly() -> None:
    from alphamind.config.models import DataSourcesConfig

    data = load_yaml(CONFIG_DIR / "data_sources.yaml")
    config = DataSourcesConfig.model_validate(data)
    assert len(config.providers) > 0
    assert len(config.retry_shapes) > 0
    assert len(config.categories) > 0


def test_data_sources_rejects_unknown_retry_shape() -> None:
    from alphamind.config.models import DataSourcesConfig

    raw = {
        "providers": {
            "bad_provider": {
                "api_key_env": "POLYGON_API_KEY",
                "rate_limit_per_minute": 100,
                "retry_shape": "nonexistent",
            }
        },
        "retry_shapes": {"critical": {"attempts": 3, "backoff": "exponential", "failover": True}},
        "categories": {},
    }
    with pytest.raises(ValidationError, match="nonexistent"):
        DataSourcesConfig.model_validate(raw)


def test_data_sources_rejects_category_with_missing_primary() -> None:
    from alphamind.config.models import DataSourcesConfig

    raw = {
        "providers": {
            "polygon": {
                "api_key_env": "POLYGON_API_KEY",
                "rate_limit_per_minute": 100,
                "retry_shape": "critical",
            }
        },
        "retry_shapes": {"critical": {"attempts": 3, "backoff": "exponential", "failover": True}},
        "categories": {
            "q1_price_volume": {
                "tier": "critical",
                "freshness_max_seconds": 300,
                "primary": "ghost_provider",
                "failover": [],
            }
        },
    }
    with pytest.raises(ValidationError, match="ghost_provider"):
        DataSourcesConfig.model_validate(raw)


def test_data_sources_rejects_api_key_env_not_in_env_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alphamind.config.models as models_mod

    monkeypatch.setattr(models_mod, "_ENV_EXAMPLE_KEYS", frozenset({"POLYGON_API_KEY"}))

    raw = {
        "providers": {
            "polygon": {
                "api_key_env": "BAD_KEY",
                "rate_limit_per_minute": 100,
                "retry_shape": "critical",
            }
        },
        "retry_shapes": {"critical": {"attempts": 3, "backoff": "exponential", "failover": True}},
        "categories": {},
    }
    with pytest.raises(ValidationError, match="BAD_KEY"):
        models_mod.DataSourcesConfig.model_validate(raw)


def test_data_sources_api_key_env_all_in_env_example() -> None:
    from alphamind.config.models import DataSourcesConfig

    data = load_yaml(CONFIG_DIR / "data_sources.yaml")
    DataSourcesConfig.model_validate(data)


# ---------------------------------------------------------------------------
# assets.yaml (story 03a)
# ---------------------------------------------------------------------------


def test_assets_yaml_parses_cleanly_and_exposes_expected_sectors() -> None:
    from alphamind.config.models import AssetsConfig

    data = load_yaml(CONFIG_DIR / "assets.yaml")
    config = AssetsConfig.model_validate(data)

    assert "AAPL" in config.sectors["tech"]
    assert "NVDA" in config.sectors["semis"]
    assert "JPM" in config.sectors["financials"]
    assert "COP" in config.sectors["energy"]


def test_assets_rejects_lowercase_ticker() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "2026-04-25",
        "discovery_sources": {},
        "sectors": {"tech": ["aapl"]},
        "benchmarks": {},
    }
    with pytest.raises(ValidationError, match="aapl"):
        AssetsConfig.model_validate(raw)


def test_assets_rejects_digit_leading_ticker() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "2026-04-25",
        "discovery_sources": {},
        "sectors": {"tech": ["123XYZ"]},
        "benchmarks": {},
    }
    with pytest.raises(ValidationError, match="123XYZ"):
        AssetsConfig.model_validate(raw)


def test_assets_rejects_benchmark_key_with_space() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "2026-04-25",
        "discovery_sources": {},
        "sectors": {},
        "benchmarks": {"SP Y": {"role": "broad_market", "description": "bad key"}},
    }
    with pytest.raises(ValidationError, match="SP Y"):
        AssetsConfig.model_validate(raw)


def test_assets_accepts_absent_last_full_validation() -> None:
    from alphamind.config.models import AssetsConfig

    raw: dict[str, object] = {
        "discovery_sources": {},
        "sectors": {},
        "benchmarks": {},
    }
    config = AssetsConfig.model_validate(raw)
    assert config.last_full_validation is None


def test_assets_rejects_malformed_last_full_validation() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "not-a-date",
        "discovery_sources": {},
        "sectors": {},
        "benchmarks": {},
    }
    with pytest.raises(ValidationError):
        AssetsConfig.model_validate(raw)


def test_assets_models_are_frozen() -> None:
    from alphamind.config.models import AssetsConfig, Benchmark, DiscoverySource

    data = load_yaml(CONFIG_DIR / "assets.yaml")
    config = AssetsConfig.model_validate(data)

    # AssetsConfig itself
    with pytest.raises(ValidationError):
        config.sectors = {}

    # Nested DiscoverySource
    src = next(iter(config.discovery_sources.values()))
    assert isinstance(src, DiscoverySource)
    with pytest.raises(ValidationError):
        src.etf = "ZZZZ"

    # Nested Benchmark
    bench = next(iter(config.benchmarks.values()))
    assert isinstance(bench, Benchmark)
    with pytest.raises(ValidationError):
        bench.description = "mutated"
