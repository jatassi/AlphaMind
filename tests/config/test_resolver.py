"""Tests for the composition resolver (story 05).

Each test exercises ``compose_config`` over already-loaded inputs (read once
from the shipped ``config/`` tree at module scope to keep parse cost off the
parametrized hot path).
"""

from collections.abc import Mapping
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
    AgentName,
    AgentsConfig,
    AnalystOutputMode,
    AssetsConfig,
    CommandType,
    ContinuousMonitorConfig,
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Mode,
    Overlay,
    PendingOrdersDefault,
    Profile,
    ProfileConfig,
    Regime,
    RegimeConfig,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SchedulerConfig,
    VenueConfig,
    compose_config,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once — every test composes from these.
_MAIN = MainConfig.model_validate(_read("main.yaml"))
_SCHEDULER = SchedulerConfig.model_validate(_read("scheduler.yaml"))
_VENUE = VenueConfig.model_validate(_read("venue.yaml"))
_EXECUTION = ExecutionConfig.model_validate(_read("execution.yaml"))
_GUARDRAILS = GuardrailsConfig.model_validate(_read("guardrails.yaml"))
_LLM_FAILURE = LLMFailureConfig.model_validate(_read("llm_failure.yaml"))
_DIGEST = DigestConfig.model_validate(_read("digest.yaml"))
_ASSETS = AssetsConfig.model_validate(_read("assets.yaml"))
_AGENTS = AgentsConfig.model_validate(_read("agents.yaml"))
_CONTINUOUS_MONITOR = ContinuousMonitorConfig.model_validate(_read("continuous_monitor.yaml"))
_PROFILES = load_profiles(CONFIG_DIR)
_REGIMES = load_regimes(CONFIG_DIR)
_MODES = load_modes(CONFIG_DIR)
_OVERLAYS = load_overlays(CONFIG_DIR)
_RUN_TYPES = load_run_types(CONFIG_DIR)


def _compose(
    *,
    profile_override: Profile | None = None,
    regime: Regime = Regime.normal,
    mode: Mode = Mode.normal,
    overlays: tuple[Overlay, ...] = (),
    run_type: RunType = RunType.pre_open,
    profiles: Mapping[Profile, ProfileConfig] | None = None,
    regimes: Mapping[Regime, RegimeConfig] | None = None,
) -> ResolvedConfig:
    """Compose with shipped-tree inputs and runtime-dimension overrides."""
    main = (
        _MAIN
        if profile_override is None
        else _MAIN.model_copy(update={"active_profile": profile_override})
    )
    inputs = LoadedConfig(
        main=main,
        scheduler=_SCHEDULER,
        venue=_VENUE,
        execution=_EXECUTION,
        guardrails=_GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=_ASSETS,
        agents=_AGENTS,
        continuous_monitor=_CONTINUOUS_MONITOR,
        profiles=dict(profiles) if profiles is not None else dict(_PROFILES),
        regimes=dict(regimes) if regimes is not None else dict(_REGIMES),
        modes=dict(_MODES),
        overlays=dict(_OVERLAYS),
        run_types=dict(_RUN_TYPES),
    )
    runtime = RuntimeDimensions(
        active_regime=regime,
        active_mode=mode,
        active_overlays=overlays,
        firing_trigger=run_type,
    )
    return compose_config(inputs, runtime)


# ---------------------------------------------------------------------------
# Cascade arithmetic
# ---------------------------------------------------------------------------


def test_canonical_composition_preserves_profile_base_under_normal_regime() -> None:
    base = _PROFILES[Profile.medium].rule_values["position_max_size_pct"]
    resolved = _compose(profile_override=Profile.medium)
    assert resolved.rule_values["position_max_size_pct"] == pytest.approx(base * 1.0)


def test_elevated_regime_multiplies_position_max_size_pct() -> None:
    base = _PROFILES[Profile.medium].rule_values["position_max_size_pct"]
    resolved = _compose(profile_override=Profile.medium, regime=Regime.elevated)
    assert resolved.rule_values["position_max_size_pct"] == pytest.approx(base * 0.70)


def test_pre_event_overlay_compounds_with_regime() -> None:
    base = _PROFILES[Profile.medium].rule_values["position_max_size_pct"]
    resolved = _compose(
        profile_override=Profile.medium,
        regime=Regime.elevated,
        overlays=(Overlay.pre_event,),
    )
    assert resolved.rule_values["position_max_size_pct"] == pytest.approx(base * 0.70 * 0.80)


