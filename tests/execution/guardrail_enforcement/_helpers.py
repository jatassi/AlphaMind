"""Shared test helpers for the guardrail-enforcement work tree.

Centralizes the progressive-tier loader, baseline parameter-set builder,
entry lookup, and determinism guard used by both ``test_composition.py``
(story 01) and ``test_orchestrator.py`` (story 02).
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any, cast

import yaml

from alphamind.config.models.guardrails import GuardrailsConfig, ProgressiveTier
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.breach_behavior import RegimeLabel
from tests.risk_guardrails.breach_behavior.fixtures import make_active_risk_parameters

_REPETITIONS = 5
"""Determinism guard repetition count."""

_GUARDRAILS_YAML = pathlib.Path("config/guardrails.yaml")
_CUMULATIVE_DRAWDOWN_RULE_ID = "cumulative_drawdown_pct"


def load_progressive_tiers() -> tuple[ProgressiveTier, ...]:
    """Load the cumulative-drawdown progressive tiers from the shipped guardrails config."""
    raw = cast(dict[str, Any], yaml.safe_load(_GUARDRAILS_YAML.read_text()))
    config = GuardrailsConfig.model_validate(raw)
    rule = next(r for r in config.rules if r.id == _CUMULATIVE_DRAWDOWN_RULE_ID)
    assert rule.progressive_tiers is not None
    return tuple(rule.progressive_tiers)


# Read-once module-level snapshot — every consumer references this rather
# than re-reading and re-validating the shipped YAML on each call.
TIERS = load_progressive_tiers()
NON_HALT_TIERS = tuple(t for t in TIERS if not t.full_halt)
FULL_HALT_TIER = next(t for t in TIERS if t.full_halt)


def assert_deterministic[T](call: Callable[[], T]) -> T:
    """Invoke ``call`` ``_REPETITIONS`` times, assert equal outputs, return one."""
    first = call()
    for _ in range(_REPETITIONS - 1):
        assert call() == first
    return first


def baseline_normal_parameters() -> ActiveRiskParameterSet:
    """Construct a normal-regime parameter set with the rules tier overrides target."""
    return make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={
            "position_max_size_pct": 5.0,
            "gross_exposure_pct": 120.0,
            "daily_drawdown_pct": 2.5,
        },
    )


def entry_by_id(parameter_set: ActiveRiskParameterSet, rule_id: str) -> ActiveRiskParameterEntry:
    """Return the entry whose ``rule_id`` matches; assert if absent."""
    for entry in parameter_set.entries:
        if entry.rule_id == rule_id:
            return entry
    raise AssertionError(f"missing entry {rule_id!r}")
