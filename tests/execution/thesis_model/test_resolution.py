"""Tests for classify_thesis_resolution — ALP-334."""

from __future__ import annotations

import pytest

from alphamind.portfolio_state.events.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisResolutionCategory,
)
from alphamind.portfolio_state.records.thesis_resolution import classify_thesis_resolution

# ---------------------------------------------------------------------------
# Convenience aliases
# ---------------------------------------------------------------------------

VALIDATED = ThesisComponentOutcome.VALIDATED
WRONG = ThesisComponentOutcome.WRONG
INCONCLUSIVE = ThesisComponentOutcome.INCONCLUSIVE

V = ThesisResolutionCategory.VALIDATED
PBW = ThesisResolutionCategory.PROFITABLE_BUT_WRONG
ISC = ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY
IWE = ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT
CNE = ThesisResolutionCategory.CANCELLED_NEVER_ENTERED

STOP = PositionExitMethod.STOP_TRIGGERED
TARGET = PositionExitMethod.TARGET_REACHED
PM = PositionExitMethod.PM_DECISION
TIME = PositionExitMethod.TIME_EXPIRED
MARGIN = PositionExitMethod.MARGIN_LIQUIDATION
FORCED = PositionExitMethod.FORCED_BUY_IN
CASH_MERGER = PositionExitMethod.CORPORATE_ACTION_CASH_MERGER
OPTION_EXPIRY = PositionExitMethod.OPTION_EXPIRY
OPTION_ASSIGNMENT = PositionExitMethod.OPTION_ASSIGNMENT
OPTION_EXERCISE = PositionExitMethod.OPTION_EXERCISE

MECHANICAL_EXITS = [
    STOP,
    TARGET,
    TIME,
    MARGIN,
    FORCED,
    CASH_MERGER,
    OPTION_EXPIRY,
    OPTION_ASSIGNMENT,
    OPTION_EXERCISE,
]

# ---------------------------------------------------------------------------
# Tracer bullet: empty tuple raises ValueError
# ---------------------------------------------------------------------------


def test_empty_outcomes_raises_value_error() -> None:
    with pytest.raises(ValueError, match="empty"):
        classify_thesis_resolution((), 100.0, STOP)


# ---------------------------------------------------------------------------
# VALIDATED path: most components VALIDATED AND P/L > 0 AND exit via TARGET_REACHED
# ---------------------------------------------------------------------------


def test_all_validated_positive_pnl_target_reached_returns_validated() -> None:
    result = classify_thesis_resolution((VALIDATED, VALIDATED, VALIDATED), 500.0, TARGET)
    assert result == V


def test_majority_validated_positive_pnl_target_reached_returns_validated() -> None:
    # The positive case (ALP-920): majority-VALIDATED, profitable, and the position
    # actually hit the target → VALIDATED. This is the only profitable path to V.
    result = classify_thesis_resolution((VALIDATED, VALIDATED, WRONG), 10.0, TARGET)
    assert result == V


@pytest.mark.parametrize(
    "exit_method",
    [em for em in PositionExitMethod if em is not TARGET],
)
def test_majority_validated_positive_pnl_non_target_exit_returns_profitable_but_wrong(
    exit_method: PositionExitMethod,
) -> None:
    # ALP-920 gate: a profitable, majority-VALIDATED thesis forced out by any
    # non-TARGET_REACHED exit did not hit the target → PROFITABLE_BUT_WRONG.
    assert classify_thesis_resolution((VALIDATED,), 1.0, exit_method) == PBW


def test_majority_validated_positive_pnl_stop_triggered_returns_profitable_but_wrong() -> None:
    # Reported scenario (ALP-920): profitable, forced out by a stop, two fired
    # invalidations (VALIDATED) + a missed target (WRONG). Majority-VALIDATED but
    # the position did not hit the target → PROFITABLE_BUT_WRONG, not VALIDATED.
    result = classify_thesis_resolution((VALIDATED, VALIDATED, WRONG), 100.0, STOP)
    assert result == PBW


def test_majority_validated_positive_pnl_time_expired_returns_profitable_but_wrong() -> None:
    # TIME_EXPIRED variant of the reported scenario — a time-limit forced exit is
    # equally a non-target exit, so a profitable majority-VALIDATED thesis is
    # PROFITABLE_BUT_WRONG.
    result = classify_thesis_resolution((VALIDATED, VALIDATED, WRONG), 100.0, TIME)
    assert result == PBW


def test_majority_validated_positive_pnl_pm_decision_returns_profitable_but_wrong() -> None:
    # Deliberate behavior change beyond the reported stop-out (ALP-920): a
    # profitable majority-VALIDATED thesis the PM closed by hand did not hit the
    # target → PROFITABLE_BUT_WRONG.
    result = classify_thesis_resolution((VALIDATED, VALIDATED, WRONG), 200.0, PM)
    assert result == PBW


