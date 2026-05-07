"""Tests for PortfolioStateSnapshot and its inline rollup types (story 04a)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    CashLedger,
    DrawdownState,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskZone,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    SupportingSignal,
    SupportingSignalStatus,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisStatus,
)
from alphamind.portfolio_state.records.thesis_quality import (
    AlphaBetaDecomposition,
    AttributionDimension,
    ConvictionCalibrationEntry,
    ConvictionSizingDeviation,
    InvalidationTimingClass,
    InvalidationTimingStat,
    PerformanceAttributionEntry,
    ResolutionWindowCounts,
    SignalHitRate,
    SignalToThesisConversion,
    ThesisDurationStat,
    ThesisQualityAggregate,
    TrailingWindow,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-test-001"


# ---------------------------------------------------------------------------
# Low-level record builders
# ---------------------------------------------------------------------------


def _make_fill(price: float = 150.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=_T0,
        fill_price=price,
        fill_quantity=100.0,
        slippage=0.01,
        fees=1.0,
    )


def _make_open_position(pos_id: str = "POS-001", ticker: str = "AAPL") -> PositionRecord:
    equity = EquityPositionDetails(
        ticker=ticker,
        share_count=100.0,
        average_cost_basis_per_share=150.0,
    )
    return PositionRecord.model_validate(
        {
            "position_id": pos_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _T0,
            "details": equity,
            "execution_history": (_make_fill(),),
            "realized_pnl_to_date_usd": None,
            "current_market_value_usd": 15500.0,
            "unrealized_pnl_usd": 500.0,
            "unrealized_pnl_pct": 3.33,
            "position_weight_pct": 10.0,
            "position_age_hours": 4.0,
            "notional_exposure_usd": 15000.0,
            "delta_adjusted_exposure_usd": 15000.0,
            "distance_to_target_usd": None,
            "distance_to_stop_usd": None,
            "risk_reward_at_current": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_pending_position(pos_id: str = "POS-003") -> PositionRecord:
    equity = EquityPositionDetails(
        ticker="GOOG",
        share_count=10.0,
        average_cost_basis_per_share=2800.0,
    )
    return PositionRecord.model_validate(
        {
            "position_id": pos_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.PENDING,
            "direction": Direction.LONG,
            "entry_timestamp": None,
            "details": equity,
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "current_market_value_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "position_weight_pct": 0.0,
            "position_age_hours": 0.0,
            "notional_exposure_usd": 0.0,
            "delta_adjusted_exposure_usd": 0.0,
            "distance_to_target_usd": None,
            "distance_to_stop_usd": None,
            "risk_reward_at_current": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_bracket(bracket_id: str = "BRK-001", position_id: str = "POS-001") -> BracketRecord:
    mechanical_leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id="ord-stop-1",
        trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord.model_validate(
        {
            "bracket_id": bracket_id,
            "position_id": position_id,
            "status": BracketStatus.PENDING_ENTRY,
            "entry_order_id": "ord-entry-1",
            "protective_legs": (mechanical_leg,),
            "modification_history": (),
            "corporate_action_cancellation_reason": None,
        }
    )


def _make_pending_order(
    order_id: str = "ORD-001",
    position_id: str = "POS-001",
    status: OrderStatus = OrderStatus.PENDING,
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker="AAPL")
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": "BRK-001",
            "role": OrderRole.ENTRY,
            "instrument_spec": spec,
            "direction": OrderDirection.BUY,
            "order_type": OrderType.MARKET,
            "price_parameters": PriceParameters(limit_price=None, stop_trigger_price=None),
            "quantity": 10.0,
            "duration": OrderDuration.DAY,
            "status": status,
            "alpaca_order_id": "alp-001",
            "alpaca_order_id_chain": ("alp-001",),
            "submission_timestamp": _T0,
            "last_update_timestamp": _T0,
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": 10.0,
            "modification_count": 0,
            "originating_thesis_id": None,
            "originating_pm_command_id": None,
            "age_hours": 1.0,
        }
    )


def _make_thesis(
    thesis_id: str = "THESIS-001",
    position_id: str = "POS-001",
) -> ThesisRecord:
    def _comp(ctype: ThesisComponentType, cid: str) -> ThesisComponent:
        return ThesisComponent(
            component_id=cid,
            thesis_id=thesis_id,
            component_type=ctype,
            linked_bracket_leg_type=None,
            instrument_reference="AAPL",
            narrative="Narrative text.",
            key_assumptions=(KeyAssumption(text="Assumption", outcome=None),),
            supporting_signals=(
                SupportingSignal(name="volume", status=SupportingSignalStatus.PRESENT),
            ),
            generation_timestamp=_T0,
            resolution_outcome=None,
            resolution_notes=None,
        )

    return ThesisRecord.model_validate(
        {
            "thesis_id": thesis_id,
            "position_id": position_id,
            "summary": "Long AAPL on momentum breakout.",
            "components": (
                _comp(ThesisComponentType.ENTRY_RATIONALE, "comp-1"),
                _comp(ThesisComponentType.TARGET_RATIONALE, "comp-2"),
                _comp(ThesisComponentType.INVALIDATION_RATIONALE, "comp-3"),
            ),
            "status": ThesisRecordStatus.ACTIVE,
            "health_status": ThesisStatus.ON_TRACK,
            "prior_health_status": None,
            "generation_timestamp": _T0,
            "time_expectation_hours": 24.0,
            "age_hours": 4.0,
            "expected_resolution_at": _T0 + timedelta(hours=24),
            "resolution_timestamp": None,
            "resolution_category": None,
            "resolution_pnl_usd": None,
            "entry_fill_gap_usd": None,
            "key_catalyst": "earnings beat",
        }
    )


def _make_activity_entry(
    entry_id: str = "ENTRY-001",
    invocation_id: str = _INV_ID,
    position_id: str | None = "POS-001",
    event_type: EventType = EventType.POSITION_OPENED,
    event_group: EventGroup = EventGroup.POSITION_LIFECYCLE,
    detail: object | None = None,
) -> ActivityLogEntry:
    if detail is None:
        detail = PositionOpenedDetail(
            ticker="AAPL",
            direction="LONG",
            fill_price=150.0,
            quantity=100.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=_T0,
        event_type=event_type,
        event_group=event_group,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.FILL_PROCESSOR,
        detail=detail,
    )


def _make_pm_decision_entry(
    entry_id: str = "ENTRY-PM-001",
    invocation_id: str = _INV_ID,
) -> ActivityLogEntry:
    detail = PMDecisionDetail(
        envelope_id="env-001",
        source_provenance_json={},
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=(),
        verdict=PMVerdict.APPROVE,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=_T0,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": 50000.0,
            "settled_cash_usd": 48000.0,
            "reserved_capital_usd": 2000.0,
            "available_buying_power_usd": 46000.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 50.0,
            "true_deployable_capital_usd": 44000.0,
            "regt_excess_trailing_30d_usd": 1000.0,
            "regt_excess_trailing_90d_usd": 3000.0,
            "regt_excess_lifetime_usd": 10000.0,
        }
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 2.0,
            "equity_high_water_mark_usd": 110000.0,
            "drawdown_duration_hours": 8.0,
            "lifetime_max_drawdown_pct": 5.0,
            "intraday_drawdown_pct": 0.5,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _make_active_risk_params() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet.model_validate(
        {
            "regime_label": RegimeLabel.NORMAL,
            "transition_state": RegimeTransitionState.STABLE,
            "transition_invocations_remaining": 0,
            "parameter_change_flag": False,
            "entries": (),
            "active_overlays": (),
        }
    )


def _make_thesis_quality() -> ThesisQualityAggregate:
    rwc = ResolutionWindowCounts.model_validate(
        {
            "window": TrailingWindow.FIVE_DAYS,
            "total_resolutions": 10,
            "validated": 6,
            "profitable_but_wrong": 1,
            "invalidated_stopped_correctly": 2,
            "invalidated_wrong_on_exit": 1,
            "cancelled_never_entered": 0,
        }
    )
    dur = ThesisDurationStat.model_validate(
        {
            "window": TrailingWindow.FIVE_DAYS,
            "mean_actual_to_expected_ratio": 1.1,
            "median_actual_to_expected_ratio": 1.0,
            "count": 10,
        }
    )
    inv_timing = InvalidationTimingStat.model_validate(
        {
            "window": TrailingWindow.FIVE_DAYS,
            "class_distribution": {
                InvalidationTimingClass.EARLY: 1,
                InvalidationTimingClass.ON_TIME: 2,
                InvalidationTimingClass.LATE: 0,
            },
            "mean_position_age_at_invalidation_hours": 5.0,
        }
    )
    shr = SignalHitRate.model_validate(
        {
            "signal_type": "options_volume",
            "window": TrailingWindow.FIVE_DAYS,
            "cited_count": 5,
            "validated_count": 3,
        }
    )
    stc = SignalToThesisConversion.model_validate(
        {
            "signal_type": "options_volume",
            "window": TrailingWindow.FIVE_DAYS,
            "signal_observed_count": 8,
            "pm_approved_count": 5,
        }
    )
    cc = ConvictionCalibrationEntry.model_validate(
        {
            "conviction_level": 3,
            "window": TrailingWindow.FIVE_DAYS,
            "count": 5,
            "validation_rate": 0.6,
            "mean_realized_pnl_pct": 2.5,
        }
    )
    csd = ConvictionSizingDeviation.model_validate(
        {
            "window": TrailingWindow.FIVE_DAYS,
            "total_proposals": 5,
            "pm_sized_above_advisory_count": 2,
            "pm_sized_below_advisory_count": 1,
            "pm_sized_within_advisory_count": 2,
            "outcome_correlation_above": None,
            "outcome_correlation_below": None,
        }
    )
    pa = PerformanceAttributionEntry.model_validate(
        {
            "dimension": AttributionDimension.SECTOR,
            "key": "Technology",
            "window": TrailingWindow.FIVE_DAYS,
            "cumulative_realized_pnl_usd": 1200.0,
            "realized_pnl_pct_of_window_capital": 1.2,
            "count": 3,
        }
    )
    ab = AlphaBetaDecomposition.model_validate(
        {
            "window": TrailingWindow.FIVE_DAYS,
            "total_realized_pnl_usd": 1200.0,
            "market_component_usd": 400.0,
            "sector_component_usd": 200.0,
            "alpha_component_usd": 600.0,
        }
    )
    return ThesisQualityAggregate.model_validate(
        {
            "as_of_timestamp": _T0,
            "resolution_counts_by_window": (rwc,),
            "duration_stats_by_window": (dur,),
            "invalidation_timing_stats_by_window": (inv_timing,),
            "signal_hit_rates": (shr,),
            "signal_to_thesis_conversions": (stc,),
            "conviction_calibration": (cc,),
            "conviction_sizing_deviation_by_window": (csd,),
            "performance_attribution": (pa,),
            "alpha_beta_decomposition_by_window": (ab,),
        }
    )


def _make_pnl(**overrides: object) -> PortfolioPnL:
    kwargs: dict[str, object] = {
        "total_unrealized_pnl_usd": 500.0,
        "total_unrealized_pnl_pct_of_portfolio": 1.5,
        "daily_realized_pnl_usd": 200.0,
        "daily_total_pnl_usd": 700.0,
        "cumulative_realized_pnl_usd": 10000.0,
        "rolling_realized_pnl": {"1d": 200.0, "3d": 600.0, "5d": 1000.0, "20d": 3000.0},
        "win_rate_pct": 62.5,
        "average_win_size_usd": 800.0,
        "average_loss_size_usd": 400.0,
        "profit_factor": 2.0,
    }
    kwargs.update(overrides)
    return PortfolioPnL.model_validate(kwargs)


def _make_directional(**overrides: object) -> DirectionalExposure:
    kwargs: dict[str, object] = {
        "total_long_delta_adjusted_usd": 50000.0,
        "total_short_delta_adjusted_usd": 20000.0,
        "net_directional_pct_of_portfolio": 30.0,
        "gross_pct_of_portfolio": 70.0,
    }
    kwargs.update(overrides)
    return DirectionalExposure.model_validate(kwargs)


def _make_sector_entry(**overrides: object) -> SectorExposureEntry:
    kwargs: dict[str, object] = {
        "sector": "TECHNOLOGY",
        "long_delta_adjusted_usd": 10000.0,
        "short_delta_adjusted_usd": 5000.0,
        "long_pct_of_portfolio": 20.0,
        "short_pct_of_portfolio": 10.0,
        "long_short_ratio": 2.0,
    }
    kwargs.update(overrides)
    return SectorExposureEntry.model_validate(kwargs)


# ---------------------------------------------------------------------------
# Happy-path snapshot builder
# ---------------------------------------------------------------------------


def _make_snapshot(**overrides: object) -> PortfolioStateSnapshot:
    """Build a valid PortfolioStateSnapshot with at least 2 open, 1 pending, 1 thesis, 1 order."""
    pos1 = _make_open_position("POS-001", "AAPL")
    pos2 = _make_open_position("POS-002", "MSFT")
    pos3 = _make_pending_position("POS-003")
    brk1 = _make_bracket("BRK-001", "POS-001")
    brk2 = _make_bracket("BRK-002", "POS-002")
    brk3 = _make_bracket("BRK-003", "POS-003")
    order1 = _make_pending_order("ORD-001", "POS-001", OrderStatus.PENDING)
    thesis1 = _make_thesis("THESIS-001", "POS-001")
    changelog_entry = _make_activity_entry("ENTRY-001", _INV_ID, "POS-001")
    pm_entry = _make_pm_decision_entry("ENTRY-PM-001", _INV_ID)
    mod_trail_entry = _make_activity_entry("ENTRY-MOD-001", _INV_ID, "POS-001")

    base: dict[str, object] = {
        "invocation_id": _INV_ID,
        "phase1_committed_at": _T0,
        "snapshot_assembled_at": _T1,
        "pipeline_invocation_started_at": _T2,
        "open_positions": (pos1, pos2),
        "pending_positions": (pos3,),
        "sector_exposure": (_make_sector_entry(),),
        "directional_exposure": _make_directional(),
        "portfolio_pnl": _make_pnl(),
        "drawdown": _make_drawdown_state(),
        "active_theses": (thesis1,),
        "recent_thesis_resolutions": (),
        "cash_ledger": _make_cash_ledger(),
        "pending_orders": (order1,),
        "risk_budget": _make_risk_budget(),
        "active_risk_parameters": _make_active_risk_params(),
        "intra_invocation_changelog": (changelog_entry,),
        "recent_pm_decision_log": (pm_entry,),
        "position_modification_trail": {"POS-001": (mod_trail_entry,)},
        "thesis_quality_aggregates": _make_thesis_quality(),
        "brackets": (brk1, brk2, brk3),
    }
    base.update(overrides)
    return PortfolioStateSnapshot.model_validate(base)


# ---------------------------------------------------------------------------
# PortfolioPnL
# ---------------------------------------------------------------------------


class TestPortfolioPnL:
    def test_valid_construction(self) -> None:
        pnl = _make_pnl()
        assert pnl.total_unrealized_pnl_usd == 500.0
        assert pnl.rolling_realized_pnl == {
            "1d": 200.0,
            "3d": 600.0,
            "5d": 1000.0,
            "20d": 3000.0,
        }
        assert pnl.win_rate_pct == 62.5
        assert pnl.profit_factor == 2.0

    def test_nullable_fields_accept_none(self) -> None:
        pnl = _make_pnl(
            win_rate_pct=None,
            average_win_size_usd=None,
            average_loss_size_usd=None,
            profit_factor=None,
        )
        assert pnl.win_rate_pct is None
        assert pnl.average_win_size_usd is None
        assert pnl.profit_factor is None

    def test_frozen(self) -> None:
        pnl = _make_pnl()
        with pytest.raises((ValidationError, AttributeError)):
            pnl.total_unrealized_pnl_usd = 999.0


# ---------------------------------------------------------------------------
# SectorExposureEntry
# ---------------------------------------------------------------------------


class TestSectorExposureEntry:
    def test_valid_construction(self) -> None:
        entry = _make_sector_entry()
        assert entry.sector == "TECHNOLOGY"
        assert entry.long_delta_adjusted_usd == 10000.0
        assert entry.long_short_ratio == 2.0

    def test_long_short_ratio_none_when_no_short(self) -> None:
        entry = _make_sector_entry(short_delta_adjusted_usd=0.0, long_short_ratio=None)
        assert entry.long_short_ratio is None

    def test_zero_pct_fields_accepted(self) -> None:
        entry = _make_sector_entry(long_pct_of_portfolio=0.0, short_pct_of_portfolio=0.0)
        assert entry.long_pct_of_portfolio == 0.0
        assert entry.short_pct_of_portfolio == 0.0

    def test_unclassified_sentinel(self) -> None:
        entry = _make_sector_entry(sector="UNCLASSIFIED")
        assert entry.sector == "UNCLASSIFIED"

    def test_frozen(self) -> None:
        entry = _make_sector_entry()
        with pytest.raises((ValidationError, AttributeError)):
            entry.sector = "OTHER"


# ---------------------------------------------------------------------------
# DirectionalExposure
# ---------------------------------------------------------------------------


class TestDirectionalExposure:
    def test_valid_construction(self) -> None:
        de = _make_directional()
        assert de.total_long_delta_adjusted_usd == 50000.0
        assert de.gross_pct_of_portfolio == 70.0

    def test_zero_portfolio_all_zeros(self) -> None:
        de = _make_directional(
            total_long_delta_adjusted_usd=0.0,
            total_short_delta_adjusted_usd=0.0,
            net_directional_pct_of_portfolio=0.0,
            gross_pct_of_portfolio=0.0,
        )
        assert de.gross_pct_of_portfolio == 0.0

    def test_negative_gross_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_directional(gross_pct_of_portfolio=-1.0)

    def test_frozen(self) -> None:
        de = _make_directional()
        with pytest.raises((ValidationError, AttributeError)):
            de.gross_pct_of_portfolio = 99.0


# ---------------------------------------------------------------------------
# Happy-path PortfolioStateSnapshot
# ---------------------------------------------------------------------------


class TestPortfolioStateSnapshotHappyPath:
    def test_constructs_without_error(self) -> None:
        snap = _make_snapshot()
        assert snap.invocation_id == _INV_ID
        assert len(snap.open_positions) == 2
        assert len(snap.pending_positions) == 1
        assert len(snap.brackets) == 3

    def test_position_by_id_open(self) -> None:
        snap = _make_snapshot()
        pos = snap.position_by_id("POS-001")
        assert pos is not None
        assert pos.position_id == "POS-001"

    def test_position_by_id_pending(self) -> None:
        snap = _make_snapshot()
        pos = snap.position_by_id("POS-003")
        assert pos is not None
        assert pos.position_id == "POS-003"

    def test_position_by_id_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.position_by_id("UNKNOWN") is None

    def test_bracket_for_position_found(self) -> None:
        snap = _make_snapshot()
        brk = snap.bracket_for_position("POS-001")
        assert brk is not None
        assert brk.bracket_id == "BRK-001"

    def test_bracket_for_position_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.bracket_for_position("UNKNOWN") is None

    def test_active_thesis_for_position_found(self) -> None:
        snap = _make_snapshot()
        thesis = snap.active_thesis_for_position("POS-001")
        assert thesis is not None
        assert thesis.thesis_id == "THESIS-001"

    def test_active_thesis_for_position_no_thesis(self) -> None:
        snap = _make_snapshot()
        # POS-002 has no thesis
        assert snap.active_thesis_for_position("POS-002") is None

    def test_active_thesis_for_position_unknown(self) -> None:
        snap = _make_snapshot()
        assert snap.active_thesis_for_position("UNKNOWN") is None

    def test_pending_orders_for_position_found(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("POS-001")
        assert len(orders) == 1
        assert orders[0].order_id == "ORD-001"

    def test_pending_orders_for_position_empty(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("POS-002")
        assert orders == ()

    def test_pending_orders_for_unknown_position(self) -> None:
        snap = _make_snapshot()
        orders = snap.pending_orders_for_position("UNKNOWN")
        assert orders == ()

    def test_pipeline_invocation_started_at_present(self) -> None:
        snap = _make_snapshot()
        assert snap.pipeline_invocation_started_at == _T2


# ---------------------------------------------------------------------------
# Validator: open position status
# ---------------------------------------------------------------------------


class TestOpenPositionStatusValidator:
    def test_open_position_correct_status_passes(self) -> None:
        snap = _make_snapshot()
        assert all(p.status == PositionStatus.OPEN for p in snap.open_positions)

    def test_open_position_wrong_status_raises(self) -> None:
        bad_pos = _make_pending_position("POS-BAD")
        with pytest.raises(ValidationError):
            _make_snapshot(open_positions=(bad_pos,))


# ---------------------------------------------------------------------------
# Validator: pending position status
# ---------------------------------------------------------------------------


class TestPendingPositionStatusValidator:
    def test_pending_position_correct_status_passes(self) -> None:
        snap = _make_snapshot()
        assert all(p.status == PositionStatus.PENDING for p in snap.pending_positions)

    def test_pending_position_wrong_status_raises(self) -> None:
        bad_pos = _make_open_position("POS-004")
        with pytest.raises(ValidationError):
            _make_snapshot(pending_positions=(bad_pos,))


# ---------------------------------------------------------------------------
# Validator: pending order status
# ---------------------------------------------------------------------------


class TestPendingOrderStatusValidator:
    def test_partially_filled_order_passes(self) -> None:
        order = OrderRecord.model_validate(
            {
                "order_id": "ORD-002",
                "position_id": "POS-001",
                "bracket_id": "BRK-001",
                "role": OrderRole.ENTRY,
                "instrument_spec": EquityInstrumentSpec(ticker="AAPL"),
                "direction": OrderDirection.BUY,
                "order_type": OrderType.MARKET,
                "price_parameters": PriceParameters(limit_price=None, stop_trigger_price=None),
                "quantity": 10.0,
                "duration": OrderDuration.DAY,
                "status": OrderStatus.PARTIALLY_FILLED,
                "alpaca_order_id": "alp-002",
                "alpaca_order_id_chain": ("alp-002",),
                "submission_timestamp": _T0,
                "last_update_timestamp": _T0,
                "filled_quantity": 5.0,
                "avg_fill_price": 150.0,
                "remaining_quantity": 5.0,
                "modification_count": 0,
                "originating_thesis_id": None,
                "originating_pm_command_id": None,
                "age_hours": 1.0,
            }
        )
        snap = _make_snapshot(pending_orders=(order,))
        assert snap.pending_orders[0].status == OrderStatus.PARTIALLY_FILLED

    def test_filled_order_in_pending_raises(self) -> None:
        filled_order = OrderRecord.model_validate(
            {
                "order_id": "ORD-FILLED",
                "position_id": "POS-001",
                "bracket_id": "BRK-001",
                "role": OrderRole.ENTRY,
                "instrument_spec": EquityInstrumentSpec(ticker="AAPL"),
                "direction": OrderDirection.BUY,
                "order_type": OrderType.MARKET,
                "price_parameters": PriceParameters(limit_price=None, stop_trigger_price=None),
                "quantity": 10.0,
                "duration": OrderDuration.DAY,
                "status": OrderStatus.FILLED,
                "alpaca_order_id": "alp-filled",
                "alpaca_order_id_chain": ("alp-filled",),
                "submission_timestamp": _T0,
                "last_update_timestamp": _T0,
                "filled_quantity": 10.0,
                "avg_fill_price": 150.0,
                "remaining_quantity": 0.0,
                "modification_count": 0,
                "originating_thesis_id": None,
                "originating_pm_command_id": None,
                "age_hours": 1.0,
            }
        )
        with pytest.raises(ValidationError):
            _make_snapshot(pending_orders=(filled_order,))


# ---------------------------------------------------------------------------
# Validator: orphan-free brackets
# ---------------------------------------------------------------------------


class TestOrphanBracketValidator:
    def test_all_brackets_resolve_passes(self) -> None:
        snap = _make_snapshot()
        for brk in snap.brackets:
            assert snap.position_by_id(brk.position_id) is not None

    def test_orphan_bracket_raises(self) -> None:
        orphan = _make_bracket("BRK-ORPHAN", "POS-NONEXISTENT")
        with pytest.raises(ValidationError):
            _make_snapshot(brackets=(_make_bracket("BRK-001", "POS-001"), orphan))


# ---------------------------------------------------------------------------
# Validator: position modification trail
# ---------------------------------------------------------------------------


class TestPositionModificationTrailValidator:
    def test_valid_trail_passes(self) -> None:
        snap = _make_snapshot()
        assert "POS-001" in snap.position_modification_trail

    def test_unresolvable_key_raises(self) -> None:
        entry = _make_activity_entry("ENTRY-X", _INV_ID, "POS-GHOST")
        with pytest.raises(ValidationError):
            _make_snapshot(position_modification_trail={"POS-GHOST": (entry,)})


# ---------------------------------------------------------------------------
# Validator: intra_invocation_changelog scoping
# ---------------------------------------------------------------------------


class TestIntraInvocationChangelogValidator:
    def test_matching_invocation_id_passes(self) -> None:
        snap = _make_snapshot()
        assert all(e.invocation_id == _INV_ID for e in snap.intra_invocation_changelog)

    def test_mismatched_invocation_id_raises(self) -> None:
        wrong_entry = _make_activity_entry("ENTRY-WRONG", "inv-other-999", "POS-001")
        with pytest.raises(ValidationError):
            _make_snapshot(intra_invocation_changelog=(wrong_entry,))


# ---------------------------------------------------------------------------
# Validator: timestamp ordering
# ---------------------------------------------------------------------------


class TestTimestampOrderingValidator:
    def test_phase1_before_assembled_passes(self) -> None:
        snap = _make_snapshot(phase1_committed_at=_T0, snapshot_assembled_at=_T1)
        assert snap.phase1_committed_at <= snap.snapshot_assembled_at

    def test_phase1_equal_assembled_passes(self) -> None:
        snap = _make_snapshot(phase1_committed_at=_T0, snapshot_assembled_at=_T0)
        assert snap.phase1_committed_at == snap.snapshot_assembled_at

    def test_phase1_after_assembled_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_snapshot(phase1_committed_at=_T1, snapshot_assembled_at=_T0)

    def test_pipeline_start_after_assembled_passes(self) -> None:
        snap = _make_snapshot(
            snapshot_assembled_at=_T1,
            pipeline_invocation_started_at=_T2,
        )
        assert snap.pipeline_invocation_started_at is not None
        assert snap.pipeline_invocation_started_at >= snap.snapshot_assembled_at

    def test_pipeline_start_none_passes(self) -> None:
        snap = _make_snapshot(pipeline_invocation_started_at=None)
        assert snap.pipeline_invocation_started_at is None

    def test_pipeline_start_before_assembled_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_snapshot(
                snapshot_assembled_at=_T1,
                pipeline_invocation_started_at=_T0,
            )


# ---------------------------------------------------------------------------
# Validator: PM decision log event type
# ---------------------------------------------------------------------------


class TestPMDecisionLogValidator:
    def test_pm_decision_entries_pass(self) -> None:
        snap = _make_snapshot()
        assert all(e.event_type == EventType.PM_DECISION for e in snap.recent_pm_decision_log)

    def test_non_pm_decision_entry_raises(self) -> None:
        non_pm = _make_activity_entry("ENTRY-NON-PM", _INV_ID, "POS-001")
        with pytest.raises(ValidationError):
            _make_snapshot(recent_pm_decision_log=(non_pm,))


# ---------------------------------------------------------------------------
# Validator: position ID uniqueness
# ---------------------------------------------------------------------------


class TestPositionIdUniquenessValidator:
    def test_unique_ids_pass(self) -> None:
        snap = _make_snapshot()
        all_ids = [p.position_id for p in snap.open_positions] + [
            p.position_id for p in snap.pending_positions
        ]
        assert len(all_ids) == len(set(all_ids))

    def test_duplicate_position_id_open_pending_raises(self) -> None:
        pos_open = _make_open_position("POS-DUP")
        pos_pending = _make_pending_position("POS-DUP")
        brk = _make_bracket("BRK-DUP", "POS-DUP")
        with pytest.raises(ValidationError):
            _make_snapshot(
                open_positions=(pos_open,),
                pending_positions=(pos_pending,),
                brackets=(brk,),
                position_modification_trail={},
                intra_invocation_changelog=(),
                pending_orders=(),
                active_theses=(),
            )


# ---------------------------------------------------------------------------
# Validator: bracket ID uniqueness
# ---------------------------------------------------------------------------


class TestBracketIdUniquenessValidator:
    def test_unique_bracket_ids_pass(self) -> None:
        snap = _make_snapshot()
        ids = [b.bracket_id for b in snap.brackets]
        assert len(ids) == len(set(ids))

    def test_duplicate_bracket_id_raises(self) -> None:
        brk1 = _make_bracket("BRK-DUP", "POS-001")
        brk2 = _make_bracket("BRK-DUP", "POS-002")
        with pytest.raises(ValidationError):
            _make_snapshot(brackets=(brk1, brk2))


# ---------------------------------------------------------------------------
# Validator: invocation_id non-empty
# ---------------------------------------------------------------------------


class TestInvocationIdValidator:
    def test_non_empty_invocation_id_passes(self) -> None:
        new_inv = "any-non-empty"
        changelog = (_make_activity_entry("ENTRY-001", new_inv, "POS-001"),)
        pm_log = (_make_pm_decision_entry("ENTRY-PM-001", new_inv),)
        mod_trail = {"POS-001": (_make_activity_entry("ENTRY-MOD-001", new_inv, "POS-001"),)}
        snap = _make_snapshot(
            invocation_id=new_inv,
            intra_invocation_changelog=changelog,
            recent_pm_decision_log=pm_log,
            position_modification_trail=mod_trail,
        )
        assert snap.invocation_id == new_inv

    def test_empty_invocation_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            _make_snapshot(invocation_id="")


# ---------------------------------------------------------------------------
# Validator: tz-aware timestamps
# ---------------------------------------------------------------------------


class TestTimestampTzAwarenessValidator:
    def test_naive_phase1_committed_at_raises(self) -> None:
        naive = _T0.replace(tzinfo=None)
        with pytest.raises(ValidationError):
            _make_snapshot(phase1_committed_at=naive)

    def test_naive_snapshot_assembled_at_raises(self) -> None:
        naive = _T1.replace(tzinfo=None)
        with pytest.raises(ValidationError):
            _make_snapshot(
                phase1_committed_at=_T0,
                snapshot_assembled_at=naive,
            )

    def test_naive_pipeline_started_at_raises(self) -> None:
        naive = _T2.replace(tzinfo=None)
        with pytest.raises(ValidationError):
            _make_snapshot(pipeline_invocation_started_at=naive)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_cannot_mutate_open_positions(self) -> None:
        snap = _make_snapshot()
        with pytest.raises((ValidationError, AttributeError)):
            snap.open_positions = ()

    def test_cannot_mutate_invocation_id(self) -> None:
        snap = _make_snapshot()
        with pytest.raises((ValidationError, AttributeError)):
            snap.invocation_id = "other"


# ---------------------------------------------------------------------------
# Deep copy equality
# ---------------------------------------------------------------------------


class TestDeepCopyEquality:
    def test_deepcopy_equals_original(self) -> None:
        snap = _make_snapshot()
        assert copy.deepcopy(snap) == snap


# ---------------------------------------------------------------------------
# Empty portfolio
# ---------------------------------------------------------------------------


class TestEmptyPortfolio:
    def test_empty_portfolio_constructs_without_error(self) -> None:
        snap = PortfolioStateSnapshot.model_validate(
            {
                "invocation_id": "inv-empty-001",
                "phase1_committed_at": _T0,
                "snapshot_assembled_at": _T1,
                "pipeline_invocation_started_at": None,
                "open_positions": (),
                "pending_positions": (),
                "sector_exposure": (),
                "directional_exposure": _make_directional(
                    total_long_delta_adjusted_usd=0.0,
                    total_short_delta_adjusted_usd=0.0,
                    net_directional_pct_of_portfolio=0.0,
                    gross_pct_of_portfolio=0.0,
                ),
                "portfolio_pnl": _make_pnl(),
                "drawdown": _make_drawdown_state(),
                "active_theses": (),
                "recent_thesis_resolutions": (),
                "cash_ledger": _make_cash_ledger(),
                "pending_orders": (),
                "risk_budget": _make_risk_budget(),
                "active_risk_parameters": _make_active_risk_params(),
                "intra_invocation_changelog": (),
                "recent_pm_decision_log": (),
                "position_modification_trail": {},
                "thesis_quality_aggregates": _make_thesis_quality(),
                "brackets": (),
            }
        )
        assert snap.open_positions == ()
        assert snap.pending_positions == ()
        assert snap.brackets == ()
        assert snap.position_modification_trail == {}
