"""Tests for the semantic self-test validator (story 06b).

Covers ``validate_semantic_invariants``, ``enumerate_compositions``, and
``SemanticInvariantError`` — the deeper layer that sits on top of parse-time
typing and cross-reference name resolution and asserts the runtime-meaningful
invariants the configuration-management design doc lists under § Validation.

Tests use the shipped YAML tree as a happy-path fixture and synthetic
single-failure fixtures for each invariant. Resolver inputs are loaded once at
module scope so per-test composition stays fast.
"""

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
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
    DigestConfig,
    ExecutionConfig,
    GuardrailsConfig,
    LLMFailureConfig,
    LoadedConfig,
    MainConfig,
    Profile,
    Regime,
    ResolvedConfig,
    SchedulerConfig,
    VenueConfig,
)
from alphamind.config.validation.semantic import (
    SemanticInvariantError,
    enumerate_compositions,
    validate_semantic_invariants,
)

REPO_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = REPO_ROOT / "config"

TODAY = date(2026, 4, 27)


def _read(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((CONFIG_DIR / name).read_text()))


# Shipped tree once — happy-path tests reuse these.
_MAIN = MainConfig.model_validate(_read("main.yaml"))
_SCHEDULER = SchedulerConfig.model_validate(_read("scheduler.yaml"))
_VENUE = VenueConfig.model_validate(_read("venue.yaml"))
_EXECUTION = ExecutionConfig.model_validate(_read("execution.yaml"))
_GUARDRAILS = GuardrailsConfig.model_validate(_read("guardrails.yaml"))
_LLM_FAILURE = LLMFailureConfig.model_validate(_read("llm_failure.yaml"))
_DIGEST = DigestConfig.model_validate(_read("digest.yaml"))
_ASSETS = AssetsConfig.model_validate(_read("assets.yaml"))
_AGENTS = AgentsConfig.model_validate(_read("agents.yaml"))
_PROFILES = load_profiles(CONFIG_DIR)
_REGIMES = load_regimes(CONFIG_DIR)
_MODES = load_modes(CONFIG_DIR)
_OVERLAYS = load_overlays(CONFIG_DIR)
_RUN_TYPES = load_run_types(CONFIG_DIR)


def _shipped_loaded() -> LoadedConfig:
    return LoadedConfig(
        main=_MAIN,
        scheduler=_SCHEDULER,
        venue=_VENUE,
        execution=_EXECUTION,
        guardrails=_GUARDRAILS,
        llm_failure=_LLM_FAILURE,
        digest=_DIGEST,
        assets=_ASSETS,
        agents=_AGENTS,
        profiles=dict(_PROFILES),
        regimes=dict(_REGIMES),
        modes=dict(_MODES),
        overlays=dict(_OVERLAYS),
        run_types=dict(_RUN_TYPES),
    )


def _replace_rule_values(resolved: ResolvedConfig, rule_values: dict[str, float]) -> ResolvedConfig:
    """Clone ``resolved`` with ``rule_values`` swapped — ResolvedConfig is frozen."""
    return replace(resolved, rule_values=MappingProxyType(rule_values))


# ---------------------------------------------------------------------------
# Happy path — shipped YAML composes cleanly and passes every invariant.
# ---------------------------------------------------------------------------


def test_shipped_yaml_passes_validate_semantic_invariants() -> None:
    loaded = _shipped_loaded()
    composed = enumerate_compositions(loaded)
    validate_semantic_invariants(
        guardrails=_GUARDRAILS,
        assets=_ASSETS,
        digest=_DIGEST,
        profiles=dict(_PROFILES),
        regimes=dict(_REGIMES),
        composed_configs=composed,
        today=TODAY,
    )


# ---------------------------------------------------------------------------
# Invariant 1 — cumulative-drawdown progressive tiers monotonic.
# ---------------------------------------------------------------------------


def _guardrails_with_progressive_tiers(triggers: list[float]) -> GuardrailsConfig:
    """Clone shipped guardrails with the cumulative_drawdown_pct tiers replaced.

    The shipped tiers are monotonic; tests pass non-monotonic triggers to
    construct a single-failure fixture. Each tier carries a benign payload.
    """
    payload = _read("guardrails.yaml")
    for rule in payload["rules"]:
        if rule["id"] == "cumulative_drawdown_pct":
            rule["progressive_tiers"] = [
                {"trigger_pct": t, "max_position_size_pct": 3, "max_gross_pct": 80}
                for t in triggers
            ]
    return GuardrailsConfig.model_validate(payload)


