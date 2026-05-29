"""Tests for src/alphamind/config/models.py — configuration scaffolding (story 03a)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


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

    with pytest.raises((ValueError, TypeError)):
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

    with pytest.raises((ValueError, TypeError)):
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
    with pytest.raises((ValueError, TypeError), match="nonexistent"):
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
    with pytest.raises((ValueError, TypeError), match="ghost_provider"):
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
    with pytest.raises((ValueError, TypeError), match="BAD_KEY"):
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
    with pytest.raises((ValueError, TypeError), match="aapl"):
        AssetsConfig.model_validate(raw)


def test_assets_rejects_digit_leading_ticker() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "2026-04-25",
        "discovery_sources": {},
        "sectors": {"tech": ["123XYZ"]},
        "benchmarks": {},
    }
    with pytest.raises((ValueError, TypeError), match="123XYZ"):
        AssetsConfig.model_validate(raw)


def test_assets_rejects_benchmark_key_with_space() -> None:
    from alphamind.config.models import AssetsConfig

    raw = {
        "last_full_validation": "2026-04-25",
        "discovery_sources": {},
        "sectors": {},
        "benchmarks": {"SP Y": {"role": "broad_market", "description": "bad key"}},
    }
    with pytest.raises((ValueError, TypeError), match="SP Y"):
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
    with pytest.raises((ValueError, TypeError)):
        AssetsConfig.model_validate(raw)


def test_assets_models_are_frozen() -> None:
    from alphamind.config.models import AssetsConfig, Benchmark, DiscoverySource

    data = load_yaml(CONFIG_DIR / "assets.yaml")
    config = AssetsConfig.model_validate(data)

    # AssetsConfig itself
    with pytest.raises((ValueError, TypeError)):
        config.sectors = {}

    # Nested DiscoverySource
    src = next(iter(config.discovery_sources.values()))
    assert isinstance(src, DiscoverySource)
    with pytest.raises((ValueError, TypeError)):
        src.etf = "ZZZZ"

    # Nested Benchmark
    bench = next(iter(config.benchmarks.values()))
    assert isinstance(bench, Benchmark)
    with pytest.raises((ValueError, TypeError)):
        bench.description = "mutated"


# ---------------------------------------------------------------------------
# scheduler.yaml (story 03c)
# ---------------------------------------------------------------------------


# Tier B schedule (ALP-745): four scheduled triggers, each at a distinct
# minute. ``off_hours_rolling`` / ``weekend_saturday`` remain valid run types
# (enum members + overlay files retained) but are deliberately unscheduled.
_EXPECTED_TRIGGER_KEYS = {
    "pre_open",
    "market_hours_rolling",
    "pre_close",
    "weekend_sunday",
}


def _valid_scheduler_raw() -> dict[str, object]:
    return {
        "timezone": "US/Eastern",
        "max_instances": 1,
        "overlap_dedup_lookback_minutes": 30,
        "emergency_poll_interval_seconds": 5,
        "market_calendar_exchange": "XNYS",
        "supervisor_shutdown_timeout_seconds": 10,
        "triggers": {"pre_open": "0 9 * * mon-fri"},
    }


def test_scheduler_yaml_parses_and_exposes_tier_b_trigger_keys() -> None:
    from alphamind.config.models import SchedulerConfig

    data = load_yaml(CONFIG_DIR / "scheduler.yaml")
    config = SchedulerConfig.model_validate(data)
    assert set(config.triggers.keys()) == _EXPECTED_TRIGGER_KEYS


def test_scheduler_rejects_malformed_cron_in_trigger() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {"pre_open": "not-a-cron"}
    with pytest.raises((ValueError, TypeError)):
        SchedulerConfig.model_validate(raw)


def test_scheduler_rejects_empty_triggers_map() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {}
    with pytest.raises((ValueError, TypeError)):
        SchedulerConfig.model_validate(raw)


@pytest.mark.parametrize("bad_key", ["pre-open", "Pre_Open"])
def test_scheduler_rejects_non_snake_case_trigger_key(bad_key: str) -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["triggers"] = {bad_key: "0 9 * * mon-fri"}
    with pytest.raises((ValueError, TypeError)):
        SchedulerConfig.model_validate(raw)


def test_scheduler_rejects_max_instances_zero() -> None:
    from alphamind.config.models import SchedulerConfig

    raw = _valid_scheduler_raw()
    raw["max_instances"] = 0
    with pytest.raises((ValueError, TypeError)):
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
    with pytest.raises((ValueError, TypeError), match="Duplicate rule id"):
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
    with pytest.raises((ValueError, TypeError), match="warning < critical < hard_block"):
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
    with pytest.raises((ValueError, TypeError), match="must not declare progressive_tiers"):
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
    with pytest.raises((ValueError, TypeError), match="requires progressive_tiers"):
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
    with pytest.raises((ValueError, TypeError), match="monotonically increasing"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_unknown_enforcement_tier() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("position_max_size_pct", enforcement_tiers=["T4"])],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises((ValueError, TypeError)):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_empty_emergency_triggers() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("position_max_size_pct")],
        "emergency_invocation": {"cooldown_minutes": 30, "triggers": []},
    }
    with pytest.raises((ValueError, TypeError)):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_rejects_invalid_rule_id_pattern() -> None:
    from alphamind.config.models import GuardrailsConfig

    raw = {
        "rules": [_minimal_rule("Position_Max_Size_Pct")],
        "emergency_invocation": _minimal_emergency_invocation(),
    }
    with pytest.raises((ValueError, TypeError), match="does not match"):
        GuardrailsConfig.model_validate(raw)


def test_guardrails_models_are_frozen() -> None:
    from alphamind.config.models import GuardrailsConfig

    data = load_yaml(CONFIG_DIR / "guardrails.yaml")
    config = GuardrailsConfig.model_validate(data)

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("rules", [])

    rule = config.rules[0]
    with pytest.raises((ValueError, TypeError)):
        rule.__setattr__("id", "mutated")

    with pytest.raises((ValueError, TypeError)):
        rule.escalation_zones.__setattr__("warning", 1)

    with pytest.raises((ValueError, TypeError)):
        config.emergency_invocation.__setattr__("cooldown_minutes", 0)


# ---------------------------------------------------------------------------
# main.yaml (story 03b)
# ---------------------------------------------------------------------------


def _valid_main_raw() -> dict[str, object]:
    return {
        "active_profile": "medium",
        "execution_mode": "paper",
        "paths": {
            "database": "%USERPROFILE%\\AlphaMind\\data\\alphamind.db",
            "logs": "%USERPROFILE%\\AlphaMind\\logs",
            "archive": "%USERPROFILE%\\AlphaMind\\archive",
            "prompts": "prompts/",
        },
    }


def test_main_yaml_parses_cleanly_and_exposes_expected_fields() -> None:
    from alphamind.config.models import ExecutionMode, MainConfig, Profile

    data = load_yaml(CONFIG_DIR / "main.yaml")
    config = MainConfig.model_validate(data)
    assert config.active_profile == Profile.medium
    assert config.execution_mode == ExecutionMode.paper
    assert config.paths.database
    assert config.paths.logs
    assert config.paths.archive
    assert config.paths.prompts


def test_main_rejects_unknown_active_profile() -> None:
    from alphamind.config.models import MainConfig

    raw = _valid_main_raw()
    raw["active_profile"] = "huge"
    with pytest.raises((ValueError, TypeError)):
        MainConfig.model_validate(raw)


def test_main_rejects_unknown_execution_mode() -> None:
    from alphamind.config.models import MainConfig

    raw = _valid_main_raw()
    raw["execution_mode"] = "backtest"
    with pytest.raises((ValueError, TypeError)):
        MainConfig.model_validate(raw)


def test_main_rejects_missing_paths_database_key() -> None:
    from alphamind.config.models import MainConfig

    raw = _valid_main_raw()
    raw["paths"] = {
        "logs": "%USERPROFILE%\\AlphaMind\\logs",
        "archive": "%USERPROFILE%\\AlphaMind\\archive",
        "prompts": "prompts/",
    }
    with pytest.raises((ValueError, TypeError)):
        MainConfig.model_validate(raw)


def test_main_preserves_userprofile_path_verbatim() -> None:
    from alphamind.config.models import MainConfig

    raw = _valid_main_raw()
    literal_database = "%USERPROFILE%\\AlphaMind\\data\\alphamind.db"
    raw["paths"] = {
        "database": literal_database,
        "logs": "%USERPROFILE%\\AlphaMind\\logs",
        "archive": "%USERPROFILE%\\AlphaMind\\archive",
        "prompts": "prompts/",
    }
    config = MainConfig.model_validate(raw)
    assert config.paths.database == literal_database


def test_main_models_are_frozen() -> None:
    from alphamind.config.models import MainConfig, Paths

    config = MainConfig.model_validate(_valid_main_raw())

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("execution_mode", "live")

    assert isinstance(config.paths, Paths)
    with pytest.raises((ValueError, TypeError)):
        config.paths.__setattr__("database", "mutated")


# ---------------------------------------------------------------------------
# agents.yaml (story 03i)
# ---------------------------------------------------------------------------


CANONICAL_AGENT_NAMES = {
    "analyst",
    "strategist",
    "portfolio_manager",
    "tech_semis_researcher",
    "financials_researcher",
    "energy_researcher",
    "qualitative_researcher",
    "adaptive_researcher",
    "synthesizer",
}


def _valid_base_agent(
    *,
    model: str = "claude-opus-4-8",
    prompt: str = "prompts/decision/analyst.md",
    tools: list[str] | None = None,
) -> dict[str, object]:
    return {
        "model": model,
        "prompt": prompt,
        "latency_budget_seconds": 180,
        "context_token_budget": 8000,
        "output_token_budget": 2000,
        "tools": tools if tools is not None else [],
    }


def _valid_adaptive_agent(
    *,
    prompt: str = "prompts/analysis/adaptive_researcher.md",
) -> dict[str, object]:
    return {
        "model": "claude-sonnet-4-6",
        "prompt": prompt,
        "latency_budget_seconds": 300,
        "context_token_budget": 8000,
        "output_token_budget": 2000,
        "tools": [],
        "cumulative_tool_call_limit": 25,
        "cumulative_tool_token_budget": 4000,
        "tool_caps": {"news_search": 10},
    }


def _valid_agents_raw() -> dict[str, object]:
    """Build a minimal raw payload that includes one entry per canonical agent name."""
    decision_prompts = {
        "analyst": "prompts/decision/analyst.md",
        "strategist": "prompts/decision/strategist.md",
        "portfolio_manager": "prompts/decision/pm.md",
    }
    sonnet_prompts = {
        "tech_semis_researcher": "prompts/analysis/tech_semis_researcher.md",
        "financials_researcher": "prompts/analysis/financials_researcher.md",
        "energy_researcher": "prompts/analysis/energy_researcher.md",
        "qualitative_researcher": "prompts/analysis/qualitative_researcher.md",
        "synthesizer": "prompts/analysis/synthesizer.md",
    }
    agents: dict[str, object] = {}
    for name, prompt in decision_prompts.items():
        agents[name] = _valid_base_agent(model="claude-opus-4-8", prompt=prompt)
    for name, prompt in sonnet_prompts.items():
        agents[name] = _valid_base_agent(model="claude-sonnet-4-6", prompt=prompt)
    agents["adaptive_researcher"] = _valid_adaptive_agent()
    return {"agents": agents}


def test_agents_yaml_parses_cleanly_and_exposes_all_nine_agents() -> None:
    from alphamind.config.models import AgentsConfig

    data = load_yaml(CONFIG_DIR / "agents.yaml")
    config = AgentsConfig.model_validate(data)
    assert {agent.value for agent in config.agents} == CANONICAL_AGENT_NAMES


def test_agents_rejects_yaml_missing_an_agent() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    del agents_map["synthesizer"]
    with pytest.raises((ValueError, TypeError), match="synthesizer"):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_yaml_with_extra_agent_name() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    agents_map["portfolio_analyst"] = _valid_base_agent()
    with pytest.raises((ValueError, TypeError)):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_nonexistent_prompt_path() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    analyst_entry = agents_map["analyst"]
    assert isinstance(analyst_entry, dict)
    analyst_entry["prompt"] = "prompts/decision/does_not_exist.md"
    with pytest.raises((ValueError, TypeError), match="does_not_exist"):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_unknown_model() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    analyst_entry = agents_map["analyst"]
    assert isinstance(analyst_entry, dict)
    analyst_entry["model"] = "claude-haiku-3-5"
    with pytest.raises((ValueError, TypeError)):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_analyst_carrying_adaptive_only_fields() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    analyst_entry = agents_map["analyst"]
    assert isinstance(analyst_entry, dict)
    analyst_entry["cumulative_tool_call_limit"] = 25
    analyst_entry["cumulative_tool_token_budget"] = 4000
    analyst_entry["tool_caps"] = {"news_search": 10}
    with pytest.raises((ValueError, TypeError), match="must not declare"):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_adaptive_missing_cumulative_tool_call_limit() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    adaptive_entry = agents_map["adaptive_researcher"]
    assert isinstance(adaptive_entry, dict)
    del adaptive_entry["cumulative_tool_call_limit"]
    with pytest.raises((ValueError, TypeError), match="must declare"):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_tool_name_with_capital_letter() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    analyst_entry = agents_map["analyst"]
    assert isinstance(analyst_entry, dict)
    analyst_entry["tools"] = ["Retrieve_Brief"]
    with pytest.raises((ValueError, TypeError), match="Retrieve_Brief"):
        AgentsConfig.model_validate(raw)


def test_agents_rejects_tool_caps_key_with_capital_letter() -> None:
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    adaptive_entry = agents_map["adaptive_researcher"]
    assert isinstance(adaptive_entry, dict)
    adaptive_entry["tool_caps"] = {"News_Search": 5}
    with pytest.raises((ValueError, TypeError), match="News_Search"):
        AgentsConfig.model_validate(raw)


def test_agents_yaml_adaptive_researcher_carries_three_extra_fields() -> None:
    from alphamind.config.models import AdaptiveAgentConfig, AgentName, AgentsConfig

    data = load_yaml(CONFIG_DIR / "agents.yaml")
    config = AgentsConfig.model_validate(data)
    adaptive = config.agents[AgentName.adaptive_researcher]
    assert isinstance(adaptive, AdaptiveAgentConfig)
    assert adaptive.cumulative_tool_call_limit >= 1
    assert adaptive.cumulative_tool_token_budget >= 1
    assert adaptive.tool_caps  # non-empty


def test_agents_yaml_qualitative_researcher_carries_expected_tool_loop_shape() -> None:
    """qualitative_researcher carries the design-doc-driven tool allowlist,
    cumulative caps, and per-tool caps per ALP-241."""
    from alphamind.config.models import AdaptiveAgentConfig, AgentName, AgentsConfig

    data = load_yaml(CONFIG_DIR / "agents.yaml")
    config = AgentsConfig.model_validate(data)
    qualitative = config.agents[AgentName.qualitative_researcher]
    assert isinstance(qualitative, AdaptiveAgentConfig)
    assert qualitative.tools == ["news_search", "prediction_markets", "earnings_commentary"]
    assert qualitative.cumulative_tool_call_limit == 15
    assert qualitative.cumulative_tool_token_budget == 4000
    assert qualitative.tool_caps == {
        "news_search": 8,
        "prediction_markets": 4,
        "earnings_commentary": 4,
    }
    assert qualitative.output_token_budget == 20000


def test_agents_accepts_qualitative_researcher_with_tool_loop_fields() -> None:
    """qualitative_researcher is a tool-loop agent: cumulative_* and tool_caps
    are valid fields on its entry (not adaptive_researcher-only)."""
    from alphamind.config.models import AgentsConfig

    raw = _valid_agents_raw()
    agents_map = raw["agents"]
    assert isinstance(agents_map, dict)
    qualitative_entry = agents_map["qualitative_researcher"]
    assert isinstance(qualitative_entry, dict)
    qualitative_entry["cumulative_tool_call_limit"] = 15
    qualitative_entry["cumulative_tool_token_budget"] = 4000
    qualitative_entry["tool_caps"] = {"news_search": 8}
    AgentsConfig.model_validate(raw)  # does not raise


def test_agents_models_are_frozen() -> None:
    from alphamind.config.models import AgentName, AgentsConfig

    data = load_yaml(CONFIG_DIR / "agents.yaml")
    config = AgentsConfig.model_validate(data)

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("agents", {})

    entry = config.agents[AgentName.analyst]
    with pytest.raises((ValueError, TypeError)):
        entry.__setattr__("model", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# execution.yaml (story 03e)
# ---------------------------------------------------------------------------


def _valid_execution_raw() -> dict[str, object]:
    return {
        "greeks_refresh": {
            "scheduled_interval_minutes": 15,
            "move_trigger_pct": 2.0,
        },
        "conservative_delta_buffer_pct": 10,
        "submission_retry_window_seconds": 30,
        "paper_harness": {
            "spread_buffer_pct": 10,
            "impact_coefficients": {"market": 0.5, "limit": 0.25, "stop": 0.75},
        },
        "pl_target_margin_pct": 5,
    }


def test_execution_yaml_parses_and_exposes_scheduled_interval() -> None:
    from alphamind.config.models import ExecutionConfig

    data = load_yaml(CONFIG_DIR / "execution.yaml")
    config = ExecutionConfig.model_validate(data)
    assert config.greeks_refresh.scheduled_interval_minutes == 15


def test_execution_rejects_missing_stop_in_impact_coefficients() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw["paper_harness"] = {
        "spread_buffer_pct": 10,
        "impact_coefficients": {"market": 0.5, "limit": 0.25},
    }
    with pytest.raises((ValueError, TypeError), match="missing"):
        ExecutionConfig.model_validate(raw)


def test_execution_rejects_unknown_order_type_in_impact_coefficients() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw["paper_harness"] = {
        "spread_buffer_pct": 10,
        "impact_coefficients": {
            "market": 0.5,
            "limit": 0.25,
            "stop": 0.75,
            "trailing_stop": 1.0,
        },
    }
    with pytest.raises((ValueError, TypeError)):
        ExecutionConfig.model_validate(raw)


def test_execution_rejects_zero_market_impact_coefficient() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw["paper_harness"] = {
        "spread_buffer_pct": 10,
        "impact_coefficients": {"market": 0, "limit": 0.25, "stop": 0.75},
    }
    with pytest.raises((ValueError, TypeError), match="market"):
        ExecutionConfig.model_validate(raw)


def test_execution_rejects_negative_conservative_delta_buffer() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw["conservative_delta_buffer_pct"] = -1
    with pytest.raises((ValueError, TypeError)):
        ExecutionConfig.model_validate(raw)


def test_execution_rejects_zero_move_trigger_pct() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw["greeks_refresh"] = {"scheduled_interval_minutes": 15, "move_trigger_pct": 0}
    with pytest.raises((ValueError, TypeError)):
        ExecutionConfig.model_validate(raw)


def test_execution_rejects_missing_pl_target_margin_pct() -> None:
    from alphamind.config.models import ExecutionConfig

    raw = _valid_execution_raw()
    raw.pop("pl_target_margin_pct")
    with pytest.raises((ValueError, TypeError)):
        ExecutionConfig.model_validate(raw)


def test_execution_models_are_frozen() -> None:
    from alphamind.config.models import ExecutionConfig

    data = load_yaml(CONFIG_DIR / "execution.yaml")
    config = ExecutionConfig.model_validate(data)

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("conservative_delta_buffer_pct", 0)

    with pytest.raises((ValueError, TypeError)):
        config.greeks_refresh.__setattr__("scheduled_interval_minutes", 1)

    with pytest.raises((ValueError, TypeError)):
        config.paper_harness.__setattr__("spread_buffer_pct", 0)


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
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


def test_digest_rejects_anti_pattern_multiplier_equal_to_one() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["anti_pattern_spike"] = {
        "baseline_window_weeks": 4,
        "multiplier_vs_baseline": 1.0,
        "min_occurrences_this_week": 5,
    }
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


def test_digest_rejects_anti_pattern_multiplier_below_one() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["anti_pattern_spike"] = {
        "baseline_window_weeks": 4,
        "multiplier_vs_baseline": 0.5,
        "min_occurrences_this_week": 5,
    }
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


def test_digest_rejects_citation_chain_delta_pp_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["citation_chain_shift"] = {"baseline_window_weeks": 4, "delta_pp_threshold": 0}
    with pytest.raises((ValueError, TypeError)):
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
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


def test_digest_rejects_sector_underperform_median_offset_sigma_zero() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["sector_underperform"] = {"baseline_window_weeks": 4, "median_offset_sigma": 0}
    with pytest.raises((ValueError, TypeError)):
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
    with pytest.raises((ValueError, TypeError)):
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
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# distillation.yaml (story 02 — distillation config schema)
# ---------------------------------------------------------------------------


_DISTILLATION_TOP_LEVEL_GROUPS = (
    "anomaly_detection",
    "regime_classification",
    "regime_transition",
    "lead_lag",
    "narrative_lag",
    "persistence_windows",
    "prediction_market",
)


def _valid_distillation_raw() -> dict[str, Any]:
    """Canonical happy-path payload mirroring the shipped distillation.yaml."""
    return {
        "anomaly_detection": {
            "volume_anomaly_sigma": 2.5,
            "price_move_atr_multiple": 1.5,
            "options_low_oi_volume_multiple": 5.0,
            "block_trade_min_shares": 10000,
            "block_trade_min_notional_usd": 500000,
            "dark_pool_one_sided_window_minutes": 60,
            "earnings_revision_cluster_count": 3,
            "earnings_revision_cluster_days": 5,
            "macro_surprise_percentile": 90,
            "funding_stress_component_alert_count": 2,
            "funding_stress_component_percentile": 90,
            "market_liquidity_alert_percentile": 10,
            "news_price_divergence_window_hours": 12,
            "news_price_divergence_min_articles": 5,
        },
        "regime_classification": {
            "regime_low_vol_vix_max": 14.0,
            "regime_normal_vix_min": 14.0,
            "regime_normal_vix_max": 22.0,
            "regime_elevated_vix_min": 22.0,
            "regime_elevated_vix_max": 35.0,
            "regime_crisis_vix_min": 35.0,
            "regime_term_structure_backwardation_threshold": 0.0,
            "regime_vvix_high_percentile": 80,
            "regime_vvix_low_percentile": 30,
        },
        "regime_transition": {
            "regime_transition_confirmed_invocations": 2,
            "regime_transition_indicator_agreement_min": 3,
            "regime_skip_emergency_trigger": True,
        },
        "lead_lag": {
            "pairs": [
                {"key": "credit_to_equity", "lead": "HYG", "lag": "SPY"},
                {"key": "semis_to_tech", "lead": "SOXX", "lag": "QQQ"},
                {"key": "financials_to_market", "lead": "XLF", "lag": "SPY"},
                {"key": "commodity_to_energy_equity", "lead": "USO", "lag": "XLE"},
            ],
            "lead_lag_funding_to_credit_max_days": 3,
            "lead_lag_credit_to_equity_max_days": 3,
            "lead_lag_semis_to_tech_max_days": 2,
            "lead_lag_financials_to_market_max_days": 1,
            "lead_lag_commodity_to_energy_equity_max_days": 1,
            "lead_lag_overdue_lead_sigma": 1.5,
        },
        "narrative_lag": {
            "narrative_lag_correlation_shift_sigma": 1.5,
            "correlation_breakdown_sigma": 3.0,
            "correlation_min_overlap_fraction": 0.9,
            "correlation_noise_floor": 0.05,
            "correlation_breakdown_fdr_q": 0.05,
            "correlation_locus_pair_count_threshold": 3,
            "narrative_lag_media_silence_hours": 12,
        },
        "persistence_windows": {
            "volume_baseline_days": 20,
            "atr_baseline_days": 14,
            "spread_baseline_days": 20,
            "correlation_short_days": 20,
            "correlation_long_days": 60,
            "sentiment_baseline_days": 60,
            "sentiment_min_observations": 30,
            "gap_fill_baseline_days": 252,
            "gap_fill_min_events": 30,
            "extended_hours_confirmation_days": 90,
            "extended_hours_min_events": 20,
            "prediction_market_history_days": 30,
            "funding_stress_baseline_days": 60,
            "market_liquidity_baseline_days": 60,
        },
        "prediction_market": {
            "prediction_market_delta_pp_threshold": 5.0,
            "prediction_market_low_liquidity_volume_min_usd": 10000,
        },
    }


def test_distillation_yaml_parses_and_exposes_all_seven_groups() -> None:
    from alphamind.config.models import DistillationConfig

    data = load_yaml(CONFIG_DIR / "distillation.yaml")
    config = DistillationConfig.model_validate(data)

    for group in _DISTILLATION_TOP_LEVEL_GROUPS:
        assert hasattr(config, group), f"DistillationConfig missing group {group!r}"


def test_distillation_rejects_negative_sigma() -> None:
    """Invariant: all `*_sigma` thresholds ≥ 0."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["volume_anomaly_sigma"] = -0.1
    with pytest.raises((ValueError, TypeError), match="volume_anomaly_sigma"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_negative_multiple() -> None:
    """Invariant: all `*_multiple` thresholds ≥ 0."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["price_move_atr_multiple"] = -0.5
    with pytest.raises((ValueError, TypeError), match="price_move_atr_multiple"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_zero_baseline_days() -> None:
    """Invariant: all `*_days` windows ≥ 1."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["persistence_windows"]["volume_baseline_days"] = 0
    with pytest.raises((ValueError, TypeError), match="volume_baseline_days"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_zero_minutes_window() -> None:
    """Invariant: all `*_minutes` windows ≥ 1."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["dark_pool_one_sided_window_minutes"] = 0
    with pytest.raises((ValueError, TypeError), match="dark_pool_one_sided_window_minutes"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_zero_hours_window() -> None:
    """Invariant: all `*_hours` windows ≥ 1."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["news_price_divergence_window_hours"] = 0
    with pytest.raises((ValueError, TypeError), match="news_price_divergence_window_hours"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_empty_lead_lag_pairs() -> None:
    """Invariant: `lead_lag.pairs` must be non-empty."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["lead_lag"]["pairs"] = []
    with pytest.raises((ValueError, TypeError), match="pairs"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_malformed_lead_lag_pair_ticker() -> None:
    """Invariant: each ticker in `lead_lag.pairs` matches the ticker regex."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["lead_lag"]["pairs"] = [{"key": "x", "lead": "hyg", "lag": "SPY"}]  # lowercase ticker
    with pytest.raises((ValueError, TypeError), match="pair ticker"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_malformed_lead_lag_pair_key() -> None:
    """Invariant: each pair key matches snake_case regex."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["lead_lag"]["pairs"] = [{"key": "BadKey", "lead": "HYG", "lag": "SPY"}]
    with pytest.raises((ValueError, TypeError), match="pair key"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_duplicate_lead_lag_pair_keys() -> None:
    """Invariant: pair keys must be unique."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["lead_lag"]["pairs"] = [
        {"key": "dup", "lead": "HYG", "lag": "SPY"},
        {"key": "dup", "lead": "USO", "lag": "XLE"},
    ]
    with pytest.raises((ValueError, TypeError), match="pair keys must be unique"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_sentiment_min_observations_above_baseline() -> None:
    """Invariant: `sentiment_min_observations` ≤ `sentiment_baseline_days`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["persistence_windows"]["sentiment_min_observations"] = 100
    raw["persistence_windows"]["sentiment_baseline_days"] = 60
    with pytest.raises((ValueError, TypeError), match="sentiment_min_observations"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_gap_fill_min_events_above_baseline() -> None:
    """Invariant: `gap_fill_min_events` ≤ `gap_fill_baseline_days`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["persistence_windows"]["gap_fill_min_events"] = 300
    raw["persistence_windows"]["gap_fill_baseline_days"] = 252
    with pytest.raises((ValueError, TypeError), match="gap_fill_min_events"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_extended_hours_min_events_above_confirmation() -> None:
    """Invariant: `extended_hours_min_events` ≤ `extended_hours_confirmation_days`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["persistence_windows"]["extended_hours_min_events"] = 100
    raw["persistence_windows"]["extended_hours_confirmation_days"] = 90
    with pytest.raises((ValueError, TypeError), match="extended_hours_min_events"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_low_vol_normal_boundary_mismatch() -> None:
    """Invariant: `regime_low_vol_vix_max == regime_normal_vix_min`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_classification"]["regime_low_vol_vix_max"] = 14.0
    raw["regime_classification"]["regime_normal_vix_min"] = 15.0
    with pytest.raises(
        (ValueError, TypeError), match=r"regime_low_vol_vix_max.*regime_normal_vix_min"
    ):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_normal_elevated_boundary_mismatch() -> None:
    """Invariant: `regime_normal_vix_max == regime_elevated_vix_min`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_classification"]["regime_normal_vix_max"] = 22.0
    raw["regime_classification"]["regime_elevated_vix_min"] = 23.0
    with pytest.raises(
        (ValueError, TypeError), match=r"regime_normal_vix_max.*regime_elevated_vix_min"
    ):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_elevated_crisis_boundary_mismatch() -> None:
    """Invariant: `regime_elevated_vix_max == regime_crisis_vix_min`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_classification"]["regime_elevated_vix_max"] = 35.0
    raw["regime_classification"]["regime_crisis_vix_min"] = 36.0
    with pytest.raises(
        (ValueError, TypeError), match=r"regime_elevated_vix_max.*regime_crisis_vix_min"
    ):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_non_monotonic_regime_ceilings() -> None:
    """Invariant: low_vol_max < normal_max < elevated_max (strict)."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    # Make normal_max equal to low_vol_max — breaks strict monotonicity but
    # leaves the equal-boundary invariants intact.
    raw["regime_classification"]["regime_low_vol_vix_max"] = 22.0
    raw["regime_classification"]["regime_normal_vix_min"] = 22.0
    raw["regime_classification"]["regime_normal_vix_max"] = 22.0
    raw["regime_classification"]["regime_elevated_vix_min"] = 22.0
    with pytest.raises((ValueError, TypeError), match=r"strictly|monotonic|<"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_vvix_low_at_or_above_high() -> None:
    """Invariant: `regime_vvix_low_percentile < regime_vvix_high_percentile`."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_classification"]["regime_vvix_low_percentile"] = 80
    raw["regime_classification"]["regime_vvix_high_percentile"] = 80
    with pytest.raises(
        ValidationError, match=r"regime_vvix_low_percentile.*regime_vvix_high_percentile"
    ):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_funding_stress_alert_count_zero() -> None:
    """Invariant: `funding_stress_component_alert_count` in [1, 4]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["funding_stress_component_alert_count"] = 0
    with pytest.raises((ValueError, TypeError), match="funding_stress_component_alert_count"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_funding_stress_alert_count_above_four() -> None:
    """Invariant: `funding_stress_component_alert_count` in [1, 4]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["funding_stress_component_alert_count"] = 5
    with pytest.raises((ValueError, TypeError), match="funding_stress_component_alert_count"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_funding_stress_percentile_below_fifty() -> None:
    """Invariant: `funding_stress_component_percentile` in [50, 100]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["funding_stress_component_percentile"] = 49
    with pytest.raises((ValueError, TypeError), match="funding_stress_component_percentile"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_market_liquidity_percentile_above_fifty() -> None:
    """Invariant: `market_liquidity_alert_percentile` in [0, 50]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["market_liquidity_alert_percentile"] = 51
    with pytest.raises((ValueError, TypeError), match="market_liquidity_alert_percentile"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_prediction_market_delta_pp_zero() -> None:
    """Invariant: `prediction_market_delta_pp_threshold` in (0, 100]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["prediction_market"]["prediction_market_delta_pp_threshold"] = 0
    with pytest.raises((ValueError, TypeError), match="prediction_market_delta_pp_threshold"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_prediction_market_delta_pp_above_hundred() -> None:
    """Invariant: `prediction_market_delta_pp_threshold` in (0, 100]."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["prediction_market"]["prediction_market_delta_pp_threshold"] = 100.1
    with pytest.raises((ValueError, TypeError), match="prediction_market_delta_pp_threshold"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_regime_transition_confirmed_invocations_zero() -> None:
    """Invariant: `regime_transition_confirmed_invocations` ≥ 1."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_transition"]["regime_transition_confirmed_invocations"] = 0
    with pytest.raises((ValueError, TypeError), match="regime_transition_confirmed_invocations"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_non_numeric_sigma() -> None:
    """Type-coercion: a string where a numeric is expected raises ValidationError."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["anomaly_detection"]["volume_anomaly_sigma"] = "two-and-a-half"
    with pytest.raises((ValueError, TypeError), match="volume_anomaly_sigma"):
        DistillationConfig.model_validate(raw)


def test_distillation_rejects_non_boolean_skip_emergency_trigger() -> None:
    """Type-coercion: a non-boolean for the boolean flag raises ValidationError."""
    from alphamind.config.models import DistillationConfig

    raw = _valid_distillation_raw()
    raw["regime_transition"]["regime_skip_emergency_trigger"] = "not-a-bool"
    with pytest.raises((ValueError, TypeError), match="regime_skip_emergency_trigger"):
        DistillationConfig.model_validate(raw)


def test_digest_rejects_sector_underperform_median_offset_sigma_negative() -> None:
    from alphamind.config.models import DigestConfig

    raw = _valid_digest_raw()
    raw["sector_underperform"] = {"baseline_window_weeks": 4, "median_offset_sigma": -1.0}
    with pytest.raises((ValueError, TypeError)):
        DigestConfig.model_validate(raw)


def test_digest_models_are_frozen() -> None:
    from alphamind.config.models import DigestConfig

    data = load_yaml(CONFIG_DIR / "digest.yaml")
    config = DigestConfig.model_validate(data)

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("regime_change", config.regime_change)
    with pytest.raises((ValueError, TypeError)):
        config.anti_pattern_spike.__setattr__("baseline_window_weeks", 99)
    with pytest.raises((ValueError, TypeError)):
        config.regime_change.__setattr__("enabled", False)
    with pytest.raises((ValueError, TypeError)):
        config.sector_underperform.__setattr__("median_offset_sigma", 99.0)
    with pytest.raises((ValueError, TypeError)):
        config.citation_chain_shift.__setattr__("delta_pp_threshold", 99.0)
    with pytest.raises((ValueError, TypeError)):
        config.source_signal_survival_drop.__setattr__("delta_pp_threshold", 99.0)
    with pytest.raises((ValueError, TypeError)):
        config.validation_window_end.__setattr__("days_before_due", 99)
    with pytest.raises((ValueError, TypeError)):
        config.validation_superseded.__setattr__("enabled", False)


# ---------------------------------------------------------------------------
# llm_failure.yaml (story 03g)
# ---------------------------------------------------------------------------

CANONICAL_FAILURE_MODES = {
    "model_api_error",
    "timeout",
    "malformed_output",
    "context_overflow",
    "tool_use_error",
}


def _valid_llm_failure_retries() -> dict[str, dict[str, object]]:
    return {
        "model_api_error": {"attempts": 3, "backoff": "exponential"},
        "timeout": {"attempts": 1, "strategy": "doubled_latency_budget"},
        "malformed_output": {"attempts": 1, "strategy": "same_context_corrective"},
        "context_overflow": {"attempts": 0},
        "tool_use_error": {"attempts": 1, "condition": "idempotent_only"},
    }


def test_llm_failure_yaml_parses_cleanly_and_exposes_all_modes() -> None:
    from alphamind.config.models import FailureMode, LLMFailureConfig

    data = load_yaml(CONFIG_DIR / "llm_failure.yaml")
    config = LLMFailureConfig.model_validate(data)
    assert {mode.value for mode in config.retries} == CANONICAL_FAILURE_MODES
    assert set(FailureMode) == {FailureMode(m) for m in CANONICAL_FAILURE_MODES}


def test_llm_failure_rejects_missing_tool_use_error_entry() -> None:
    from alphamind.config.models import LLMFailureConfig

    retries = _valid_llm_failure_retries()
    del retries["tool_use_error"]
    with pytest.raises((ValueError, TypeError), match="tool_use_error"):
        LLMFailureConfig.model_validate({"retries": retries})


def test_llm_failure_rejects_context_overflow_with_positive_attempts() -> None:
    from alphamind.config.models import LLMFailureConfig

    retries = _valid_llm_failure_retries()
    retries["context_overflow"] = {"attempts": 1}
    with pytest.raises((ValueError, TypeError), match="context_overflow"):
        LLMFailureConfig.model_validate({"retries": retries})


def test_llm_failure_rejects_model_api_error_without_backoff() -> None:
    from alphamind.config.models import LLMFailureConfig

    retries = _valid_llm_failure_retries()
    retries["model_api_error"] = {"attempts": 3}
    with pytest.raises((ValueError, TypeError), match="model_api_error"):
        LLMFailureConfig.model_validate({"retries": retries})


def test_llm_failure_rejects_tool_use_error_with_backoff() -> None:
    from alphamind.config.models import LLMFailureConfig

    retries = _valid_llm_failure_retries()
    retries["tool_use_error"] = {
        "attempts": 1,
        "condition": "idempotent_only",
        "backoff": "exponential",
    }
    with pytest.raises((ValueError, TypeError), match="tool_use_error"):
        LLMFailureConfig.model_validate({"retries": retries})


def test_llm_failure_accepts_timeout_with_alternate_known_strategy() -> None:
    from alphamind.config.models import FailureMode, LLMFailureConfig, RetryStrategy

    retries = _valid_llm_failure_retries()
    retries["timeout"] = {"attempts": 1, "strategy": "same_context_corrective"}
    config = LLMFailureConfig.model_validate({"retries": retries})
    assert config.retries[FailureMode.timeout].strategy is RetryStrategy.same_context_corrective


def test_llm_failure_models_are_frozen() -> None:
    from alphamind.config.models import FailureMode, LLMFailureConfig

    data = load_yaml(CONFIG_DIR / "llm_failure.yaml")
    config = LLMFailureConfig.model_validate(data)

    with pytest.raises((ValueError, TypeError)):
        config.__setattr__("retries", {})

    policy = config.retries[FailureMode.model_api_error]
    with pytest.raises((ValueError, TypeError)):
        policy.__setattr__("attempts", 99)