# ---------------------------------------------------------------------------
# Mode behavioral transform
# ---------------------------------------------------------------------------


def test_halt_mode_restricts_decision_layer_outputs() -> None:
    resolved = _compose(profile_override=Profile.medium, mode=Mode.halt)
    assert resolved.analyst_output_mode is AnalystOutputMode.watchlist
    assert CommandType.OPEN not in resolved.pm_allowed_command_types
    assert CommandType.ADD not in resolved.pm_allowed_command_types
    assert resolved.pending_orders_default is PendingOrdersDefault.cancel


# ---------------------------------------------------------------------------
# Run-type filter and overrides
# ---------------------------------------------------------------------------


def test_off_hours_rolling_omits_adaptive_researcher_from_enabled_agents() -> None:
    resolved = _compose(profile_override=Profile.medium, run_type=RunType.off_hours_rolling)
    assert AgentName.adaptive_researcher not in resolved.enabled_agents


def test_pre_open_includes_adaptive_researcher_with_tool_call_override() -> None:
    resolved = _compose(profile_override=Profile.medium, run_type=RunType.pre_open)
    assert AgentName.adaptive_researcher in resolved.enabled_agents
    overrides = resolved.agent_overrides[AgentName.adaptive_researcher]
    assert overrides["cumulative_tool_call_limit"] == 25


# ---------------------------------------------------------------------------
# Feature-flag closure
# ---------------------------------------------------------------------------


def test_micro_profile_drops_options_rules_from_resolved_rule_values() -> None:
    resolved = _compose(profile_override=Profile.micro)
    assert all(not key.startswith("options_") for key in resolved.rule_values)
    assert "position_max_loss_options_pct" not in resolved.rule_values
    assert "portfolio_theta_pct_per_day" not in resolved.rule_values
    assert "portfolio_vega_pct_per_iv_point" not in resolved.rule_values


# ---------------------------------------------------------------------------
# Purity and hashability
# ---------------------------------------------------------------------------


def test_resolver_is_pure_same_inputs_yield_equal_and_hash_equal_outputs() -> None:
    first = _compose(profile_override=Profile.medium)
    second = _compose(profile_override=Profile.medium)
    assert first == second
    assert hash(first) == hash(second)


# ---------------------------------------------------------------------------
# Active profile/regime label propagation (consumed by the guardrail-evaluation
# library's LibraryConfig adapter — story 02c reads these to populate
# LibraryConfig.active_profile and LibraryConfig.active_regime)
# ---------------------------------------------------------------------------


def test_resolver_propagates_active_profile_and_regime_labels() -> None:
    resolved = _compose(profile_override=Profile.medium, regime=Regime.elevated)
    assert resolved.profile_label == "medium"
    assert resolved.regime_label == "elevated"


def test_resolver_label_changes_distinguish_hash() -> None:
    medium_normal = _compose(profile_override=Profile.medium, regime=Regime.normal)
    medium_elevated = _compose(profile_override=Profile.medium, regime=Regime.elevated)
    assert medium_normal != medium_elevated
    assert hash(medium_normal) != hash(medium_elevated)


# ---------------------------------------------------------------------------
# Structural error: missing rule in regime multiplier map
# ---------------------------------------------------------------------------


def test_missing_rule_id_in_regime_multipliers_raises_with_rule_id_named() -> None:
    # Build a regime with one of the medium profile's rules absent from its
    # multiplier map. The structural-error contract is that the resolver names
    # the missing rule so the operator can reconcile.
    medium = _PROFILES[Profile.medium]
    missing_rule = "position_max_size_pct"
    incomplete_multipliers = {rule: 1.0 for rule in medium.rule_values if rule != missing_rule}
    broken_regime = RegimeConfig.model_validate(
        {
            "vix_range": [14, 22],
            "multipliers": incomplete_multipliers,
            "transition": {
                "tighten_on_entry": "immediate",
                "loosen_on_exit": "linear_over_invocations_3",
            },
        }
    )
    regimes = dict(_REGIMES)
    regimes[Regime.normal] = broken_regime

    with pytest.raises(KeyError, match=missing_rule):
        _compose(profile_override=Profile.medium, regimes=regimes)