# ---------------------------------------------------------------------------
# PROFITABLE_BUT_WRONG path: not most-validated but P/L > 0
# ---------------------------------------------------------------------------


def test_all_wrong_positive_pnl_returns_profitable_but_wrong() -> None:
    result = classify_thesis_resolution((WRONG, WRONG, WRONG), 50.0, STOP)
    assert result == PBW


def test_all_inconclusive_positive_pnl_returns_profitable_but_wrong() -> None:
    result = classify_thesis_resolution((INCONCLUSIVE, INCONCLUSIVE), 10.0, STOP)
    assert result == PBW


def test_mixed_one_each_positive_pnl_returns_profitable_but_wrong() -> None:
    # 1 VALIDATED, 1 WRONG, 1 INCONCLUSIVE → tie (1==1) → not most-validated → PBW
    result = classify_thesis_resolution((VALIDATED, WRONG, INCONCLUSIVE), 200.0, PM)
    assert result == PBW


def test_tie_between_validated_and_wrong_positive_pnl_returns_profitable_but_wrong() -> None:
    # 2 VALIDATED vs 2 WRONG → tie → PBW
    result = classify_thesis_resolution((VALIDATED, VALIDATED, WRONG, WRONG), 1.0, STOP)
    assert result == PBW


# ---------------------------------------------------------------------------
# INVALIDATED_STOPPED_CORRECTLY path: P/L <= 0 + mechanical exit
# ---------------------------------------------------------------------------


def test_all_wrong_zero_pnl_stop_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG, WRONG), 0.0, STOP)
    assert result == ISC


def test_all_wrong_negative_pnl_time_expired_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG,), -250.0, TIME)
    assert result == ISC


def test_all_wrong_negative_pnl_target_reached_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG,), -10.0, TARGET)
    assert result == ISC


def test_all_wrong_negative_pnl_margin_liquidation_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG,), -100.0, MARGIN)
    assert result == ISC


def test_all_wrong_negative_pnl_forced_buy_in_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG,), -50.0, FORCED)
    assert result == ISC


def test_all_wrong_negative_pnl_cash_merger_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((WRONG,), -30.0, CASH_MERGER)
    assert result == ISC


def test_all_inconclusive_negative_pnl_stop_returns_stopped_correctly() -> None:
    result = classify_thesis_resolution((INCONCLUSIVE, INCONCLUSIVE), -75.0, STOP)
    assert result == ISC


def test_wrong_nonpositive_pnl_option_expiry_returns_stopped_correctly() -> None:
    """ALP-918 AC2: an option-lifecycle close is categorically mechanical — an
    OTM expiry with a wrong component and non-positive P/L is stopped-correctly,
    not held-too-long (the new members joined ``_MECHANICAL_EXIT_METHODS``)."""
    assert classify_thesis_resolution((WRONG,), -100.0, OPTION_EXPIRY) == ISC
    assert classify_thesis_resolution((WRONG,), 0.0, OPTION_EXPIRY) == ISC


# ---------------------------------------------------------------------------
# INVALIDATED_WRONG_ON_EXIT path: P/L <= 0 + pm-decision
# ---------------------------------------------------------------------------


def test_all_wrong_negative_pnl_pm_decision_returns_wrong_on_exit() -> None:
    result = classify_thesis_resolution((WRONG, WRONG), -100.0, PM)
    assert result == IWE


def test_all_wrong_zero_pnl_pm_decision_returns_wrong_on_exit() -> None:
    result = classify_thesis_resolution((WRONG,), 0.0, PM)
    assert result == IWE


def test_all_inconclusive_negative_pnl_pm_decision_returns_wrong_on_exit() -> None:
    result = classify_thesis_resolution((INCONCLUSIVE,), -10.0, PM)
    assert result == IWE


# ---------------------------------------------------------------------------
# Never returns CANCELLED_NEVER_ENTERED
# ---------------------------------------------------------------------------


def test_never_returns_cancelled_never_entered() -> None:
    all_outcomes = list(ThesisComponentOutcome)
    all_exits = list(PositionExitMethod)
    pnls = [-100.0, 0.0, 100.0]
    for outcome in all_outcomes:
        for em in all_exits:
            for pnl in pnls:
                result = classify_thesis_resolution((outcome,), pnl, em)
                assert result != CNE, (
                    f"Should never produce CANCELLED_NEVER_ENTERED "
                    f"(outcome={outcome}, exit={em}, pnl={pnl})"
                )