def test_non_monotonic_progressive_tiers_raise() -> None:
    # The guardrails Pydantic model enforces monotonicity at parse time, so
    # we bypass the parse layer to exercise the semantic validator's defensive
    # re-check: build the model from a payload that the parse-time validator
    # would catch, then mutate the resulting object via model_copy.
    triggers = [8.0, 12.0, 10.0]  # second-to-third decreases
    monotonic = _guardrails_with_progressive_tiers([8.0, 10.0, 12.0])
    cumulative_rule = next(r for r in monotonic.rules if r.id == "cumulative_drawdown_pct")
    assert cumulative_rule.progressive_tiers is not None
    new_tiers = [
        tier.model_copy(update={"trigger_pct": triggers[i]})
        for i, tier in enumerate(cumulative_rule.progressive_tiers)
    ]
    new_rules = [
        r.model_copy(update={"progressive_tiers": new_tiers})
        if r.id == "cumulative_drawdown_pct"
        else r
        for r in monotonic.rules
    ]
    broken = monotonic.model_copy(update={"rules": new_rules})

    with pytest.raises(SemanticInvariantError, match="progressive_tiers"):
        validate_semantic_invariants(
            guardrails=broken,
            assets=_ASSETS,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 2 — no regime multiplier drives a rule limit to zero or negative.
# ---------------------------------------------------------------------------


def test_zero_regime_multiplier_for_a_profile_rule_raises() -> None:
    # The story's invariant: every (profile, regime) pair must yield strictly
    # positive resolved limits. Model validators reject zero multipliers at
    # parse time, so we mutate the loaded RegimeConfig via model_copy and feed
    # the broken regime into the composition.
    rule_in_every_profile = "position_max_size_pct"
    broken_normal = _REGIMES[Regime.normal].model_copy(
        update={"multipliers": {**_REGIMES[Regime.normal].multipliers, rule_in_every_profile: 0.0}}
    )
    regimes = dict(_REGIMES)
    regimes[Regime.normal] = broken_normal

    with pytest.raises(SemanticInvariantError, match=rule_in_every_profile):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=_ASSETS,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=regimes,
            composed_configs={},
            today=TODAY,
        )


