"""Shared view-level builders for root-level portfolio_state tests.

ALP-814: hoisted + signature-reconciled from test_freshness.py, test_snapshot.py,
test_repository.py, test_assembler.py (root files only).

These build PositionView / PortfolioStateSnapshot / rollup views directly
(for freshness/snapshot tests) or the state objects used in RepositoryFixture
(for assembler/repository).

ALP-821: this module is the single source for the CashLedger/DrawdownState builders
(_make_cash_ledger, _make_drawdown_state) — both tests/portfolio_state/_fixtures.py
and tests/portfolio_state/consumers/_builders.py delegate to them rather than
re-declaring the record literals, so a new field on those records is one edit. It is
also the source for the bracket/order/risk builders that consumers/_builders.py
re-exports unchanged (_make_bracket, _make_pending_order, _make_risk_budget,
_make_active_risk_parameters). _fixtures.py keeps its own richer _make_bracket
(two protective legs) and _make_active_risk_parameters (populated entries) — those are
distinct fixtures, never duplicates of these. consumers/_builders.py also keeps its
own _make_open_position/_make_pending_position because each encodes suite-specific
fixture values its assertions depend on (merging would force assertion changes).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
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
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
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
    PositionFill,
    PositionRecord,
    PositionStatus,
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

# ---------------------------------------------------------------------------
# Shared timestamps (reconciled across origins; entry times precede phase times)
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-test-001"

_ENTRY_AT = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)  # used by position builders
_FILL_COLLECTION_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_NOW = datetime(2025, 6, 1, 9, 0, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Low-level record builders (supporting the rich snapshot builder + validators)
# ---------------------------------------------------------------------------


def _make_open_position(position_id: str = "POS-001", ticker: str = "NVDA") -> PositionView:
    """Reconciled: param name=position_id (was pos_id in one copy); defaults NVDA/AAPL drifted."""
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=500.0,
        borrow_rate_pct=None,
        locate_status=None,
        margin_held_usd=None,
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_AT,
        fill_price=price(500.0),
        fill_quantity=100.0,
        slippage=signed_money(0.0),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_ENTRY_AT,
        details=equity,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(52000.0),
        unrealized_pnl_usd=signed_money(2000.0),
        unrealized_pnl_pct=4.0,
        position_weight_pct=100.0,
        position_age_hours=1.5,
        notional_exposure_usd=money(52000.0),
        delta_adjusted_exposure_usd=signed_money(52000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_pending_position(position_id: str = "PEND-001", ticker: str = "AAPL") -> PositionView:
    """Reconciled: param name=position_id; supports ticker (was fixed GOOG in one copy)."""
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=150.0,
        borrow_rate_pct=None,
        locate_status=None,
        margin_held_usd=None,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
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
        current_market_value_usd=signed_money(1600.0),
        unrealized_pnl_usd=signed_money(100.0),
        unrealized_pnl_pct=6.7,
        position_weight_pct=3.0,
        position_age_hours=0.0,
        notional_exposure_usd=money(1600.0),
        delta_adjusted_exposure_usd=signed_money(1600.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_bracket(bracket_id: str = "BRK-001", position_id: str = "POS-001") -> BracketRecord:
    mechanical_leg = BracketLeg(
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
        protective_legs=(mechanical_leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_pending_order(
    order_id: str = "ORD-001",
    position_id: str = "POS-001",
    status: OrderStatus = OrderStatus.PENDING,
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
        status=status,
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
        summary="Long AAPL on momentum breakout.",
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
        originating_proposal_json={},
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


# ---------------------------------------------------------------------------
# View-level rollup builders (parametrized + reconciled signatures)
# ---------------------------------------------------------------------------


def _make_cash_ledger(
    current_cash_usd: float = 10000.0,
    settled_cash_usd: float | None = None,
    reserved_capital_usd: float = 0.0,
    available_buying_power_usd: float | None = None,
    margin_held_usd: float = 0.0,
    cash_pct_of_portfolio: float = 0.0,
    true_deployable_capital_usd: float = 0.0,
    regt_excess_trailing_30d_usd: float = 0.0,
    regt_excess_trailing_90d_usd: float = 0.0,
    regt_excess_lifetime_usd: float = 0.0,
) -> CashLedger:
    """Reconciled: supports current_cash (for assembler), settled etc for repo/snapshot variants."""
    settled = settled_cash_usd if settled_cash_usd is not None else current_cash_usd
    avail = (
        available_buying_power_usd if available_buying_power_usd is not None else current_cash_usd
    )
    return CashLedger(
        current_cash_usd=current_cash_usd,
        settled_cash_usd=settled,
        reserved_capital_usd=reserved_capital_usd,
        available_buying_power_usd=avail,
        margin_held_usd=margin_held_usd,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=cash_pct_of_portfolio,
        true_deployable_capital_usd=true_deployable_capital_usd,
        regt_excess_trailing_30d_usd=regt_excess_trailing_30d_usd,
        regt_excess_trailing_90d_usd=regt_excess_trailing_90d_usd,
        regt_excess_lifetime_usd=regt_excess_lifetime_usd,
    )


def _make_drawdown_state(
    current_drawdown_pct: float = 0.0,
    equity_high_water_mark_usd: float = 100000.0,
    drawdown_duration_hours: float = 0.0,
    lifetime_max_drawdown_pct: float = 0.0,
    intraday_drawdown_pct: float = 0.0,
    daily_zone: RiskZone = RiskZone.NORMAL,
    cumulative_zone: RiskZone = RiskZone.NORMAL,
    cumulative_tier: DrawdownTier | None = None,
    drawdown_by_source_pct: dict[str, float] | None = None,
) -> DrawdownState:
    """Reconciled: supports current + by_source (assembler), other fields for drifted values."""
    return DrawdownState(
        current_drawdown_pct=current_drawdown_pct,
        equity_high_water_mark_usd=equity_high_water_mark_usd,
        drawdown_duration_hours=drawdown_duration_hours,
        lifetime_max_drawdown_pct=lifetime_max_drawdown_pct,
        intraday_drawdown_pct=intraday_drawdown_pct,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct=drawdown_by_source_pct or {},
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _make_active_risk_parameters(
    regime: RegimeLabel = RegimeLabel.NORMAL,
    transition_state: RegimeTransitionState = RegimeTransitionState.STABLE,
    transition_invocations_remaining: int = 0,
    parameter_change_flag: bool = False,
) -> ActiveRiskParameterSet:
    """Reconciled: supports regime/flag (assembler); unified name from _params/_parameters drift."""
    return ActiveRiskParameterSet(
        regime_label=regime,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        parameter_change_flag=parameter_change_flag,
        entries=(),
        active_overlays=(),
    )


def _make_portfolio_pnl(**overrides: object) -> PortfolioPnL:
    """Non-zero defaults (required by snapshot.py TestPortfolioPnL asserts); supports overrides."""
    kwargs: dict[str, Any] = {
        "total_unrealized_pnl_usd": signed_money(500.0),
        "total_unrealized_pnl_pct_of_portfolio": 1.5,
        "daily_realized_pnl_usd": signed_money(200.0),
        "daily_total_pnl_usd": signed_money(700.0),
        "cumulative_realized_pnl_usd": signed_money(10000.0),
        "rolling_realized_pnl": {
            "1d": signed_money(200.0),
            "3d": signed_money(600.0),
            "5d": signed_money(1000.0),
            "20d": signed_money(3000.0),
        },
        "win_rate_pct": 62.5,
        "average_win_size_usd": signed_money(800.0),
        "average_loss_size_usd": signed_money(400.0),
        "profit_factor": 2.0,
    }
    kwargs.update(overrides)
    return PortfolioPnL(**kwargs)


def _make_directional_exposure(**overrides: object) -> DirectionalExposure:
    """Non-zero defaults (for snapshot TestDirectional asserts); supports overrides."""
    kwargs: dict[str, Any] = {
        "total_long_delta_adjusted_usd": signed_money(50000.0),
        "total_short_delta_adjusted_usd": signed_money(20000.0),
        "net_directional_pct_of_portfolio": 30.0,
        "gross_pct_of_portfolio": 70.0,
    }
    kwargs.update(overrides)
    return DirectionalExposure(**kwargs)


def _make_thesis_quality() -> ThesisQualityAggregate:
    rwc = ResolutionWindowCounts(
        window=TrailingWindow.FIVE_DAYS,
        total_resolutions=10,
        validated=6,
        profitable_but_wrong=1,
        invalidated_stopped_correctly=2,
        invalidated_wrong_on_exit=1,
        cancelled_never_entered=0,
    )
    dur = ThesisDurationStat(
        window=TrailingWindow.FIVE_DAYS,
        mean_actual_to_expected_ratio=1.1,
        median_actual_to_expected_ratio=1.0,
        count=10,
    )
    inv_timing = InvalidationTimingStat(
        window=TrailingWindow.FIVE_DAYS,
        class_distribution={
            InvalidationTimingClass.EARLY: 1,
            InvalidationTimingClass.ON_TIME: 2,
            InvalidationTimingClass.LATE: 0,
        },
        mean_position_age_at_invalidation_hours=5.0,
    )
    shr = SignalHitRate(
        signal_type="options_volume",
        window=TrailingWindow.FIVE_DAYS,
        cited_count=5,
        validated_count=3,
    )
    stc = SignalToThesisConversion(
        signal_type="options_volume",
        window=TrailingWindow.FIVE_DAYS,
        signal_observed_count=8,
        pm_approved_count=5,
    )
    cc = ConvictionCalibrationEntry(
        conviction_level=3,
        window=TrailingWindow.FIVE_DAYS,
        count=5,
        validation_rate=0.6,
        mean_realized_pnl_pct=2.5,
    )
    csd = ConvictionSizingDeviation(
        window=TrailingWindow.FIVE_DAYS,
        total_proposals=5,
        pm_sized_above_advisory_count=2,
        pm_sized_below_advisory_count=1,
        pm_sized_within_advisory_count=2,
        outcome_correlation_above=None,
        outcome_correlation_below=None,
    )
    pa = PerformanceAttributionEntry(
        dimension=AttributionDimension.SECTOR,
        key="Technology",
        window=TrailingWindow.FIVE_DAYS,
        cumulative_realized_pnl_usd=1200.0,
        realized_pnl_pct_of_window_capital=1.2,
        count=3,
    )
    ab = AlphaBetaDecomposition(
        window=TrailingWindow.FIVE_DAYS,
        total_realized_pnl_usd=1200.0,
        market_component_usd=400.0,
        sector_component_usd=200.0,
        alpha_component_usd=600.0,
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


def _make_sector_entry(**overrides: object) -> SectorExposureEntry:
    kwargs: dict[str, Any] = {
        "sector": "TECHNOLOGY",
        "long_delta_adjusted_usd": signed_money(10000.0),
        "short_delta_adjusted_usd": signed_money(5000.0),
        "long_pct_of_portfolio": 20.0,
        "short_pct_of_portfolio": 10.0,
        "long_short_ratio": 2.0,
    }
    kwargs.update(overrides)
    return SectorExposureEntry(**kwargs)


# ---------------------------------------------------------------------------
# Happy-path snapshot builder (rich by default to satisfy snapshot.py tests;
# supports open/pending/phase overrides used by freshness.py)
# ---------------------------------------------------------------------------


def _make_snapshot(**overrides: object) -> PortfolioStateSnapshot:
    """Build a valid PortfolioStateSnapshot with at least 2 open, 1 pending, 1 thesis, 1 order.

    Reconciled from drifted copies: supports the kw overrides used across files
    (open_positions=, pending_positions=, fill_collection_committed_at=,
    snapshot_assembled_at= etc).
    Bare call yields rich data (for snapshot.py); explicit () for positions yields minimal-ish
    (for freshness no-pos cases).
    """
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

    base: dict[str, Any] = {
        "invocation_id": _INV_ID,
        "fill_collection_committed_at": _T0,
        "snapshot_assembled_at": _T1,
        "pipeline_invocation_started_at": _T2,
        "open_positions": (pos1, pos2),
        "pending_positions": (pos3,),
        "sector_exposure": (_make_sector_entry(),),
        "directional_exposure": _make_directional_exposure(),
        "portfolio_pnl": _make_portfolio_pnl(),
        "drawdown": _make_drawdown_state(),
        "active_theses": (thesis1,),
        "recent_thesis_resolutions": (),
        "cash_ledger": _make_cash_ledger(),
        "pending_orders": (order1,),
        "risk_budget": _make_risk_budget(),
        "active_risk_parameters": _make_active_risk_parameters(),
        "intra_invocation_changelog": (changelog_entry,),
        "recent_pm_decision_log": (pm_entry,),
        "position_modification_trail": {"POS-001": (mod_trail_entry,)},
        "thesis_quality_aggregates": _make_thesis_quality(),
        "brackets": (brk1, brk2, brk3),
    }
    # When caller overrides positions (incl. to () for minimal/empty cases from freshness),
    # clear dependent rich data (brackets etc that ref positions) and pipeline (to avoid
    # cross-era timestamp ordering violations when overriding times).
    if "open_positions" in overrides or "pending_positions" in overrides:
        base["open_positions"] = overrides.get("open_positions", ())
        base["pending_positions"] = overrides.get("pending_positions", ())
        base["brackets"] = ()
        base["pending_orders"] = ()
        base["active_theses"] = ()
        base["recent_thesis_resolutions"] = ()
        base["intra_invocation_changelog"] = ()
        base["recent_pm_decision_log"] = ()
        base["position_modification_trail"] = {}
        base["sector_exposure"] = ()
        if "snapshot_assembled_at" in overrides or "fill_collection_committed_at" in overrides:
            base["pipeline_invocation_started_at"] = None
    base.update(overrides)
    return PortfolioStateSnapshot(**base)