# ---------------------------------------------------------------------------
# Parametrized sweep: all exit methods x outcome distributions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_method", list(PositionExitMethod))
@pytest.mark.parametrize(
    "outcomes",
    [
        # Not majority-VALIDATED, so the TARGET_REACHED gate never engages — these
        # are PROFITABLE_BUT_WRONG for every exit method when P/L > 0.
        (WRONG, WRONG),
        (INCONCLUSIVE,),
    ],
)
def test_positive_pnl_non_majority_validated_always_profitable_but_wrong(
    exit_method: PositionExitMethod,
    outcomes: tuple[ThesisComponentOutcome, ...],
) -> None:
    """When P/L > 0 and components are not majority-VALIDATED, exit method is
    irrelevant — the result is PROFITABLE_BUT_WRONG for every exit (ALP-920 only
    gates the majority-VALIDATED case on TARGET_REACHED)."""
    assert classify_thesis_resolution(outcomes, 100.0, exit_method) == PBW


@pytest.mark.parametrize("exit_method", list(PositionExitMethod))
def test_positive_pnl_majority_validated_only_target_reached_is_validated(
    exit_method: PositionExitMethod,
) -> None:
    """When P/L > 0 and components are majority-VALIDATED, exit method is the
    deciding factor: TARGET_REACHED → VALIDATED, every other exit → PROFITABLE_BUT_WRONG."""
    expected = V if exit_method is TARGET else PBW
    assert classify_thesis_resolution((VALIDATED, VALIDATED), 100.0, exit_method) == expected


@pytest.mark.parametrize("exit_method", MECHANICAL_EXITS)
@pytest.mark.parametrize(
    "outcomes",
    [
        (WRONG,),
        (WRONG, WRONG, WRONG),
        (INCONCLUSIVE,),
        (VALIDATED, WRONG),  # tie → not most-validated
    ],
)
def test_non_positive_pnl_mechanical_exit_returns_stopped_correctly(
    exit_method: PositionExitMethod,
    outcomes: tuple[ThesisComponentOutcome, ...],
) -> None:
    """All mechanical exits → INVALIDATED_STOPPED_CORRECTLY when P/L <= 0 and not VALIDATED."""
    assert classify_thesis_resolution(outcomes, -50.0, exit_method) == ISC


def test_pm_decision_exit_all_wrong_negative_returns_wrong_on_exit() -> None:
    """PM-judgment exit → INVALIDATED_WRONG_ON_EXIT when P/L <= 0."""
    assert classify_thesis_resolution((WRONG,), -1.0, PM) == IWE


@pytest.mark.parametrize(
    "outcomes,exit_method,pnl,expected",
    [
        # Validate all four categories are reachable. VALIDATED requires both
        # majority-VALIDATED and a TARGET_REACHED exit (ALP-920).
        ((VALIDATED,), TARGET, 1.0, V),
        # Majority-VALIDATED but a non-target exit → PROFITABLE_BUT_WRONG
        ((VALIDATED,), STOP, 1.0, PBW),
        ((WRONG,), STOP, 1.0, PBW),
        ((WRONG,), STOP, -1.0, ISC),
        ((WRONG,), PM, -1.0, IWE),
        # Edge: zero P/L is non-positive
        ((WRONG,), STOP, 0.0, ISC),
        ((WRONG,), PM, 0.0, IWE),
        # INCONCLUSIVE-only with negative P/L, mechanical vs pm
        ((INCONCLUSIVE,), TIME, -1.0, ISC),
        ((INCONCLUSIVE,), PM, -1.0, IWE),
        # Majority VALIDATED, negative P/L → spec says VALIDATED requires P/L > 0;
        # majority VALIDATED + negative P/L falls to the INVALIDATED branch per exit method)
        ((VALIDATED, VALIDATED, WRONG), STOP, -10.0, ISC),
        ((VALIDATED, VALIDATED, WRONG), PM, -10.0, IWE),
        # Majority VALIDATED, positive P/L, TARGET_REACHED → VALIDATED (positive case)
        ((VALIDATED, VALIDATED, WRONG), TARGET, 10.0, V),
        # Majority VALIDATED, positive P/L, but a non-target exit → PROFITABLE_BUT_WRONG
        ((VALIDATED, VALIDATED, WRONG), PM, 10.0, PBW),
    ],
)
def test_truth_table(
    outcomes: tuple[ThesisComponentOutcome, ...],
    exit_method: PositionExitMethod,
    pnl: float,
    expected: ThesisResolutionCategory,
) -> None:
    """Full truth-table sweep covering all four categories and edge cases."""
    assert classify_thesis_resolution(outcomes, pnl, exit_method) == expected
