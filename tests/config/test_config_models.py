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


# ---------------------------------------------------------------------------
# scheduler.yaml (story 03c)
# ---------------------------------------------------------------------------


_EXPECTED_TRIGGER_KEYS = {
    "market_hours_rolling",
    "off_hours_rolling",
    "pre_open",
    "pre_close",
    "weekend_saturday",
    "weekend_sunday",
}


def _valid_scheduler_raw() -> dict[str, object]:
    return {
        "timezone": "US/Eastern",
        "max_instances": 1,
        "overlap_dedup_lookback_minutes": 30,
        "triggers": {"pre_open": "0 9 * * mon-fri"},
    }


def test_scheduler_yaml_parses_and_exposes_six_trigger_keys() -> None:
    from alphamind.config.models import SchedulerConfig

    data = load_yaml(CONFIG_DIR / "scheduler.yaml")
    config = SchedulerConfig.model_validate(data)
    assert set(config.triggers.keys()) == _EXPECTED_TRIGGER_KEYS


def test_scheduler_rejects_malformed_cron_in_trigger() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {"pre_open": "not-a-cron"}
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


def test_scheduler_rejects_empty_triggers_map() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {}
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


@pytest.mark.parametrize("bad_key", ["pre-open", "Pre_Open"])
def test_scheduler_rejects_non_snake_case_trigger_key(bad_key: str) -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {bad_key: "0 9 * * mon-fri"}
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


def test_scheduler_rejects_max_instances_zero() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["max_instances"] = 0
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# guardrails.yaml (story 03f)
# ---------------------------------------------------------------------------

CANONICAL_GUARDRAIL_RULE_IDS = {
    "position_max_size_pct",
    "position_max_loss_equity_pct",
    "position_max_loss_options_pct",
    "sector_concentration_pct",
    "net_long_pct",
    "net_short_pct",
    "gross_exposure_pct",
    "daily_drawdown_pct",
    "cumulative_drawdown_pct",
    "correlation_max",
    "thesis_dependency_flag_pct",
    "options_delta_pct",
    "portfolio_theta_pct_per_day",
    "portfolio_vega_pct_per_iv_point",
    "total_short_pct",
    "single_short_max_pct",
    "borrow_cost_budget_pct_per_day",
    "min_cash_reserve_pct",
    "pending_order_capital_pct",
}

# Rules whose breach the engine handles autonomously per
# docs/design/06-risk-guardrails/breach-behavior.md § Per-rule classification.
IMMEDIATE_ENGINE_RULE_IDS = {
    "position_max_loss_equity_pct",
    "position_max_loss_options_pct",
    "daily_drawdown_pct",
    "cumulative_drawdown_pct",
    "single_short_max_pct",
}


