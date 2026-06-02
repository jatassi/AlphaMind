"""Tests for the breach-behavior zone classifier (story 04a)."""

from __future__ import annotations

import pytest

from alphamind.risk_guardrails.breach_behavior import (
    EscalationZones,
    RiskZone,
    classify_zone,
)


def _default_zones() -> EscalationZones:
    """Default 70/85/95 escalation zones from the design doc."""
    return EscalationZones(warning=70, critical=85, hard_block=95)


def _daily_drawdown_zones() -> EscalationZones:
    """Daily drawdown override 60/80/90."""
    return EscalationZones(warning=60, critical=80, hard_block=90)


def _cumulative_drawdown_zones() -> EscalationZones:
    """Cumulative drawdown override 50/70/85."""
    return EscalationZones(warning=50, critical=70, hard_block=85)


def test_zero_consumption_classifies_as_normal() -> None:
    result = classify_zone(
        current_value=0,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.NORMAL


def test_just_under_warning_classifies_as_normal() -> None:
    result = classify_zone(
        current_value=69.9,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.NORMAL


def test_warning_boundary_is_inclusive() -> None:
    """A current value at exactly the warning threshold classifies as WARNING."""
    result = classify_zone(
        current_value=70,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.WARNING


def test_just_below_critical_classifies_as_warning() -> None:
    result = classify_zone(
        current_value=84.9,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.WARNING


def test_critical_boundary_is_inclusive() -> None:
    """A current value at exactly the critical threshold classifies as CRITICAL."""
    result = classify_zone(
        current_value=85,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.CRITICAL


def test_just_below_hard_block_classifies_as_critical() -> None:
    result = classify_zone(
        current_value=94.9,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.CRITICAL


def test_hard_block_boundary_is_inclusive() -> None:
    """A current value at exactly the hard_block threshold classifies as BLOCKED."""
    result = classify_zone(
        current_value=95,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.BLOCKED


def test_consumption_at_limit_classifies_as_blocked() -> None:
    result = classify_zone(
        current_value=100,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.BLOCKED


def test_consumption_beyond_limit_classifies_as_blocked() -> None:
    """A regime-tightening transition that puts a position above the new limit."""
    result = classify_zone(
        current_value=120,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is RiskZone.BLOCKED


@pytest.mark.parametrize(
    ("current_value", "expected_zone"),
    [
        # 1.4 / 2.5 = 56% consumption — below warning at 60%, classifies NORMAL.
        # The story example annotation "just under warning" agrees with NORMAL;
        # the example zone label is a story typo. The formal classifier
        # contract (consumption < zones.warning -> NORMAL) governs.
        (1.4, RiskZone.NORMAL),
        (1.5, RiskZone.WARNING),  # consumption 60% — inclusive boundary
        (2.0, RiskZone.CRITICAL),  # consumption 80%
        (2.25, RiskZone.BLOCKED),  # consumption 90%
    ],
)
def test_daily_drawdown_overrides_match_design(
    current_value: float, expected_zone: RiskZone
) -> None:
    """Daily drawdown rule overrides are 60/80/90 (vs default 70/85/95)."""
    result = classify_zone(
        current_value=current_value,
        limit_value=2.5,
        escalation_zones=_daily_drawdown_zones(),
    )
    assert result is expected_zone


@pytest.mark.parametrize(
    ("current_value", "expected_zone"),
    [
        (4.0, RiskZone.WARNING),  # consumption 50% — inclusive boundary
        (5.6, RiskZone.CRITICAL),  # consumption 70% — inclusive boundary
        (6.8, RiskZone.BLOCKED),  # consumption 85% — inclusive boundary
    ],
)
def test_cumulative_drawdown_overrides_match_design(
    current_value: float, expected_zone: RiskZone
) -> None:
    """Cumulative drawdown rule overrides are 50/70/85 (vs default 70/85/95)."""
    result = classify_zone(
        current_value=current_value,
        limit_value=8.0,
        escalation_zones=_cumulative_drawdown_zones(),
    )
    assert result is expected_zone


@pytest.mark.parametrize(
    ("current_value", "expected_zone"),
    [
        # Each pair tests a boundary and its just-below counterpart for the
        # default 70/85/95 zones over a limit of 100.
        (70.0, RiskZone.WARNING),
        (69.999, RiskZone.NORMAL),
        (85.0, RiskZone.CRITICAL),
        (84.999, RiskZone.WARNING),
        (95.0, RiskZone.BLOCKED),
        (94.999, RiskZone.CRITICAL),
    ],
)
def test_boundary_inclusivity(current_value: float, expected_zone: RiskZone) -> None:
    """Each zone owns its lower bound (closed-open intervals)."""
    result = classify_zone(
        current_value=current_value,
        limit_value=100,
        escalation_zones=_default_zones(),
    )
    assert result is expected_zone


def test_negative_current_value_raises_with_value_in_message() -> None:
    with pytest.raises(ValueError, match=r"-1") as exc_info:
        classify_zone(
            current_value=-1,
            limit_value=100,
            escalation_zones=_default_zones(),
        )
    assert "current_value" in str(exc_info.value)


@pytest.mark.parametrize("limit_value", [0, -5])
def test_non_positive_limit_value_raises_with_value_in_message(limit_value: float) -> None:
    with pytest.raises(ValueError, match=str(limit_value)) as exc_info:
        classify_zone(
            current_value=10,
            limit_value=limit_value,
            escalation_zones=_default_zones(),
        )
    assert "limit_value" in str(exc_info.value)
