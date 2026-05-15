"""Tests for capital state records (story 03d)."""
# mypy: disable-error-code="arg-type,call-arg,dict-item,misc,no-untyped-def,no-untyped-call,unused-ignore,no-any-return,var-annotated"

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alphamind._kernel.regime import (
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.cash import (
    CashLedger,
    UnsettledProceedsEntry,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestRegimeLabel:
    def test_members(self) -> None:
        members = {m.name for m in RegimeLabel}
        assert members == {"LOW_VOL", "NORMAL", "ELEVATED", "CRISIS"}

    def test_string_values(self) -> None:
        assert RegimeLabel.LOW_VOL == "LOW_VOL"
        assert RegimeLabel.NORMAL == "NORMAL"
        assert RegimeLabel.ELEVATED == "ELEVATED"
        assert RegimeLabel.CRISIS == "CRISIS"

    def test_exact_count(self) -> None:
        assert len(RegimeLabel) == 4


class TestRegimeTransitionState:
    def test_members(self) -> None:
        members = {m.name for m in RegimeTransitionState}
        assert members == {"STABLE", "TIGHTENING", "LOOSENING"}

    def test_string_values(self) -> None:
        assert RegimeTransitionState.STABLE == "STABLE"
        assert RegimeTransitionState.TIGHTENING == "TIGHTENING"
        assert RegimeTransitionState.LOOSENING == "LOOSENING"

    def test_exact_count(self) -> None:
        assert len(RegimeTransitionState) == 3


class TestRiskZone:
    def test_members(self) -> None:
        members = {m.name for m in RiskZone}
        assert members == {"NORMAL", "WARNING", "CRITICAL", "BLOCKED"}

    def test_string_values(self) -> None:
        assert RiskZone.NORMAL == "NORMAL"
        assert RiskZone.WARNING == "WARNING"
        assert RiskZone.CRITICAL == "CRITICAL"
        assert RiskZone.BLOCKED == "BLOCKED"

    def test_exact_count(self) -> None:
        assert len(RiskZone) == 4


class TestDrawdownTier:
    def test_members(self) -> None:
        members = {m.name for m in DrawdownTier}
        assert members == {"CONSTRAINED", "HEAVILY_CONSTRAINED", "FULL_HALT"}

    def test_string_values(self) -> None:
        assert DrawdownTier.CONSTRAINED == "CONSTRAINED"
        assert DrawdownTier.HEAVILY_CONSTRAINED == "HEAVILY_CONSTRAINED"
        assert DrawdownTier.FULL_HALT == "FULL_HALT"

    def test_exact_count(self) -> None:
        assert len(DrawdownTier) == 3


# ---------------------------------------------------------------------------
# UnsettledProceedsEntry
# ---------------------------------------------------------------------------


class TestUnsettledProceedsEntry:
    def _valid(self, **kwargs: object) -> UnsettledProceedsEntry:
        defaults: dict[str, object] = {
            "settlement_date": datetime(2024, 1, 5, 0, 0, tzinfo=UTC),
            "amount_usd": 1500.00,
            "source_transaction_id": "TXN-001",
        }
        defaults.update(kwargs)
        return UnsettledProceedsEntry(**defaults)

    def test_valid_construction(self) -> None:
        entry = self._valid()
        assert entry.amount_usd == 1500.00
        assert entry.source_transaction_id == "TXN-001"

    def test_tz_aware_date_accepted(self) -> None:
        entry = self._valid(settlement_date=datetime(2024, 1, 5, 12, 0, tzinfo=UTC))
        assert entry.settlement_date.tzinfo is not None

    def test_naive_datetime_raises(self) -> None:
        naive = datetime.fromisoformat("2024-01-05T00:00:00")  # no tzinfo
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(settlement_date=naive)

    def test_frozen(self) -> None:
        entry = self._valid()
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            entry.amount_usd = 999.0  # pyright: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# CashLedger
# ---------------------------------------------------------------------------


class TestCashLedger:
    def _valid(self, **kwargs: object) -> CashLedger:
        defaults: dict[str, object] = {
            "current_cash_usd": 10_000.0,
            "settled_cash_usd": 9_800.0,
            "reserved_capital_usd": 500.0,
            "available_buying_power_usd": 9_300.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 20.0,
            "true_deployable_capital_usd": 8_800.0,
            "regt_excess_trailing_30d_usd": 100.0,
            "regt_excess_trailing_90d_usd": 300.0,
            "regt_excess_lifetime_usd": 1_000.0,
        }
        defaults.update(kwargs)
        return CashLedger(**defaults)

    def test_valid_empty_unsettled(self) -> None:
        ledger = self._valid()
        assert ledger.unsettled_proceeds == ()

    def test_valid_with_unsettled_proceeds(self) -> None:
        entry = UnsettledProceedsEntry(
            settlement_date=datetime(2024, 1, 5, 0, 0, tzinfo=UTC),
            amount_usd=200.0,
            source_transaction_id="TXN-X",
        )
        ledger = self._valid(unsettled_proceeds=(entry,))
        assert len(ledger.unsettled_proceeds) == 1

    def test_cash_pct_at_zero_accepted(self) -> None:
        ledger = self._valid(cash_pct_of_portfolio=0.0)
        assert ledger.cash_pct_of_portfolio == 0.0

    def test_cash_pct_at_100_accepted(self) -> None:
        ledger = self._valid(cash_pct_of_portfolio=100.0)
        assert ledger.cash_pct_of_portfolio == 100.0

    def test_cash_pct_below_zero_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(cash_pct_of_portfolio=-0.1)

    def test_cash_pct_above_100_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(cash_pct_of_portfolio=100.1)

    def test_nan_usd_field_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(current_cash_usd=float("nan"))

    def test_inf_usd_field_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(settled_cash_usd=float("inf"))

    def test_neg_inf_usd_field_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(margin_held_usd=float("-inf"))

    def test_frozen(self) -> None:
        ledger = self._valid()
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            ledger.current_cash_usd = 0.0  # pyright: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# DrawdownState
# ---------------------------------------------------------------------------


class TestDrawdownState:
    def _valid(self, **kwargs: object) -> DrawdownState:
        defaults: dict[str, object] = {
            "current_drawdown_pct": 3.5,
            "equity_high_water_mark_usd": 120_000.0,
            "drawdown_duration_hours": 12.0,
            "lifetime_max_drawdown_pct": 8.0,
            "intraday_drawdown_pct": 1.2,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.WARNING,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {"AAPL": 1.5, "tech": 2.0},
        }
        defaults.update(kwargs)
        return DrawdownState(**defaults)

    def test_valid_construction(self) -> None:
        state = self._valid()
        assert state.current_drawdown_pct == 3.5
        assert state.cumulative_tier is None

    def test_cumulative_tier_accepts_none(self) -> None:
        state = self._valid(cumulative_tier=None)
        assert state.cumulative_tier is None

    def test_cumulative_tier_accepts_constrained(self) -> None:
        state = self._valid(cumulative_tier=DrawdownTier.CONSTRAINED)
        assert state.cumulative_tier == DrawdownTier.CONSTRAINED

    def test_cumulative_tier_accepts_full_halt(self) -> None:
        state = self._valid(cumulative_tier=DrawdownTier.FULL_HALT)
        assert state.cumulative_tier == DrawdownTier.FULL_HALT

    def test_current_drawdown_pct_zero_accepted(self) -> None:
        state = self._valid(current_drawdown_pct=0.0)
        assert state.current_drawdown_pct == 0.0

    def test_current_drawdown_pct_negative_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(current_drawdown_pct=-0.1)

    def test_lifetime_max_drawdown_pct_negative_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(lifetime_max_drawdown_pct=-0.1)

    def test_intraday_drawdown_pct_negative_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(intraday_drawdown_pct=-0.1)

    def test_drawdown_duration_hours_negative_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            self._valid(drawdown_duration_hours=-1.0)

    def test_empty_drawdown_by_source_accepted(self) -> None:
        state = self._valid(drawdown_by_source_pct={})
        assert state.drawdown_by_source_pct == {}


# ---------------------------------------------------------------------------
# RiskBudgetEntry
# ---------------------------------------------------------------------------


def _make_risk_budget_entry(**kwargs: object) -> RiskBudgetEntry:
    defaults: dict[str, object] = {
        "rule_id": "concentration.single_name",
        "rule_label": "Single Name Concentration",
        "current_value": 15.0,
        "limit_value": 20.0,
        "headroom": 5.0,  # 20.0 - 15.0
        "headroom_pct_of_limit": 25.0,  # 5/20 * 100
        "zone": RiskZone.NORMAL,
        "unit": "% of portfolio (delta-adjusted)",
        "cumulative_invocation_impact_value": 0.0,
    }
    defaults.update(kwargs)
    return RiskBudgetEntry(**defaults)


class TestRiskBudgetEntry:
    def test_valid_construction(self) -> None:
        entry = _make_risk_budget_entry()
        assert entry.headroom == 5.0
        assert entry.zone == RiskZone.NORMAL

    def test_headroom_inconsistent_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_risk_budget_entry(current_value=15.0, limit_value=20.0, headroom=6.0)  # wrong

    def test_headroom_pct_at_zero_accepted(self) -> None:
        entry = _make_risk_budget_entry(
            current_value=20.0, limit_value=20.0, headroom=0.0, headroom_pct_of_limit=0.0
        )
        assert entry.headroom_pct_of_limit == 0.0

    def test_headroom_pct_at_100_accepted(self) -> None:
        entry = _make_risk_budget_entry(
            current_value=0.0, limit_value=20.0, headroom=20.0, headroom_pct_of_limit=100.0
        )
        assert entry.headroom_pct_of_limit == 100.0

    def test_headroom_pct_below_zero_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_risk_budget_entry(
                current_value=15.0, limit_value=20.0, headroom=5.0, headroom_pct_of_limit=-1.0
            )

    def test_headroom_pct_above_100_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_risk_budget_entry(
                current_value=0.0, limit_value=20.0, headroom=20.0, headroom_pct_of_limit=101.0
            )

    def test_invalid_zone_accepted_at_construction(self) -> None:
        """Post-Pydantic dataclass: zone-string validation lives at the codec boundary.

        The Pydantic record enforced enum membership at construction. The frozen
        dataclass stores whatever ``zone`` value the caller passes; mismatched
        enums surface at the codec / consumer layer instead. Documenting the
        post-migration behavior so the test rebaselines explicitly.
        """
        entry = _make_risk_budget_entry(zone="UNKNOWN_ZONE")
        assert entry.zone == "UNKNOWN_ZONE"

    def test_nan_current_value_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_risk_budget_entry(
                current_value=float("nan"),
                limit_value=20.0,
                headroom=float("nan"),
                headroom_pct_of_limit=0.0,
            )

    def test_inf_limit_value_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_risk_budget_entry(
                current_value=0.0,
                limit_value=float("inf"),
                headroom=float("inf"),
                headroom_pct_of_limit=0.0,
            )


# ---------------------------------------------------------------------------
# RiskBudgetConsumption
# ---------------------------------------------------------------------------


def _make_entries() -> tuple[RiskBudgetEntry, ...]:
    e1 = _make_risk_budget_entry(
        rule_id="rule.alpha",
        rule_label="Alpha Rule",
        current_value=10.0,
        limit_value=20.0,
        headroom=10.0,
        headroom_pct_of_limit=50.0,
        zone=RiskZone.NORMAL,
    )
    e2 = _make_risk_budget_entry(
        rule_id="rule.beta",
        rule_label="Beta Rule",
        current_value=18.0,
        limit_value=20.0,
        headroom=2.0,
        headroom_pct_of_limit=10.0,
        zone=RiskZone.CRITICAL,
    )
    e3 = _make_risk_budget_entry(
        rule_id="rule.gamma",
        rule_label="Gamma Rule",
        current_value=19.5,
        limit_value=20.0,
        headroom=0.5,
        headroom_pct_of_limit=2.5,
        zone=RiskZone.BLOCKED,
    )
    return (e1, e2, e3)


class TestRiskBudgetConsumption:
    def test_valid_construction(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        assert len(consumption.entries) == 3

    def test_duplicate_rule_id_raises(self) -> None:
        e1 = _make_risk_budget_entry(
            rule_id="rule.dup",
            current_value=10.0,
            limit_value=20.0,
            headroom=10.0,
            headroom_pct_of_limit=50.0,
        )
        e2 = _make_risk_budget_entry(
            rule_id="rule.dup",  # duplicate
            current_value=5.0,
            limit_value=20.0,
            headroom=15.0,
            headroom_pct_of_limit=75.0,
        )
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            RiskBudgetConsumption(entries=(e1, e2))

    def test_entries_by_zone_normal(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        normal_entries = consumption.entries_by_zone(RiskZone.NORMAL)
        assert len(normal_entries) == 1
        assert normal_entries[0].rule_id == "rule.alpha"

    def test_entries_by_zone_empty_result(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        warning_entries = consumption.entries_by_zone(RiskZone.WARNING)
        assert warning_entries == ()

    def test_entries_by_zone_critical(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        critical_entries = consumption.entries_by_zone(RiskZone.CRITICAL)
        assert len(critical_entries) == 1
        assert critical_entries[0].rule_id == "rule.beta"

    def test_breaching_entries_returns_critical_and_blocked(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        breaching = consumption.breaching_entries()
        rule_ids = {e.rule_id for e in breaching}
        assert rule_ids == {"rule.beta", "rule.gamma"}

    def test_entry_by_rule_id_hit(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        entry = consumption.entry_by_rule_id("rule.beta")
        assert entry is not None
        assert entry.rule_id == "rule.beta"

    def test_entry_by_rule_id_miss(self) -> None:
        consumption = RiskBudgetConsumption(entries=_make_entries())
        assert consumption.entry_by_rule_id("rule.nonexistent") is None

    def test_empty_entries_accepted(self) -> None:
        consumption = RiskBudgetConsumption(entries=())
        assert consumption.entries == ()
        assert consumption.breaching_entries() == ()


# ---------------------------------------------------------------------------
# ActiveRiskParameterSet
# ---------------------------------------------------------------------------


def _make_param_entry(**kwargs: object) -> ActiveRiskParameterEntry:
    defaults: dict[str, object] = {
        "rule_id": "concentration.single_name",
        "rule_label": "Single Name Concentration",
        "value": 20.0,
        "unit": "% of portfolio (delta-adjusted)",
        "regime_multiplier_applied": 1.0,
        "base_value": 20.0,
    }
    defaults.update(kwargs)
    return ActiveRiskParameterEntry(**defaults)


def _make_param_set(**kwargs: object) -> ActiveRiskParameterSet:
    defaults: dict[str, object] = {
        "regime_label": RegimeLabel.NORMAL,
        "transition_state": RegimeTransitionState.STABLE,
        "transition_invocations_remaining": 0,
        "parameter_change_flag": False,
        "entries": (
            _make_param_entry(rule_id="rule.alpha"),
            _make_param_entry(rule_id="rule.beta"),
        ),
        "active_overlays": (),
    }
    defaults.update(kwargs)
    return ActiveRiskParameterSet(**defaults)


class TestActiveRiskParameterSet:
    def test_valid_stable_construction(self) -> None:
        param_set = _make_param_set()
        assert param_set.transition_state == RegimeTransitionState.STABLE
        assert param_set.transition_invocations_remaining == 0
        assert param_set.active_overlays == ()

    def test_stable_with_invocations_remaining_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_param_set(
                transition_state=RegimeTransitionState.STABLE,
                transition_invocations_remaining=1,
            )

    def test_loosening_with_two_remaining_passes(self) -> None:
        param_set = _make_param_set(
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
        )
        assert param_set.transition_invocations_remaining == 2

    def test_tightening_with_nonzero_remaining_passes(self) -> None:
        # tightening does not require remaining == 0
        param_set = _make_param_set(
            transition_state=RegimeTransitionState.TIGHTENING,
            transition_invocations_remaining=1,
        )
        assert param_set.transition_state == RegimeTransitionState.TIGHTENING

    def test_transition_invocations_remaining_zero_passes(self) -> None:
        param_set = _make_param_set(
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=0,
        )
        assert param_set.transition_invocations_remaining == 0

    def test_transition_invocations_remaining_negative_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_param_set(
                transition_state=RegimeTransitionState.LOOSENING,
                transition_invocations_remaining=-1,
            )

    def test_duplicate_rule_id_in_entries_raises(self) -> None:
        dup_entry = _make_param_entry(rule_id="rule.dup")
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            _make_param_set(entries=(dup_entry, dup_entry))

    def test_empty_active_overlays_passes(self) -> None:
        param_set = _make_param_set(active_overlays=())
        assert param_set.active_overlays == ()

    def test_active_overlays_with_values(self) -> None:
        param_set = _make_param_set(active_overlays=("pre-event-tightening", "stress-overlay"))
        assert len(param_set.active_overlays) == 2

    def test_parameter_change_flag_true(self) -> None:
        param_set = _make_param_set(parameter_change_flag=True)
        assert param_set.parameter_change_flag is True

    def test_crisis_regime_label(self) -> None:
        param_set = _make_param_set(regime_label=RegimeLabel.CRISIS)
        assert param_set.regime_label == RegimeLabel.CRISIS
