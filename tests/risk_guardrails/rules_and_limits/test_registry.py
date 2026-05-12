"""Tests for the rule-registry runtime accessor (story 01a).

The registry is a thin runtime wrapper over ``GuardrailsConfig.rules`` that
gives downstream guardrail features a stable lookup surface. These tests
exercise it against the shipped ``config/guardrails.yaml`` plus a few
synthetic constructions for the structural-error paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from alphamind.config.loaders import (
    load_modes,
    load_overlays,
    load_profiles,
    load_regimes,
    load_run_types,
)
from alphamind.config.models import (
    AgentsConfig,
    AssetsConfig,
    BreachResponse,
    ContinuousMonitorConfig,
    DigestConfig,
    EnforcementTier,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Mode,
    Profile,
    Regime,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SchedulerConfig,
    VenueConfig,
    compose_config,
)
from alphamind.risk_guardrails.rules_and_limits import (
    ResolvedRule,
    build_rule_registry,
    iter_active_rules,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read_yaml(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once — every test reads from this snapshot.
_GUARDRAILS = GuardrailsConfig.model_validate(_read_yaml("guardrails.yaml"))
_REGISTRY = build_rule_registry(_GUARDRAILS)


def _resolved_for_profile(profile: Profile) -> ResolvedConfig:
    """Compose a ``ResolvedConfig`` over the shipped tree for ``profile``.

    The composed snapshot's ``rule_values`` is the input ``iter_active_rules``
    joins against the registry; the rest of the snapshot is irrelevant to
    these tests. Loaders run at call time — the per-call cost is small
    relative to the whole-suite cost of importing the loader chain.
    """
    main = MainConfig.model_validate(_read_yaml("main.yaml")).model_copy(
        update={"active_profile": profile}
    )
    inputs = LoadedConfig(
        main=main,
        scheduler=SchedulerConfig.model_validate(_read_yaml("scheduler.yaml")),
        venue=VenueConfig.model_validate(_read_yaml("venue.yaml")),
        execution=ExecutionConfig.model_validate(_read_yaml("execution.yaml")),
        guardrails=_GUARDRAILS,
        llm_failure=LLMFailureConfig.model_validate(_read_yaml("llm_failure.yaml")),
        digest=DigestConfig.model_validate(_read_yaml("digest.yaml")),
        assets=AssetsConfig.model_validate(_read_yaml("assets.yaml")),
        agents=AgentsConfig.model_validate(_read_yaml("agents.yaml")),
        continuous_monitor=ContinuousMonitorConfig.model_validate(
            _read_yaml("continuous_monitor.yaml")
        ),
        profiles=dict(load_profiles(CONFIG_DIR)),
        regimes=dict(load_regimes(CONFIG_DIR)),
        modes=dict(load_modes(CONFIG_DIR)),
        overlays=dict(load_overlays(CONFIG_DIR)),
        run_types=dict(load_run_types(CONFIG_DIR)),
    )
    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
    )
    return compose_config(inputs, runtime)


# The 19 canonical suffixed rule IDs from story 03f's Scope.
_CANONICAL_RULE_IDS: tuple[str, ...] = (
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
)


def test_shipped_guardrails_yaml_builds_a_19_entry_registry() -> None:
    assert len(_REGISTRY) == 19
    assert {rule.id for rule in _REGISTRY} == set(_CANONICAL_RULE_IDS)


def test_get_returns_metadata_for_sector_concentration_pct() -> None:
    rule = _REGISTRY.get("sector_concentration_pct")
    assert rule.id == "sector_concentration_pct"
    assert rule.enforcement_tiers == [
        EnforcementTier.T1,
        EnforcementTier.T2,
        EnforcementTier.T3,
    ]
    assert rule.breach_response is BreachResponse.deferred_to_pm
    assert rule.monitor_between_invocations is True


def test_get_raises_keyerror_naming_the_missing_rule() -> None:
    with pytest.raises(KeyError, match="nonexistent_rule"):
        _REGISTRY.get("nonexistent_rule")


def test_try_get_returns_none_for_missing_rule_without_raising() -> None:
    assert _REGISTRY.try_get("nonexistent_rule") is None


def test_at_tier_t1_includes_canonical_t1_rules_and_excludes_daily_drawdown() -> None:
    t1_ids = {rule.id for rule in _REGISTRY.at_tier(EnforcementTier.T1)}
    assert "position_max_size_pct" in t1_ids
    assert "sector_concentration_pct" in t1_ids
    assert "correlation_max" in t1_ids
    # Daily drawdown is T2+T3 only per the per-rule enforcement summary.
    assert "daily_drawdown_pct" not in t1_ids


def test_at_tier_t3_excludes_t1_and_t2_only_rules() -> None:
    t3_ids = {rule.id for rule in _REGISTRY.at_tier(EnforcementTier.T3)}
    # correlation_max and thesis_dependency_flag_pct are T1+T2 only.
    assert "correlation_max" not in t3_ids
    assert "thesis_dependency_flag_pct" not in t3_ids


def test_requiring_monitor_includes_sector_concentration_and_excludes_position_max_size() -> None:
    monitor_ids = {rule.id for rule in _REGISTRY.requiring_monitor()}
    assert "sector_concentration_pct" in monitor_ids
    # position_max_size_pct is command-time only, so the continuous monitor
    # never re-evaluates it.
    assert "position_max_size_pct" not in monitor_ids


def test_with_progressive_tiers_returns_only_cumulative_drawdown() -> None:
    progressive = _REGISTRY.with_progressive_tiers()
    assert len(progressive) == 1
    assert progressive[0].id == "cumulative_drawdown_pct"


def test_dunder_iter_preserves_declaration_order_len_and_contains_work() -> None:
    declared_order = tuple(rule.id for rule in _GUARDRAILS.rules)
    iterated_order = tuple(rule.id for rule in _REGISTRY)
    assert iterated_order == declared_order
    assert len(_REGISTRY) == 19
    assert "sector_concentration_pct" in _REGISTRY


def test_build_rule_registry_rejects_duplicate_rule_ids() -> None:
    # Re-use a real rule entry, then synthesise a GuardrailsConfig with that
    # entry repeated. ``model_construct`` bypasses the parse-time uniqueness
    # validator so the registry's defensive guard is the only thing that can
    # catch the duplicate.
    duplicated = _GUARDRAILS.rules[0]
    broken = GuardrailsConfig.model_construct(
        rules=[duplicated, duplicated],
        emergency_invocation=_GUARDRAILS.emergency_invocation,
    )
    with pytest.raises(ValueError, match=duplicated.id):
        build_rule_registry(broken)


def test_iter_active_rules_pairs_metadata_with_composed_limits() -> None:
    resolved = _resolved_for_profile(Profile.medium)
    actives = iter_active_rules(resolved, _REGISTRY)

    expected_order = tuple(resolved.rule_values.keys())
    assert tuple(active.metadata.id for active in actives) == expected_order
    for active in actives:
        assert isinstance(active, ResolvedRule)
        assert active.limit == resolved.rule_values[active.metadata.id]
        assert active.metadata is _REGISTRY.get(active.metadata.id)


def test_iter_active_rules_raises_when_resolved_rule_absent_from_registry() -> None:
    resolved = _resolved_for_profile(Profile.medium)
    # Drop one rule from a synthetic registry so the resolved snapshot now
    # references a rule the registry doesn't know about.
    dropped_id = next(iter(resolved.rule_values))
    narrowed = GuardrailsConfig.model_construct(
        rules=[rule for rule in _GUARDRAILS.rules if rule.id != dropped_id],
        emergency_invocation=_GUARDRAILS.emergency_invocation,
    )
    narrowed_registry = build_rule_registry(narrowed)

    with pytest.raises(KeyError, match=dropped_id):
        iter_active_rules(resolved, narrowed_registry)
