"""Tests for consumers/analyst.py — story 07."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.aggregates.thesis_quality import (
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
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystAvailableCapital,
    AnalystHeldPosition,
    AnalystView,
    project_analyst_view,
)
from alphamind.portfolio_state.consumers.synthesizer import SynthesizerThesisSummary
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    CommandAbandonedDetail,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
)
from alphamind.portfolio_state.records.cash import CashLedger
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
    InstrumentType,
    OptionGreeks,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.state_delivery.primitives import POSITION_MAX_SIZE_RULE_ID

# ---------------------------------------------------------------------------
# Shared timestamps / IDs
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-analyst-001"
_ANALYST_AGENT = "analyst"
_PER_POSITION_RULE_ID = POSITION_MAX_SIZE_RULE_ID
_TOTAL_PORTFOLIO_VALUE = 100000.0


# ---------------------------------------------------------------------------
# Low-level record builders
# ---------------------------------------------------------------------------


def _make_fill(at_price: float = 150.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=_T0,
        fill_price=price(at_price),
        fill_quantity=100.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )


def _make_open_position(pos_id: str = "POS-001", ticker: str = "AAPL") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=150.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_T0,
        details=equity,
        execution_history=(_make_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(15500.0),
        unrealized_pnl_usd=signed_money(500.0),
        unrealized_pnl_pct=3.33,
        position_weight_pct=10.0,
        position_age_hours=4.0,
        notional_exposure_usd=money(15000.0),
        delta_adjusted_exposure_usd=signed_money(15000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_pending_position(pos_id: str = "POS-PEND") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol("GOOG"),
        share_count=10.0,
        average_cost_basis_per_share=2800.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=equity,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(0.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=0.0,
        position_age_hours=0.0,
        notional_exposure_usd=money(0.0),
        delta_adjusted_exposure_usd=signed_money(0.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_strategy_position(
    pos_id: str = "POS-STRAT",
    strategy_type_label: str = "iron_condor",
) -> PositionView:
    strategy = StrategyPositionDetails(
        strategy_type_label=strategy_type_label,
        legs=(),
        net_premium_usd=-100.0,
        max_profit_usd=200.0,
        max_loss_usd=-500.0,
        breakeven_levels=(),
        strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_T0,
        details=strategy,
        execution_history=(_make_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(0.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=2.0,
        position_age_hours=4.0,
        notional_exposure_usd=money(0.0),
        delta_adjusted_exposure_usd=signed_money(0.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_bracket(bracket_id: str = "BRK-001", position_id: str = "POS-001") -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-stop-1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_pending_order(
    order_id: str = "ORD-001",
    position_id: str = "POS-001",
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol("AAPL"))
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId("BRK-001"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(limit_price=None, stop_trigger_price=None),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-001"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-001"),),
        submission_timestamp=_T0,
        last_update_timestamp=_T0,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )


def _make_thesis(
    thesis_id: str = "THESIS-001",
    position_id: str = "POS-001",
) -> ThesisRecord:
    def _comp(ctype: ThesisComponentType, cid: str) -> ThesisComponent:
        return ThesisComponent(
            component_id=cid,
            thesis_id=ThesisId(thesis_id),
            component_type=ctype,
            linked_bracket_leg_type=None,
            instrument_reference="AAPL",
            narrative="Narrative text.",
            key_assumptions=(KeyAssumption(text="Assumption", outcome=None),),
            generation_timestamp=_T0,
            resolution_outcome=None,
            resolution_notes=None,
        )

    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Long AAPL on momentum.",
        components=(
            _comp(ThesisComponentType.ENTRY_RATIONALE, "comp-1"),
            _comp(ThesisComponentType.TARGET_RATIONALE, "comp-2"),
            _comp(ThesisComponentType.INVALIDATION_RATIONALE, "comp-3"),
        ),
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_T0,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=_T0 + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="earnings beat",
    )


def _make_pm_decision_entry(entry_id: str = "ENTRY-PM-001") -> ActivityLogEntry:
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
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _make_abandoned_entry(
    entry_id: str = "ENTRY-ABN-001",
    originating_agent: str = _ANALYST_AGENT,
) -> ActivityLogEntry:
    detail = CommandAbandonedDetail(
        envelope_id="env-abn-001",
        command_id="cmd-abn-001",
        originating_agent=originating_agent,
        command_type="OPEN",
        failure_reason="Guardrail blocked",
        retry_attempt_count=0,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.COMMAND_ABANDONED,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _make_changelog_entry(
    entry_id: str = "ENTRY-CL-001",
    position_id: str | None = "POS-001",
) -> ActivityLogEntry:
    detail = PositionOpenedDetail(
        ticker=Symbol("AAPL"),
        direction="LONG",
        fill_price=price("150.0"),
        quantity=100.0,
        thesis_id=None,
        bracket_id=None,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INV_ID,
        timestamp=_T0,
        event_type=EventType.POSITION_OPENED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.FILL_PROCESSOR,
        detail=detail,
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=50000.0,
        settled_cash_usd=48000.0,
        reserved_capital_usd=2000.0,
        available_buying_power_usd=46000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=50.0,
        true_deployable_capital_usd=44000.0,
        regt_excess_trailing_30d_usd=1000.0,
        regt_excess_trailing_90d_usd=3000.0,
        regt_excess_lifetime_usd=10000.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=2.0,
        equity_high_water_mark_usd=110000.0,
        drawdown_duration_hours=8.0,
        lifetime_max_drawdown_pct=5.0,
        intraday_drawdown_pct=0.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _make_active_risk_params_with_size_rule(rule_value: float = 5.0) -> ActiveRiskParameterSet:
    entry = ActiveRiskParameterEntry(
        rule_id=_PER_POSITION_RULE_ID,
        rule_label="Position max size",
        value=rule_value,
        unit="pct",
        regime_multiplier_applied=1.0,
        base_value=rule_value,
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(entry,),
        active_overlays=(),
    )


def _make_thesis_quality() -> ThesisQualityAggregate:
    rwc = ResolutionWindowCounts(
        window=TrailingWindow.FIVE_DAYS,
        total_resolutions=0,
        validated=0,
        profitable_but_wrong=0,
        invalidated_stopped_correctly=0,
        invalidated_wrong_on_exit=0,
        cancelled_never_entered=0,
    )
    dur = ThesisDurationStat(
        window=TrailingWindow.FIVE_DAYS,
        mean_actual_to_expected_ratio=1.0,
        median_actual_to_expected_ratio=1.0,
        count=0,
    )
    inv_timing = InvalidationTimingStat(
        window=TrailingWindow.FIVE_DAYS,
        class_distribution={
            InvalidationTimingClass.EARLY: 0,
            InvalidationTimingClass.ON_TIME: 0,
            InvalidationTimingClass.LATE: 0,
        },
        mean_position_age_at_invalidation_hours=0.0,
    )
    shr = SignalHitRate(
        signal_type="volume", window=TrailingWindow.FIVE_DAYS, cited_count=0, validated_count=0
    )
    stc = SignalToThesisConversion(
        signal_type="volume",
        window=TrailingWindow.FIVE_DAYS,
        signal_observed_count=0,
        pm_approved_count=0,
    )
    cc = ConvictionCalibrationEntry(
        conviction_level=1,
        window=TrailingWindow.FIVE_DAYS,
        count=0,
        validation_rate=0.0,
        mean_realized_pnl_pct=0.0,
    )
    csd = ConvictionSizingDeviation(
        window=TrailingWindow.FIVE_DAYS,
        total_proposals=0,
        pm_sized_above_advisory_count=0,
        pm_sized_below_advisory_count=0,
        pm_sized_within_advisory_count=0,
        outcome_correlation_above=None,
        outcome_correlation_below=None,
    )
    pa = PerformanceAttributionEntry(
        dimension=AttributionDimension.SECTOR,
        key="Technology",
        window=TrailingWindow.FIVE_DAYS,
        cumulative_realized_pnl_usd=0.0,
        realized_pnl_pct_of_window_capital=0.0,
        count=0,
    )
    ab = AlphaBetaDecomposition(
        window=TrailingWindow.FIVE_DAYS,
        total_realized_pnl_usd=0.0,
        market_component_usd=0.0,
        sector_component_usd=0.0,
        alpha_component_usd=0.0,
    )
    return ThesisQualityAggregate(
        as_of_timestamp=_T0,
        resolution_counts_by_window=(rwc,),
        duration_stats_by_window=(dur,),
        invalidation_timing_stats_by_window=(inv_timing,),
        signal_hit_rates=(shr,),
        signal_to_thesis_conversions=(stc,),
        conviction_calibration=(cc,),
        conviction_sizing_deviation_by_window=(csd,),
        performance_attribution=(pa,),
        alpha_beta_decomposition_by_window=(ab,),
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=signed_money(0.0),
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=signed_money(0.0),
        daily_total_pnl_usd=signed_money(0.0),
        cumulative_realized_pnl_usd=signed_money(0.0),
        rolling_realized_pnl={
            "1d": signed_money(0.0),
            "3d": signed_money(0.0),
            "5d": signed_money(0.0),
            "20d": signed_money(0.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=signed_money(0.0),
        total_short_delta_adjusted_usd=signed_money(0.0),
        net_directional_pct_of_portfolio=0.0,
        gross_pct_of_portfolio=0.0,
    )


def _make_sector_entry() -> SectorExposureEntry:
    return SectorExposureEntry(
        sector="TECHNOLOGY",
        long_delta_adjusted_usd=signed_money(10000.0),
        short_delta_adjusted_usd=signed_money(0.0),
        long_pct_of_portfolio=20.0,
        short_pct_of_portfolio=0.0,
        long_short_ratio=None,
    )


def _simple_sector_resolver() -> SectorResolver:
    def resolver(pos: PositionRecord) -> str:
        return "TECHNOLOGY"

    return resolver


def _make_snapshot(**overrides: object) -> PortfolioStateSnapshot:
    pos1 = _make_open_position("POS-001", "AAPL")
    pos2 = _make_open_position("POS-002", "MSFT")
    pos3 = _make_pending_position("POS-PEND")
    brk1 = _make_bracket("BRK-001", "POS-001")
    brk2 = _make_bracket("BRK-002", "POS-002")
    brk3 = _make_bracket("BRK-003", "POS-PEND")
    order1 = _make_pending_order("ORD-001", "POS-001")
    thesis1 = _make_thesis("THESIS-001", "POS-001")
    pm_entry = _make_pm_decision_entry("ENTRY-PM-001")
    changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
    mod_entry = _make_changelog_entry("ENTRY-MOD-001", "POS-001")
    base: dict[str, Any] = {
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
        "active_risk_parameters": _make_active_risk_params_with_size_rule(),
        "intra_invocation_changelog": (changelog,),
        "recent_pm_decision_log": (pm_entry,),
        "position_modification_trail": {"POS-001": (mod_entry,)},
        "thesis_quality_aggregates": _make_thesis_quality(),
        "brackets": (brk1, brk2, brk3),
    }
    base.update(overrides)
    return PortfolioStateSnapshot(**base)


def _make_empty_snapshot() -> PortfolioStateSnapshot:
    return PortfolioStateSnapshot(
        invocation_id=_INV_ID,
        phase1_committed_at=_T0,
        snapshot_assembled_at=_T1,
        pipeline_invocation_started_at=None,
        open_positions=(),
        pending_positions=(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown_state(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=_make_risk_budget(),
        active_risk_parameters=_make_active_risk_params_with_size_rule(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality(),
        brackets=(),
    )


# ---------------------------------------------------------------------------
# Tests: value object models
# ---------------------------------------------------------------------------


class TestAnalystValueObjects:
    def test_held_position_frozen(self) -> None:
        hp = AnalystHeldPosition(
            position_id=PositionId("POS-001"),
            ticker=Symbol("AAPL"),
            direction=Direction.LONG,
            sector="TECHNOLOGY",
            size_pct=10.0,
            instrument_type=InstrumentType.EQUITY,
            strategy_type_label=None,
        )
        with pytest.raises(FrozenInstanceError):
            hp.ticker = "MSFT"  # type: ignore[misc]

    def test_available_capital_frozen(self) -> None:
        ac = AnalystAvailableCapital(
            available_for_new_positions_usd=44000.0,
            available_for_new_positions_pct=44.0,
            per_position_max_size_usd=5000.0,
            per_position_max_size_pct=5.0,
        )
        with pytest.raises(FrozenInstanceError):
            ac.available_for_new_positions_usd = 0.0  # type: ignore[misc]

    def test_abandoned_opening_frozen(self) -> None:
        ao = AnalystAbandonedOpening(
            envelope_id="env-001",
            direction=Direction.LONG,
            ticker=Symbol("AAPL"),
            instrument_type=InstrumentType.EQUITY,
            size_pct=5.0,
            abandoned_at=_T0,
            failure_reason="blocked",
        )
        with pytest.raises(FrozenInstanceError):
            ao.ticker = "MSFT"  # type: ignore[misc]

    def test_analyst_view_frozen(self) -> None:
        view = AnalystView(
            held_positions=(),
            active_thesis_summaries=(),
            available_capital=AnalystAvailableCapital(
                available_for_new_positions_usd=0.0,
                available_for_new_positions_pct=0.0,
                per_position_max_size_usd=0.0,
                per_position_max_size_pct=0.0,
            ),
            pending_orders=(),
            abandoned_openings=(),
        )
        with pytest.raises(FrozenInstanceError):
            view.pending_orders = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: project_analyst_view — happy path
# ---------------------------------------------------------------------------


class TestProjectAnalystViewHappyPath:
    def test_held_position_fields_are_thin_slice(self) -> None:
        """AnalystHeldPosition has only ticker, direction, size, sector, instrument_type."""
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        pos = next(p for p in view.held_positions if p.ticker == "AAPL")
        assert pos.position_id == "POS-001"
        assert pos.direction == Direction.LONG
        assert pos.sector == "TECHNOLOGY"
        assert pos.size_pct == 10.0
        assert pos.instrument_type == InstrumentType.EQUITY
        # Confirm no P/L fields
        assert not hasattr(pos, "unrealized_pnl_usd")
        assert not hasattr(pos, "current_market_value_usd")

    def test_held_equity_position_carries_direction_and_no_strategy_label(self) -> None:
        """An equity held position projects a concrete direction and a None label."""
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        pos = next(p for p in view.held_positions if p.ticker == "AAPL")
        assert pos.direction == Direction.LONG
        assert pos.strategy_type_label is None

    def test_held_strategy_position_projects_none_direction(self) -> None:
        """A multi-leg strategy held position yields direction=None via the accessor."""
        strategy = _make_strategy_position("POS-STRAT", "iron_condor")
        snapshot = _make_snapshot(
            open_positions=(strategy,),
            pending_positions=(),
            brackets=(),
            active_theses=(),
            position_modification_trail={},
            intra_invocation_changelog=(),
            recent_pm_decision_log=(),
            pending_orders=(),
        )
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        pos = next(p for p in view.held_positions if p.position_id == "POS-STRAT")
        assert pos.direction is None
        assert pos.strategy_type_label == "iron_condor"
        assert pos.instrument_type == InstrumentType.STRATEGY

    def test_available_capital_values(self) -> None:
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view.available_capital.available_for_new_positions_usd == 44000.0
        assert view.available_capital.available_for_new_positions_pct == pytest.approx(44.0)

    def test_pending_orders_present(self) -> None:
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert len(view.pending_orders) == 1
        assert view.pending_orders[0].order_id == "ORD-001"

    def test_active_thesis_summaries_present(self) -> None:
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert len(view.active_thesis_summaries) == 1
        assert isinstance(view.active_thesis_summaries[0], SynthesizerThesisSummary)

    def test_abandoned_openings_filtered_by_analyst_agent(self) -> None:
        """Only COMMAND_ABANDONED entries with originating_agent==analyst are included."""
        analyst_abn = _make_abandoned_entry("ENTRY-ABN-ANALYST", _ANALYST_AGENT)
        other_abn = _make_abandoned_entry("ENTRY-ABN-OTHER", "strategist")
        changelog = _make_changelog_entry("ENTRY-CL-001", "POS-001")
        snapshot = _make_snapshot(
            intra_invocation_changelog=(changelog, analyst_abn, other_abn),
        )
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert len(view.abandoned_openings) == 1
        assert view.abandoned_openings[0].envelope_id == "env-abn-001"
        assert view.abandoned_openings[0].failure_reason == "Guardrail blocked"

    def test_abandoned_openings_empty_when_none(self) -> None:
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view.abandoned_openings == ()


# ---------------------------------------------------------------------------
# Tests: project_analyst_view — empty portfolio
# ---------------------------------------------------------------------------


class TestProjectAnalystViewEmpty:
    def test_empty_held_positions(self) -> None:
        view = project_analyst_view(
            _make_empty_snapshot(),
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view.held_positions == ()
        assert view.pending_orders == ()
        assert view.active_thesis_summaries == ()
        assert view.abandoned_openings == ()

    def test_capital_computed_without_positions(self) -> None:
        view = project_analyst_view(
            _make_empty_snapshot(),
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view.available_capital.available_for_new_positions_usd == 44000.0


class TestAnalystViewMatchesStrategist:
    """Regressions for ALP-549 — analyst saw $5 per-position-max + Held=None
    while strategist/PM saw $4,748 + 8 positions for the same snapshot.
    """

    def test_per_position_max_treats_rule_value_as_pct_of_portfolio(self) -> None:
        """``position_max_size_pct.value`` is a percent (unit="pct"). The
        analyst's USD cap is ``pct/100 * portfolio_value``, not the raw value.
        """
        snapshot = _make_snapshot(
            active_risk_parameters=_make_active_risk_params_with_size_rule(rule_value=5.0),
        )
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view.available_capital.per_position_max_size_pct == pytest.approx(5.0)
        assert view.available_capital.per_position_max_size_usd == pytest.approx(5000.0)

    def test_held_positions_includes_pending(self) -> None:
        """Strategist's view bundles open + pending; the analyst's must too —
        otherwise a snapshot whose positions are still PENDING reads as
        ``Held positions: None`` while the strategist sees the same positions.
        """
        snapshot = _make_snapshot()
        view = project_analyst_view(
            snapshot,
            sector_resolver=_simple_sector_resolver(),
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        position_ids = {p.position_id for p in view.held_positions}
        assert position_ids == {"POS-001", "POS-002", "POS-PEND"}


class TestProjectAnalystViewDeterminism:
    def test_identical_calls_produce_equal_output(self) -> None:
        snapshot = _make_snapshot()
        sector_resolver = _simple_sector_resolver()
        view1 = project_analyst_view(
            snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        view2 = project_analyst_view(
            snapshot,
            sector_resolver=sector_resolver,
            per_position_size_rule_id=_PER_POSITION_RULE_ID,
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE,
        )
        assert view1 == view2