def test_resolved_rule_value_of_zero_raises() -> None:
    # The composed-config arm of invariant 2: even if every regime multiplier
    # is positive, a synthetic ResolvedConfig with a zero limit must trip the
    # validator. Mutates one rule value to zero and feeds the snapshot in.
    loaded = _shipped_loaded()
    composed = enumerate_compositions(loaded)
    sample_key = next(iter(composed))
    sample = composed[sample_key]
    broken_rule_values = dict(sample.rule_values)
    broken_rule_values[next(iter(broken_rule_values))] = 0.0
    broken_resolved = _replace_rule_values(sample, broken_rule_values)

    with pytest.raises(SemanticInvariantError, match="must be > 0"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=_ASSETS,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={sample_key: broken_resolved},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 4 — capital ranges non-overlapping.
# ---------------------------------------------------------------------------


def test_overlapping_capital_ranges_raise() -> None:
    micro = _PROFILES[Profile.micro]
    small = _PROFILES[Profile.small]
    overlapping_micro = micro.model_copy(update={"capital_range_usd": (1000, 10000)})
    overlapping_small = small.model_copy(update={"capital_range_usd": (5000, 15000)})
    profiles = dict(_PROFILES)
    profiles[Profile.micro] = overlapping_micro
    profiles[Profile.small] = overlapping_small

    with pytest.raises(SemanticInvariantError, match="overlaps"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=_ASSETS,
            digest=_DIGEST,
            profiles=profiles,
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 5 — ticker uniqueness across sectors and benchmarks.
# ---------------------------------------------------------------------------


def test_duplicate_ticker_across_sectors_raises() -> None:
    payload = _read("assets.yaml")
    # AAPL is in tech; insert into financials too to force a duplicate.
    payload["sectors"]["financials"].append("AAPL")
    broken_assets = AssetsConfig.model_validate(payload)

    with pytest.raises(SemanticInvariantError, match="AAPL"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=broken_assets,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


def test_duplicate_ticker_between_sector_and_benchmark_raises() -> None:
    payload = _read("assets.yaml")
    # SPY is a benchmark; place it inside a sector to force a duplicate.
    payload["sectors"]["tech"].append("SPY")
    broken_assets = AssetsConfig.model_validate(payload)

    with pytest.raises(SemanticInvariantError, match="SPY"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=broken_assets,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 7 — sector listed in active_sectors must have at least one ticker.
# ---------------------------------------------------------------------------


def test_empty_sector_referenced_by_profile_raises() -> None:
    payload = _read("assets.yaml")
    # Wipe the tech sector's tickers; every shipped profile lists tech in its
    # active_sectors so the failure fires regardless of profile.
    payload["sectors"]["tech"] = []
    broken_assets = AssetsConfig.model_validate(payload)

    with pytest.raises(SemanticInvariantError, match="empty ticker list"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=broken_assets,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 8 — last_full_validation must not be in the future.
# ---------------------------------------------------------------------------


def test_future_last_full_validation_raises() -> None:
    broken_assets = _ASSETS.model_copy(update={"last_full_validation": date(2099, 1, 1)})

    with pytest.raises(SemanticInvariantError, match="2099-01-01"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=broken_assets,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Invariant 9 — feature-flag closure (no options/shorts residue).
# ---------------------------------------------------------------------------


def test_micro_profile_resolved_configs_drop_all_options_rules() -> None:
    """Positive assertion: the shipped micro profile has no options residue."""
    loaded = _shipped_loaded()
    composed = enumerate_compositions(loaded)
    micro_compositions = {key: cfg for key, cfg in composed.items() if key[0] is Profile.micro}
    assert micro_compositions, "micro must appear in the composition matrix"
    for resolved in micro_compositions.values():
        for rule_id in resolved.rule_values:
            assert not rule_id.startswith("options_")
            assert not rule_id.startswith("portfolio_theta_")
            assert not rule_id.startswith("portfolio_vega_")
            assert rule_id != "position_max_loss_options_pct"


def test_options_residue_in_micro_profile_raises() -> None:
    """A synthetic resolver leaves a zero-valued options rule in micro's config."""
    loaded = _shipped_loaded()
    composed = enumerate_compositions(loaded)
    micro_key = next(key for key in composed if key[0] is Profile.micro)
    sample = composed[micro_key]
    residue = dict(sample.rule_values)
    residue["options_delta_pct"] = 0.5  # leaked through despite options_enabled=false
    leaky_resolved = _replace_rule_values(sample, residue)

    with pytest.raises(SemanticInvariantError, match="options_enabled"):
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=_ASSETS,
            digest=_DIGEST,
            profiles=dict(_PROFILES),
            regimes=dict(_REGIMES),
            composed_configs={micro_key: leaky_resolved},
            today=TODAY,
        )


# ---------------------------------------------------------------------------
# Composition matrix size.
# ---------------------------------------------------------------------------


def test_enumerate_compositions_produces_full_matrix() -> None:
    loaded = _shipped_loaded()
    composed = enumerate_compositions(loaded)
    # 4 profiles x 4 regimes x 2 modes x 4 overlay subsets x 7 run-types = 896
    # The 7th run-type is ``emergency`` — non-cron, dispatched by the
    # pipeline scheduler's emergency receiver (story 04b).
    assert len(composed) == 4 * 4 * 2 * 4 * 7


# ---------------------------------------------------------------------------
# Failure aggregation: multiple violations produce one error listing each.
# ---------------------------------------------------------------------------


def test_validator_aggregates_failures() -> None:
    # Combine three failures: future last_full_validation, overlapping capital
    # ranges (micro vs. small), and a duplicate ticker. The validator must
    # raise once with all three messages present.
    broken_assets_payload = _read("assets.yaml")
    broken_assets_payload["sectors"]["financials"].append("AAPL")
    broken_assets = AssetsConfig.model_validate(broken_assets_payload).model_copy(
        update={"last_full_validation": date(2099, 1, 1)}
    )
    profiles = dict(_PROFILES)
    profiles[Profile.micro] = _PROFILES[Profile.micro].model_copy(
        update={"capital_range_usd": (1000, 10000)}
    )
    profiles[Profile.small] = _PROFILES[Profile.small].model_copy(
        update={"capital_range_usd": (5000, 15000)}
    )

    with pytest.raises(SemanticInvariantError) as exc_info:
        validate_semantic_invariants(
            guardrails=_GUARDRAILS,
            assets=broken_assets,
            digest=_DIGEST,
            profiles=profiles,
            regimes=dict(_REGIMES),
            composed_configs={},
            today=TODAY,
        )

    message = str(exc_info.value)
    assert "AAPL" in message
    assert "overlaps" in message
    assert "2099-01-01" in message
