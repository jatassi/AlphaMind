"""Shared test helpers for the guardrail-enforcement work tree.

Centralizes the baseline parameter-set builder, entry lookup, determinism
guard, and the read-once tier-snapshot constants used by both
``test_composition.py`` (story 01) and ``test_orchestrator.py`` (story 02).

The cumulative-drawdown progressive-tier loader itself lives in
``alphamind.config.guardrails_helpers``; this module just re-exposes its
read-once tuple under the existing ``TIERS`` / ``NON_HALT_TIERS`` /
``FULL_HALT_TIER`` names so the test surface keeps the same imports.
"""

from __future__ import annotations

from collections.abc import Callable

from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.breach_behavior import RegimeLabel
from tests.risk_guardrails.breach_behavior.fixtures import make_active_risk_parameters

_REPETITIONS = 5
"""Determinism guard repetition count."""


# Read-once module-level snapshot — every consumer references this rather
# than re-reading and re-validating the shipped YAML on each call.
TIERS = load_cumulative_drawdown_progressive_tiers()
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
