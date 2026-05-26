"""Tests for the effective-limit adapter and feature-flag gate (story 02c).

The adapter (``from_resolved_config``) bridges the upstream configuration
resolver (``compose_config``) to the library's narrower ``LibraryConfig``
surface. The feature gate (``classify_feature_gate``) is a per-proposal
classifier that returns the canonical ``feature_disabled`` rejection for
proposed deltas referencing instrument classes the active profile disables.

The adapter tests compose synthetic ``ResolvedConfig`` objects from the shipped
configuration tree (read once at module scope) and exercise the adapter over
known fixtures. The gate tests assemble narrow ``LibraryConfig``/``ProposedDelta``
fixtures inline.
"""

from __future__ import annotations

import dataclasses
import pickle
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest
import yaml

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money
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
    ContinuousMonitorConfig,
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Mode,
    Overlay,
    Profile,
    Regime,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SchedulerConfig,
    VenueConfig,
    compose_config,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    ContractType,
    Direction,
    EffectiveLimitAdapterError,
    FeatureDisabledRejection,
    FeatureFlagsView,
    LibraryConfig,
    OptionLeg,
    ProposedDelta,
    classify_feature_gate,
    from_resolved_config,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once; adapter tests compose against these.
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
    execution_override: ExecutionConfig | None = None,
) -> ResolvedConfig:
    """Compose a ``ResolvedConfig`` from the shipped tree with overrides."""
    main = (
        _MAIN
        if profile_override is None
        else _MAIN.model_copy(update={"active_profile": profile_override})
    )
    inputs = LoadedConfig(
        main=main,
        scheduler=_SCHEDULER,
        venue=_VENUE,
        execution=execution_override if execution_override is not None else _EXECUTION,
        guardrails=_GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=_ASSETS,
        agents=_AGENTS,
        continuous_monitor=_CONTINUOUS_MONITOR,
        profiles=dict(_PROFILES),
        regimes=dict(_REGIMES),
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
# Adapter happy path
# ---------------------------------------------------------------------------


def test_adapter_happy_path_medium_normal_no_overlays() -> None:
    """Medium x normal x no overlays produces a ``LibraryConfig`` whose
    ``effective_limits`` equals the resolved ``rule_values``, every rule has
    populated ``escalation_zones``, both flags are enabled, sectors are the
    medium-profile tuple, and ``conservative_buffer_pct`` is the shipped
    execution value."""
    resolved = _compose(profile_override=Profile.medium)

    config = from_resolved_config(resolved)

    assert isinstance(config, LibraryConfig)
    assert dict(config.effective_limits) == dict(resolved.rule_values)
    # Every rule in effective_limits must have a populated escalation_zones entry.
    assert set(config.escalation_zones.keys()) == set(config.effective_limits.keys())
    assert config.feature_flags == FeatureFlagsView(
        options_enabled=True, short_selling_enabled=True
    )
    assert config.active_sectors == ("tech", "semis", "financials", "energy")
    assert config.active_profile == "medium"
    assert config.active_regime == "normal"
    assert config.conservative_buffer_pct == _EXECUTION.conservative_delta_buffer_pct


def test_adapter_does_not_re_cascade_under_regime_and_overlay() -> None:
    """``effective_limits`` byte-equals the resolver's ``rule_values`` even when
    a regime multiplier and an overlay multiplier are active. The cascade is
    upstream's job; the adapter must not re-multiply."""
    resolved = _compose(
        profile_override=Profile.medium,
        regime=Regime.elevated,
        overlays=(Overlay.pre_event,),
    )

    config = from_resolved_config(resolved)

    # The medium profile carries sector_concentration_pct=25; elevated multiplies
    # by 0.80; pre_event does not multiply this rule. The adapter copies what
    # the resolver already produced — verifying the value pins the no-re-cascade
    # contract.
    assert resolved.rule_values["sector_concentration_pct"] == pytest.approx(25 * 0.80 * 1.0)
    assert dict(config.effective_limits) == dict(resolved.rule_values)


def test_adapter_copies_escalation_zones_per_rule_id() -> None:
    """Per-rule ``escalation_zones`` are looked up by rule ID in
    ``guardrails.rules`` and converted to the library's frozen dataclass.

    Spot-checks one default-band rule (``position_max_size_pct``: 70/85/95) and
    one rule with a tighter band (``daily_drawdown_pct``: 60/80/90) per the
    shipped ``guardrails.yaml``."""
    resolved = _compose(profile_override=Profile.medium)

    config = from_resolved_config(resolved)

    default_rule = config.escalation_zones["position_max_size_pct"]
    assert (default_rule.warning, default_rule.critical, default_rule.hard_block) == (
        70.0,
        85.0,
        95.0,
    )
    drawdown_rule = config.escalation_zones["daily_drawdown_pct"]
    assert (drawdown_rule.warning, drawdown_rule.critical, drawdown_rule.hard_block) == (
        60.0,
        80.0,
        90.0,
    )


# ---------------------------------------------------------------------------
# Adapter structural errors
# ---------------------------------------------------------------------------


def test_adapter_raises_when_rule_value_is_missing_from_guardrails_rules() -> None:
    """A rule key in ``rule_values`` that is absent from ``guardrails.rules``
    is a structural error post-cross-reference validation; the adapter raises
    ``EffectiveLimitAdapterError`` naming the rule."""
    resolved = _compose(profile_override=Profile.medium)
    bogus_rule_values = MappingProxyType(
        {**dict(resolved.rule_values), "bogus_orphan_rule_pct": 1.0}
    )
    broken = dataclasses.replace(resolved, rule_values=bogus_rule_values)

    with pytest.raises(EffectiveLimitAdapterError, match="bogus_orphan_rule_pct"):
        from_resolved_config(broken)


@pytest.mark.parametrize("invalid_buffer", [-5.0, 150.0])
def test_adapter_raises_when_conservative_buffer_pct_is_out_of_range(
    invalid_buffer: float,
) -> None:
    """The conservative delta buffer must lie in ``[0, 100]``. Negative or
    >100 values are a structural inconsistency the adapter rejects."""
    bogus_execution = _EXECUTION.model_copy(
        update={"conservative_delta_buffer_pct": invalid_buffer}
    )
    resolved = _compose(profile_override=Profile.medium, execution_override=bogus_execution)

    with pytest.raises(EffectiveLimitAdapterError, match="conservative_delta_buffer_pct"):
        from_resolved_config(resolved)


# ---------------------------------------------------------------------------
# Adapter purity
# ---------------------------------------------------------------------------


def test_adapter_output_round_trips_through_pickle() -> None:
    """Regression for ALP-681. A ``LibraryConfig`` produced by
    ``from_resolved_config`` survives ``pickle.dumps`` / ``pickle.loads``
    intact. The subprocess-isolated agents (``invoke_analyst_in_subprocess``,
    ``invoke_strategist_in_subprocess``, ``invoke_portfolio_manager_in_subprocess``;
    PR #179 / ALP-650) ship the carrying ``ValidationToolState`` across a
    process boundary via base64-pickle and rely on this property."""
    resolved = _compose(profile_override=Profile.medium)

    config = from_resolved_config(resolved)
    restored = pickle.loads(pickle.dumps(config))

    assert restored == config


def test_adapter_is_pure_two_calls_produce_equal_and_hash_equal_outputs() -> None:
    """Two calls to ``from_resolved_config`` on equal ``ResolvedConfig`` inputs
    produce ``LibraryConfig`` outputs that compare equal and hash equal — the
    pure-function contract the adapter inherits from the resolver."""
    resolved_a = _compose(profile_override=Profile.medium)
    resolved_b = _compose(profile_override=Profile.medium)

    config_a = from_resolved_config(resolved_a)
    config_b = from_resolved_config(resolved_b)

    assert config_a == config_b
    assert hash(config_a) == hash(config_b)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def _library_config(*, options_enabled: bool, short_selling_enabled: bool) -> LibraryConfig:
    """Minimal ``LibraryConfig`` for feature-gate tests — only ``feature_flags``
    is consulted by ``classify_feature_gate``; the other fields carry stable
    placeholders."""
    return LibraryConfig(
        effective_limits=MappingProxyType({}),
        escalation_zones=MappingProxyType({}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=(),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


_LONG_CALL = (
    OptionLeg(
        contract_type=ContractType.CALL,
        strike=100.0,
        expiration=date(2026, 6, 19),
        quantity=1,
    ),
)


def _proposed_delta(
    *,
    asset_type: AssetType,
    direction: Direction,
    action: Action = Action.OPEN,
    proposal_id: str = "p1",
    option_legs: tuple[OptionLeg, ...] | None = None,
) -> ProposedDelta:
    """Minimal proposal for feature-gate tests — only ``asset_type`` and
    ``direction`` (and ``action`` for the action-agnosticism test) are read."""
    return ProposedDelta(
        id=proposal_id,
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=direction,
        asset_type=asset_type,
        notional_usd=money(1000.0),
        quantity=10.0,
        option_legs=option_legs,
        action=action,
        existing_position_id=None,
    )


def test_feature_gate_blocks_option_proposal_when_options_disabled() -> None:
    config = _library_config(options_enabled=False, short_selling_enabled=True)
    proposal = _proposed_delta(
        asset_type=AssetType.OPTION,
        direction=Direction.LONG,
        proposal_id="opt1",
        option_legs=_LONG_CALL,
    )

    rejection = classify_feature_gate(proposal, config)

    assert rejection == FeatureDisabledRejection(
        proposal_id="opt1",
        reason="options_disabled",
        disabled_feature="options",
    )


def test_feature_gate_blocks_equity_short_when_shorts_disabled() -> None:
    config = _library_config(options_enabled=True, short_selling_enabled=False)
    proposal = _proposed_delta(
        asset_type=AssetType.EQUITY,
        direction=Direction.SHORT,
        proposal_id="shrt1",
    )

    rejection = classify_feature_gate(proposal, config)

    assert rejection == FeatureDisabledRejection(
        proposal_id="shrt1",
        reason="shorts_disabled",
        disabled_feature="shorts",
    )


def test_feature_gate_options_precedence_for_short_option_with_both_disabled() -> None:
    """A short option under both flags disabled returns ``options_disabled``;
    the more fundamental block (the instrument cannot be opened at all)
    supersedes ``shorts_disabled``."""
    config = _library_config(options_enabled=False, short_selling_enabled=False)
    short_put = (
        OptionLeg(
            contract_type=ContractType.PUT,
            strike=100.0,
            expiration=date(2026, 6, 19),
            quantity=-1,
        ),
    )
    proposal = _proposed_delta(
        asset_type=AssetType.OPTION,
        direction=Direction.SHORT,
        proposal_id="shrt_opt",
        option_legs=short_put,
    )

    rejection = classify_feature_gate(proposal, config)

    assert rejection == FeatureDisabledRejection(
        proposal_id="shrt_opt",
        reason="options_disabled",
        disabled_feature="options",
    )


def test_feature_gate_allows_every_proposal_when_both_flags_enabled() -> None:
    """Under fully-enabled flags, every combination of ``asset_type`` and
    ``direction`` returns ``None``."""
    config = _library_config(options_enabled=True, short_selling_enabled=True)

    for asset_type in AssetType:
        for direction in Direction:
            legs = _LONG_CALL if asset_type in (AssetType.OPTION, AssetType.STRATEGY) else None
            proposal = _proposed_delta(
                asset_type=asset_type,
                direction=direction,
                proposal_id=f"{asset_type.value}_{direction.value}",
                option_legs=legs,
            )
            assert classify_feature_gate(proposal, config) is None


def test_feature_gate_close_action_on_disabled_option_returns_same_rejection_as_open() -> None:
    """The gate is structural (asset_type + direction); ``action`` is ignored.
    A ``CLOSE`` of an existing option under ``options_enabled=False`` returns
    the same rejection as an ``OPEN``. Entry-point policy (story 05) decides
    how ``CLOSE`` interacts with disabled features."""
    config = _library_config(options_enabled=False, short_selling_enabled=True)
    open_proposal = _proposed_delta(
        asset_type=AssetType.OPTION,
        direction=Direction.LONG,
        action=Action.OPEN,
        proposal_id="opt_open",
        option_legs=_LONG_CALL,
    )
    close_proposal = _proposed_delta(
        asset_type=AssetType.OPTION,
        direction=Direction.LONG,
        action=Action.CLOSE,
        proposal_id="opt_close",
        option_legs=_LONG_CALL,
    )

    open_rejection = classify_feature_gate(open_proposal, config)
    close_rejection = classify_feature_gate(close_proposal, config)

    assert open_rejection is not None
    assert close_rejection is not None
    assert open_rejection.reason == close_rejection.reason == "options_disabled"
    assert open_rejection.disabled_feature == close_rejection.disabled_feature == "options"


def test_feature_gate_allows_equity_long_when_shorts_disabled() -> None:
    """An equity long is unaffected by ``short_selling_enabled=False``."""
    config = _library_config(options_enabled=True, short_selling_enabled=False)
    proposal = _proposed_delta(
        asset_type=AssetType.EQUITY,
        direction=Direction.LONG,
        proposal_id="lng",
    )

    assert classify_feature_gate(proposal, config) is None


def test_feature_gate_is_total_over_every_combination_of_inputs() -> None:
    """``classify_feature_gate`` returns ``None`` or a ``FeatureDisabledRejection``
    for every combination of flag pair, asset type, direction, and action — it
    never raises."""
    for options_enabled in (True, False):
        for shorts_enabled in (True, False):
            config = _library_config(
                options_enabled=options_enabled,
                short_selling_enabled=shorts_enabled,
            )
            for asset_type in AssetType:
                for direction in Direction:
                    for action in Action:
                        legs = (
                            _LONG_CALL
                            if asset_type in (AssetType.OPTION, AssetType.STRATEGY)
                            else None
                        )
                        proposal = _proposed_delta(
                            asset_type=asset_type,
                            direction=direction,
                            action=action,
                            proposal_id=(f"{asset_type.value}_{direction.value}_{action.value}"),
                            option_legs=legs,
                        )
                        result = classify_feature_gate(proposal, config)
                        assert result is None or isinstance(result, FeatureDisabledRejection)
