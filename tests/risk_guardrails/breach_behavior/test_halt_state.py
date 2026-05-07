"""Tests for the halt-state computation primitive (story 05a).

Exercises ``compute_halt_state`` against the trigger semantics documented in
``docs/design/06-risk-guardrails/breach-behavior.md`` § *Drawdown halt mode*
and the ``HaltState`` post-validator from
``src/alphamind/risk_guardrails/breach_behavior/types.py`` (story 03).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
    DrawdownState,
)
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownTier,
    HaltState,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
    compute_halt_state,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_drawdown_state(
    *,
    intraday_drawdown_pct: float,
    cumulative_tier: DrawdownTier | None = None,
    current_drawdown_pct: float = 0.0,
) -> DrawdownState:
    """Build a ``DrawdownState`` for halt-state tests.

    Only ``intraday_drawdown_pct`` and ``cumulative_tier`` participate in halt
    detection; the remaining fields carry filler values that satisfy the
    record's validators.
    """
    return DrawdownState(
        current_drawdown_pct=current_drawdown_pct,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=intraday_drawdown_pct,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct={},
    )


def _build_active_risk_parameters(
    *,
    daily_drawdown_limit_pct: float | None = 2.5,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
) -> ActiveRiskParameterSet:
    """Build an ``ActiveRiskParameterSet`` carrying a ``daily_drawdown_pct`` entry.

    Pass ``daily_drawdown_limit_pct=None`` to omit the entry entirely (used by
    the missing-entry validation test).
    """
    entries: tuple[ActiveRiskParameterEntry, ...] = ()
    if daily_drawdown_limit_pct is not None:
        entries = (
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="Daily drawdown limit (% of opening equity)",
                value=daily_drawdown_limit_pct,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=2.5,
            ),
        )
    return ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


# ---------------------------------------------------------------------------
# Daily-halt-only
# ---------------------------------------------------------------------------


def test_daily_at_exactly_limit_triggers_halt() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=2.5)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result == HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.5,
        daily_drawdown_limit_pct=2.5,
    )


def test_daily_above_limit_triggers_halt() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=3.2)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.cumulative_full_halt_active is False
    assert result.daily_drawdown_pct == 3.2
    assert result.daily_drawdown_limit_pct == 2.5


def test_daily_just_below_limit_returns_none() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=2.4)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


# ---------------------------------------------------------------------------
# Cumulative-full-halt-only
# ---------------------------------------------------------------------------


def test_cumulative_full_halt_alone_triggers_halt() -> None:
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=0.8,
        cumulative_tier=DrawdownTier.FULL_HALT,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result == HaltState(
        daily_halt_active=False,
        cumulative_full_halt_active=True,
        daily_drawdown_pct=0.8,
        daily_drawdown_limit_pct=2.5,
    )


def test_constrained_tier_alone_returns_none() -> None:
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=0.5,
        cumulative_tier=DrawdownTier.CONSTRAINED,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


def test_heavily_constrained_tier_alone_returns_none() -> None:
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=0.5,
        cumulative_tier=DrawdownTier.HEAVILY_CONSTRAINED,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


def test_cumulative_tier_none_with_daily_below_limit_returns_none() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=1.0, cumulative_tier=None)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


# ---------------------------------------------------------------------------
# Both halts active
# ---------------------------------------------------------------------------


def test_daily_at_limit_and_cumulative_full_halt_sets_both_flags() -> None:
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=2.5,
        cumulative_tier=DrawdownTier.FULL_HALT,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.cumulative_full_halt_active is True
    assert result.daily_drawdown_pct == 2.5
    assert result.daily_drawdown_limit_pct == 2.5


# ---------------------------------------------------------------------------
# Daily limit lookup — regime-resolved limits drive the boundary
# ---------------------------------------------------------------------------


def test_elevated_regime_limit_2pct_at_boundary_triggers_halt() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=2.0)
    params = _build_active_risk_parameters(
        daily_drawdown_limit_pct=2.0,
        regime_label=RegimeLabel.ELEVATED,
    )

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.daily_drawdown_limit_pct == 2.0


def test_elevated_regime_limit_2pct_just_below_returns_none() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=1.99)
    params = _build_active_risk_parameters(
        daily_drawdown_limit_pct=2.0,
        regime_label=RegimeLabel.ELEVATED,
    )

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


def test_crisis_regime_limit_1_5pct_at_boundary_triggers_halt() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=1.5)
    params = _build_active_risk_parameters(
        daily_drawdown_limit_pct=1.5,
        regime_label=RegimeLabel.CRISIS,
    )

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.daily_drawdown_limit_pct == 1.5


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_daily_drawdown_rule_raises_valueerror() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=1.0)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=None)

    with pytest.raises(ValueError, match="daily_drawdown_pct"):
        compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)


# ---------------------------------------------------------------------------
# Determinism and shape
# ---------------------------------------------------------------------------


def test_repeated_calls_with_halt_active_produce_identical_haltstate() -> None:
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=2.7,
        cumulative_tier=DrawdownTier.FULL_HALT,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    results = {
        compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)
        for _ in range(100)
    }

    assert len(results) == 1


def test_repeated_calls_with_no_halt_produce_none() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=0.5)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    results = [
        compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)
        for _ in range(100)
    ]

    assert all(result is None for result in results)


def test_returned_haltstate_is_frozen() -> None:
    drawdown = _build_drawdown_state(intraday_drawdown_pct=2.5)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    with pytest.raises(ValidationError):
        result.daily_halt_active = False


def test_no_halt_returns_none_not_haltstate_with_false_flags() -> None:
    """When neither halt is active, the function returns ``None`` rather than
    constructing a ``HaltState`` (which would fail the post-validator anyway).
    """
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=0.5,
        cumulative_tier=DrawdownTier.CONSTRAINED,
    )
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    assert compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params) is None


# ---------------------------------------------------------------------------
# Worked examples from the design doc
# ---------------------------------------------------------------------------


def test_scenario_a4_daily_halt_only_normal_regime() -> None:
    """Scenario A4: 2.8% daily drawdown vs 2.5% normal-regime limit, no
    cumulative breach.
    """
    drawdown = _build_drawdown_state(intraday_drawdown_pct=2.8)
    params = _build_active_risk_parameters(daily_drawdown_limit_pct=2.5)

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.cumulative_full_halt_active is False
    assert result.daily_drawdown_pct == 2.8
    assert result.daily_drawdown_limit_pct == 2.5


def test_scenario_a10_both_halts_in_crisis_regime() -> None:
    """Scenario A10: crisis regime; drawdown spikes triggering both daily and
    cumulative-full-halt.
    """
    drawdown = _build_drawdown_state(
        intraday_drawdown_pct=4.0,
        cumulative_tier=DrawdownTier.FULL_HALT,
    )
    params = _build_active_risk_parameters(
        daily_drawdown_limit_pct=1.5,
        regime_label=RegimeLabel.CRISIS,
    )

    result = compute_halt_state(drawdown_state=drawdown, active_risk_parameters=params)

    assert result is not None
    assert result.daily_halt_active is True
    assert result.cumulative_full_halt_active is True
    assert result.daily_drawdown_pct == 4.0
    assert result.daily_drawdown_limit_pct == 1.5


# ---------------------------------------------------------------------------
# Re-export
# ---------------------------------------------------------------------------


def test_compute_halt_state_re_exported_from_package() -> None:
    from alphamind.risk_guardrails import breach_behavior

    assert breach_behavior.compute_halt_state is compute_halt_state
