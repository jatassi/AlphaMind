"""Tests for portfolio_state delivery-time field computations (story 05d)."""

from __future__ import annotations

import datetime

import pytest

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.computations.risk_budget import (
    compute_cash_pct_of_portfolio,
    compute_order_age_hours,
    compute_parameter_change_flag,
    compute_true_deployable_capital_usd,
)
from alphamind.portfolio_state.records.cash import CashLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cash_ledger(
    *,
    settled: float,
    reserved: float,
    margin: float,
) -> CashLedger:
    deployable = settled - reserved - margin
    return CashLedger(
        current_cash_usd=settled,
        settled_cash_usd=settled,
        reserved_capital_usd=reserved,
        available_buying_power_usd=max(deployable, 0.0),
        margin_held_usd=margin,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=50.0,
        true_deployable_capital_usd=deployable,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_param_entry(rule_id: str, value: float) -> ActiveRiskParameterEntry:
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_id,
        value=value,
        unit="pct",
        regime_multiplier_applied=1.0,
        base_value=value,
    )


def _make_param_set(
    *,
    regime: RegimeLabel = RegimeLabel.NORMAL,
    entries: tuple[ActiveRiskParameterEntry, ...] = (),
    overlays: tuple[str, ...] = (),
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=regime,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=overlays,
    )


# ---------------------------------------------------------------------------
# compute_cash_pct_of_portfolio
# ---------------------------------------------------------------------------


def test_cash_pct_happy_path() -> None:
    result = compute_cash_pct_of_portfolio(1_500.0, 10_000.0)
    assert result == pytest.approx(15.0)


def test_cash_pct_zero_cash() -> None:
    result = compute_cash_pct_of_portfolio(0.0, 10_000.0)
    assert result == pytest.approx(0.0)


def test_cash_pct_zero_total_returns_zero_not_error() -> None:
    result = compute_cash_pct_of_portfolio(1_000.0, 0.0)
    assert result == 0.0


def test_cash_pct_negative_cash_raises() -> None:
    with pytest.raises(ValueError):
        compute_cash_pct_of_portfolio(-1.0, 10_000.0)


def test_cash_pct_negative_total_raises() -> None:
    with pytest.raises(ValueError):
        compute_cash_pct_of_portfolio(100.0, -1.0)


def test_cash_pct_deterministic() -> None:
    assert compute_cash_pct_of_portfolio(500.0, 2_000.0) == compute_cash_pct_of_portfolio(
        500.0, 2_000.0
    )


# ---------------------------------------------------------------------------
# compute_true_deployable_capital_usd
# ---------------------------------------------------------------------------


def test_deployable_capital_positive() -> None:
    ledger = _make_cash_ledger(settled=5_000.0, reserved=1_000.0, margin=500.0)
    assert compute_true_deployable_capital_usd(ledger) == pytest.approx(3_500.0)


def test_deployable_capital_negative_allowed() -> None:
    # settled < reserved → negative result, no exception
    ledger = _make_cash_ledger(settled=1_000.0, reserved=1_200.0, margin=0.0)
    assert compute_true_deployable_capital_usd(ledger) == pytest.approx(-200.0)


def test_deployable_capital_deterministic() -> None:
    ledger = _make_cash_ledger(settled=3_000.0, reserved=500.0, margin=100.0)
    assert compute_true_deployable_capital_usd(ledger) == compute_true_deployable_capital_usd(
        ledger
    )


# ---------------------------------------------------------------------------
# compute_order_age_hours
# ---------------------------------------------------------------------------

_UTC = datetime.UTC


def test_order_age_six_hours() -> None:
    submission = datetime.datetime(2024, 1, 1, 6, 0, 0, tzinfo=_UTC)
    now = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    assert compute_order_age_hours(submission, now) == pytest.approx(6.0)


def test_order_age_zero_when_equal() -> None:
    ts = datetime.datetime(2024, 1, 1, 9, 0, 0, tzinfo=_UTC)
    assert compute_order_age_hours(ts, ts) == pytest.approx(0.0)


def test_order_age_negative_when_now_before_submission() -> None:
    submission = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    now = datetime.datetime(2024, 1, 1, 11, 0, 0, tzinfo=_UTC)
    result = compute_order_age_hours(submission, now)
    assert result < 0.0


def test_order_age_mixed_tz_raises() -> None:
    aware = datetime.datetime(2024, 1, 1, 9, 0, 0, tzinfo=_UTC)
    # Strip tzinfo to produce a naive datetime for testing the mixed-awareness guard
    naive = aware.replace(tzinfo=None)
    with pytest.raises(ValueError):
        compute_order_age_hours(naive, aware)
    with pytest.raises(ValueError):
        compute_order_age_hours(aware, naive)


def test_order_age_deterministic() -> None:
    submission = datetime.datetime(2024, 3, 15, 8, 0, 0, tzinfo=_UTC)
    now = datetime.datetime(2024, 3, 15, 10, 30, 0, tzinfo=_UTC)
    assert compute_order_age_hours(submission, now) == compute_order_age_hours(submission, now)


# ---------------------------------------------------------------------------
# compute_parameter_change_flag
# ---------------------------------------------------------------------------


def test_param_flag_prior_none_returns_false() -> None:
    current = _make_param_set()
    assert compute_parameter_change_flag(current, None) is False


def test_param_flag_regime_differs_returns_true() -> None:
    current = _make_param_set(regime=RegimeLabel.CRISIS)
    prior = _make_param_set(regime=RegimeLabel.NORMAL)
    assert compute_parameter_change_flag(current, prior) is True


def test_param_flag_rule_value_differs_returns_true() -> None:
    entry_current = _make_param_entry("max_position_pct", 5.0)
    entry_prior = _make_param_entry("max_position_pct", 4.0)
    current = _make_param_set(entries=(entry_current,))
    prior = _make_param_set(entries=(entry_prior,))
    assert compute_parameter_change_flag(current, prior) is True


def test_param_flag_overlays_differ_returns_true() -> None:
    current = _make_param_set(overlays=("pre-event",))
    prior = _make_param_set(overlays=())
    assert compute_parameter_change_flag(current, prior) is True


def test_param_flag_rule_added_returns_true() -> None:
    entry = _make_param_entry("max_sector_pct", 20.0)
    current = _make_param_set(entries=(entry,))
    prior = _make_param_set(entries=())
    assert compute_parameter_change_flag(current, prior) is True


def test_param_flag_rule_removed_returns_true() -> None:
    entry = _make_param_entry("max_sector_pct", 20.0)
    current = _make_param_set(entries=())
    prior = _make_param_set(entries=(entry,))
    assert compute_parameter_change_flag(current, prior) is True


def test_param_flag_identical_records_returns_false() -> None:
    current = _make_param_set(
        regime=RegimeLabel.NORMAL,
        entries=(_make_param_entry("max_position_pct", 5.0),),
        overlays=("stress",),
    )
    prior = _make_param_set(
        regime=RegimeLabel.NORMAL,
        entries=(_make_param_entry("max_position_pct", 5.0),),
        overlays=("stress",),
    )
    assert compute_parameter_change_flag(current, prior) is False


def test_param_flag_deterministic() -> None:
    current = _make_param_set(regime=RegimeLabel.ELEVATED)
    prior = _make_param_set(regime=RegimeLabel.NORMAL)
    assert compute_parameter_change_flag(current, prior) == compute_parameter_change_flag(
        current, prior
    )