def _minimal_rule(
    rule_id: str = "position_max_size_pct",
    *,
    enforcement_tiers: list[str] | None = None,
    escalation_zones: dict[str, int] | None = None,
    breach_response: str = "deferred_to_pm",
    monitor_between_invocations: bool = False,
    progressive_tiers: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    rule: dict[str, object] = {
        "id": rule_id,
        "enforcement_tiers": enforcement_tiers or ["T1", "T2", "T3"],
        "escalation_zones": escalation_zones or {"warning": 70, "critical": 85, "hard_block": 95},
        "breach_response": breach_response,
        "monitor_between_invocations": monitor_between_invocations,
    }
    if progressive_tiers is not None:
        rule["progressive_tiers"] = progressive_tiers
    return rule


def _minimal_emergency_invocation() -> dict[str, object]:
    return {"cooldown_minutes": 30, "triggers": ["regime_jump"]}


def test_guardrails_yaml_parses_cleanly() -> None:
    from alphamind.config.models import GuardrailsConfig

    data = load_yaml(CONFIG_DIR / "guardrails.yaml")
    config = GuardrailsConfig.model_validate(data)
    rule_ids = {rule.id for rule in config.rules}
    assert rule_ids == CANONICAL_GUARDRAIL_RULE_IDS
    assert len(config.rules) == 19


def test_guardrails_immediate_engine_classification_matches_breach_behavior() -> None:
    from alphamind.config.models import BreachResponse, GuardrailsConfig

    data = load_yaml(CONFIG_DIR / "guardrails.yaml")
    config = GuardrailsConfig.model_validate(data)
    immediate_engine = {
        rule.id for rule in config.rules if rule.breach_response == BreachResponse.immediate_engine
    }
    assert immediate_engine == IMMEDIATE_ENGINE_RULE_IDS


def test_guardrails_only_cumulative_drawdown_carries_progressive_tiers() -> None:
    from alphamind.config.models import GuardrailsConfig

    data = load_yaml(CONFIG_DIR / "guardrails.yaml")
    config = GuardrailsConfig.model_validate(data)
    rules_with_progressive_tiers = {
        rule.id for rule in config.rules if rule.progressive_tiers is not None
    }
    assert rules_with_progressive_tiers == {"cumulative_drawdown_pct"}


def test_guardrails_rejects_duplicate_rule_id() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [
            _minimal_rule("position_max_size_pct"),
            _minimal_rule("position_max_size_pct"),
        ],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="Duplicate rule id"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_misordered_escalation_zones() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [
            _minimal_rule(
                "position_max_size_pct",
                escalation_zones={"warning": 85, "critical": 70, "hard_block": 95},
            )
        ],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="warning < critical < hard_block"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_progressive_tiers_on_non_cumulative_rule() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [
            _minimal_rule(
                "position_max_size_pct",
                progressive_tiers=[{"trigger_pct": 5, "max_position_size_pct": 3}],
            )
        ],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="must not declare progressive_tiers"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_cumulative_drawdown_without_progressive_tiers() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [
            _minimal_rule(
                "cumulative_drawdown_pct",
                enforcement_tiers=["T2", "T3"],
                escalation_zones={"warning": 50, "critical": 70, "hard_block": 85},
                breach_response="immediate_engine",
                monitor_between_invocations=True,
            )
        ],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="requires progressive_tiers"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_non_monotonic_progressive_tiers() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [
            _minimal_rule(
                "cumulative_drawdown_pct",
                enforcement_tiers=["T2", "T3"],
                escalation_zones={"warning": 50, "critical": 70, "hard_block": 85},
                breach_response="immediate_engine",
                monitor_between_invocations=True,
                progressive_tiers=[
                    {"trigger_pct": 10, "max_position_size_pct": 3},
                    {"trigger_pct": 8, "max_position_size_pct": 2},
                ],
            )
        ],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="monotonically increasing"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_unknown_enforcement_tier() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("position_max_size_pct", enforcement_tiers=["T4"])],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_empty_emergency_triggers() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("position_max_size_pct")],
        "emergency_invocation": {"cooldown_minutes": 30, "triggers": []},
    }
    with pytest.raises(ValidationError):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_invalid_rule_id_pattern() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("Position_Max_Size_Pct")],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises(ValidationError, match="does not match"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_models_are_frozen() -> None:
    from alphamind.config.models import GuardrailsConfig

    data = load_yaml(CONFIG_DIR / "guardrails.yaml")
    config = GuardrailsConfig.model_validate(data)

    with pytest.raises(ValidationError):
        config.__setattr__("rules", [])

    rule = config.rules[0]
    with pytest.raises(ValidationError):
        rule.__setattr__("id", "mutated")

    with pytest.raises(ValidationError):
        rule.escalation_zones.__setattr__("warning", 1)

    with pytest.raises(ValidationError):
        config.emergency_invocation.__setattr__("cooldown_minutes", 0)


# ---------------------------------------------------------------------------
# digest.yaml (story 03h)
# ---------------------------------------------------------------------------


_DIGEST_SHIFT_BLOCKS = (
    "anti_pattern_spike",
    "regime_change",
    "sector_underperform",
    "citation_chain_shift",
    "source_signal_survival_drop",
    "validation_window_end",
    "validation_superseded",
)


def _valid_digest_raw() -> dict[str, object]:
    return {
        "anti_pattern_spike": {
            "baseline_window_weeks": 4,
            "multiplier_vs_baseline": 2.0,
            "min_occurrences_this_week": 5,
        },
        "regime_change": {"enabled": True},
        "sector_underperform": {
            "baseline_window_weeks": 4,
            "median_offset_sigma": 1.5,
        },
        "citation_chain_shift": {
            "baseline_window_weeks": 4,
            "delta_pp_threshold": 20.0,
        },
        "source_signal_survival_drop": {
            "baseline_window_weeks": 4,
            "delta_pp_threshold": 20.0,
        },
        "validation_window_end": {"days_before_due": 7},
        "validation_superseded": {"enabled": True},
    }


def test_digest_yaml_parses_and_exposes_all_seven_shift_blocks() -> None:
    from alphamind.config.models import DigestConfig

    data = load_yaml(CONFIG_DIR / "digest.yaml")
    config = DigestConfig.model_validate(data)

    for block in _DIGEST_SHIFT_BLOCKS:
        assert hasattr(config, block), f"DigestConfig missing block {block!r}"


def test_digest_rejects_missing_regime_change_block() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    del raw["regime_change"]
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_rejects_anti_pattern_multiplier_equal_to_one() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["anti_pattern_spike"] = {
        "baseline_window_weeks": 4,
        "multiplier_vs_baseline": 1.0,
        "min_occurrences_this_week": 5,
    }
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_rejects_anti_pattern_multiplier_below_one() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["anti_pattern_spike"] = {
        "baseline_window_weeks": 4,
        "multiplier_vs_baseline": 0.5,
        "min_occurrences_this_week": 5,
    }
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_rejects_citation_chain_delta_pp_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["citation_chain_shift"] = {"baseline_window_weeks": 4, "delta_pp_threshold": 0}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_accepts_citation_chain_delta_pp_at_upper_bound() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["citation_chain_shift"] = {"baseline_window_weeks": 4, "delta_pp_threshold": 100.0}
    config = DigestConfig.model_validate(raw)
    assert config.citation_chain_shift.delta_pp_threshold == 100.0


def test_digest_rejects_citation_chain_delta_pp_above_upper_bound() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["citation_chain_shift"] = {"baseline_window_weeks": 4, "delta_pp_threshold": 100.1}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_rejects_sector_underperform_median_offset_sigma_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["sector_underperform"] = {"baseline_window_weeks": 4, "median_offset_sigma": 0}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_accepts_validation_window_days_before_due_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["validation_window_end"] = {"days_before_due": 0}
    config = DigestConfig.model_validate(raw)
    assert config.validation_window_end.days_before_due == 0


def test_digest_rejects_validation_window_days_before_due_negative() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["validation_window_end"] = {"days_before_due": -1}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_accepts_anti_pattern_min_occurrences_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["anti_pattern_spike"] = {
        "baseline_window_weeks": 4,
        "multiplier_vs_baseline": 2.0,
        "min_occurrences_this_week": 0,
    }
    config = DigestConfig.model_validate(raw)
    assert config.anti_pattern_spike.min_occurrences_this_week == 0


def test_digest_rejects_baseline_window_weeks_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["sector_underperform"] = {"baseline_window_weeks": 0, "median_offset_sigma": 1.5}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_rejects_sector_underperform_median_offset_sigma_negative() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["sector_underperform"] = {"baseline_window_weeks": 4, "median_offset_sigma": -1.0}
    with pytest.raises(ValidationError):
        DigestConfig.model_validate(raw)


def test_digest_models_are_frozen() -> None:
    from alphamind.config.models import DigestConfig

    data = load_yaml(CONFIG_DIR / "digest.yaml")
    config = DigestConfig.model_validate(data)

    with pytest.raises(ValidationError):
        config.__setattr__("regime_change", config.regime_change)
    with pytest.raises(ValidationError):
        config.anti_pattern_spike.__setattr__("baseline_window_weeks", 99)
    with pytest.raises(ValidationError):
        config.regime_change.__setattr__("enabled", False)
    with pytest.raises(ValidationError):
        config.sector_underperform.__setattr__("median_offset_sigma", 99.0)
    with pytest.raises(ValidationError):
        config.citation_chain_shift.__setattr__("delta_pp_threshold", 99.0)
    with pytest.raises(ValidationError):
        config.source_signal_survival_drop.__setattr__("delta_pp_threshold", 99.0)
    with pytest.raises(ValidationError):
        config.validation_window_end.__setattr__("days_before_due", 99)
    with pytest.raises(ValidationError):
        config.validation_superseded.__setattr__("enabled", False)
