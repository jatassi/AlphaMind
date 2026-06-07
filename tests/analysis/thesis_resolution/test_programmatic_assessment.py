"""Tests for assess_component_programmatically — ALP-897."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import ThesisId
from alphamind.analysis.thesis_resolution.programmatic import (
    assess_component_programmatically,
)
from alphamind.portfolio_state.events.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

VALIDATED = ThesisComponentOutcome.VALIDATED
WRONG = ThesisComponentOutcome.WRONG
INCONCLUSIVE = ThesisComponentOutcome.INCONCLUSIVE

TARGET = PositionExitMethod.TARGET_REACHED
STOP = PositionExitMethod.STOP_TRIGGERED
TIME = PositionExitMethod.TIME_EXPIRED
PM = PositionExitMethod.PM_DECISION
MARGIN = PositionExitMethod.MARGIN_LIQUIDATION
FORCED = PositionExitMethod.FORCED_BUY_IN
CASH_MERGER = PositionExitMethod.CORPORATE_ACTION_CASH_MERGER
OPTION_EXPIRY = PositionExitMethod.OPTION_EXPIRY


def _make_component(
    component_type: ThesisComponentType,
    component_id: str = "c1",
) -> ThesisComponent:
    return ThesisComponent(
        component_id=component_id,
        thesis_id=ThesisId("t1"),
        component_type=component_type,
        linked_bracket_leg_type=None,
        instrument_reference="NVDA",
        narrative="Test narrative.",
        key_assumptions=(),
        generation_timestamp=NOW,
        resolution_outcome=None,
        resolution_notes=None,
    )


# ---------------------------------------------------------------------------
# TARGET_RATIONALE: target hit → VALIDATED
# ---------------------------------------------------------------------------


def test_target_rationale_target_reached_returns_validated() -> None:
    component = _make_component(ThesisComponentType.TARGET_RATIONALE)
    result = assess_component_programmatically(component, exit_method=TARGET)
    assert result == VALIDATED


# ---------------------------------------------------------------------------
# TARGET_RATIONALE: stop triggered → WRONG (thesis wrong: target was never hit)
# ---------------------------------------------------------------------------


def test_target_rationale_stop_triggered_returns_wrong() -> None:
    component = _make_component(ThesisComponentType.TARGET_RATIONALE)
    result = assess_component_programmatically(component, exit_method=STOP)
    assert result == WRONG


def test_target_rationale_time_expired_returns_wrong() -> None:
    component = _make_component(ThesisComponentType.TARGET_RATIONALE)
    result = assess_component_programmatically(component, exit_method=TIME)
    assert result == WRONG


# ---------------------------------------------------------------------------
# TARGET_RATIONALE: external/judgment exits → INCONCLUSIVE
#
# OPTION_EXPIRY is direction-dependent (a long option expiring worthless missed
# its target → WRONG; a short option capturing premium → VALIDATED) and the
# assessor sees only component-type + exit-method, not direction — so it must
# stay out of _TARGET_MISS_EXITS and route to the LLM evaluator (ALP-918 AC3,
# mirroring CORPORATE_ACTION_CASH_MERGER). The assessor itself is unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_method",
    [PM, MARGIN, FORCED, CASH_MERGER, OPTION_EXPIRY],
)
def test_target_rationale_external_exit_returns_inconclusive(
    exit_method: PositionExitMethod,
) -> None:
    component = _make_component(ThesisComponentType.TARGET_RATIONALE)
    result = assess_component_programmatically(component, exit_method=exit_method)
    assert result == INCONCLUSIVE


# ---------------------------------------------------------------------------
# INVALIDATION_RATIONALE: stop/time triggers → VALIDATED (the invalidation
# rationale correctly identified the exit — "stopped out correctly", a
# positive process outcome per thesis-model.md § Resolution).
# ---------------------------------------------------------------------------


def test_invalidation_rationale_stop_triggered_returns_validated() -> None:
    component = _make_component(ThesisComponentType.INVALIDATION_RATIONALE)
    result = assess_component_programmatically(component, exit_method=STOP)
    assert result == VALIDATED


def test_invalidation_rationale_time_expired_returns_validated() -> None:
    component = _make_component(ThesisComponentType.INVALIDATION_RATIONALE)
    result = assess_component_programmatically(component, exit_method=TIME)
    assert result == VALIDATED


# ---------------------------------------------------------------------------
# INVALIDATION_RATIONALE: target / PM / other exits → INCONCLUSIVE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exit_method",
    [TARGET, PM, MARGIN, FORCED, CASH_MERGER],
)
def test_invalidation_rationale_non_stop_exit_returns_inconclusive(
    exit_method: PositionExitMethod,
) -> None:
    component = _make_component(ThesisComponentType.INVALIDATION_RATIONALE)
    result = assess_component_programmatically(component, exit_method=exit_method)
    assert result == INCONCLUSIVE


# ---------------------------------------------------------------------------
# ENTRY_RATIONALE: always INCONCLUSIVE (qualitative — LLM evaluator handles it)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_method", list(PositionExitMethod))
def test_entry_rationale_always_inconclusive(exit_method: PositionExitMethod) -> None:
    component = _make_component(ThesisComponentType.ENTRY_RATIONALE)
    result = assess_component_programmatically(component, exit_method=exit_method)
    assert result == INCONCLUSIVE
