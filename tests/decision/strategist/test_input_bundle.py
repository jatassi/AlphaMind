"""Tests for the strategist input-bundle assembler — story 04 (ALP-304)."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
from alphamind.decision.strategist.input_bundle import (
    _render_pending_order_row,
    assemble_input_bundle_defensive_posture,
    assemble_input_bundle_normal,
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
from alphamind.portfolio_state.consumers.analyst import AnalystAbandonedOpening
from alphamind.portfolio_state.consumers.strategist import (
    BetweenInvocationClosure,
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    BracketModificationSource,
    BracketModifiedDetail,
    DistillationConfigChange,
    DistillationConfigChangeDetail,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
    GreeksRefreshFailedDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
    PMDecisionDetail,
    PMVerdict,
    PositionExitMethod,
    ReconciliationAlertDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    EventTrigger,
    InstrumentSpec,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
    StrategyInstrumentSpec,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation.types import EscalationZones
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALIDATE_GUARDRAIL_TOOL = "mcp__alphamind_decision_validation__validate_guardrail"
_RETRIEVE_BRIEF_TOOL = "mcp__alphamind_synthesizer_retrieval__retrieve_brief"
_TOOL_NAMES: tuple[str, ...] = (_VALIDATE_GUARDRAIL_TOOL, _RETRIEVE_BRIEF_TOOL)

_SYNTHESIZER_BRIEF = (
    "## Cross-domain market snapshot\n\n"
    "Tech tape mixed [SA-TECH-3]; financials grinding higher [SA-FIN-1]; "
    "energy quiet ahead of inventory print [SA-ENERGY-2]."
)

_INVOCATION_ID = "inv-strat-001"
_TIMESTAMP = datetime(2026, 5, 4, 14, 32, 5, tzinfo=UTC)
_ENTRY_TIMESTAMP = datetime(2026, 5, 3, 14, 0, 0, tzinfo=UTC)
_ABANDONED_TIMESTAMP = datetime(2026, 5, 3, 13, 30, 0, tzinfo=UTC)

_TOTAL_PORTFOLIO_VALUE_USD = 1_000_000.0
_AVAILABLE_FOR_NEW_POSITIONS_USD = 200_000.0
_PER_POSITION_MAX_PCT = 5.0

_SECTOR_LABELS = {
    "tech": "Tech",
    "semis": "Semis",
    "financials": "Financials",
    "energy": "Energy",
}


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_DEFAULT_POSITION_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _make_state_delivery_config() -> StateDeliveryConfig:
    return StateDeliveryConfig(
        recent_engine_actions_lookback_invocations=3,
        correlation_state_min_position_count=4,
        dependency_risk_flag_min_position_count=2,
        abandoned_window_lookback_invocations=1,
    )


def _make_budget_entry(
    *,
    rule_id: str,
    rule_label: str,
    current_value: float,
    limit_value: float,
    zone: RiskZone = RiskZone.NORMAL,
    unit: str = "% of portfolio",
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    headroom_pct = max(0.0, min(100.0, (headroom / limit_value) * 100.0)) if limit_value else 0.0
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=zone,
        unit=unit,
        cumulative_invocation_impact_value=0.0,
    )


def _make_param_entry(
    *,
    rule_id: str,
    rule_label: str,
    value: float,
    unit: str = "% of portfolio",
) -> ActiveRiskParameterEntry:
    return ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_label,
        value=value,
        unit=unit,
        regime_multiplier_applied=1.0,
        base_value=value,
    )


def _make_active_risk_parameters() -> ActiveRiskParameterSet:
    """Active parameter set with all rules required by the strategist header renderer."""
    from tests.decision.conftest import compose_active_risk_parameters_via_orchestrator

    entries = (
        _make_param_entry(
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            value=_PER_POSITION_MAX_PCT,
        ),
        _make_param_entry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            value=2.5,
        ),
        _make_param_entry(
            rule_id="cumulative_drawdown_pct",
            rule_label="Cumulative drawdown",
            value=10.0,
        ),
    )
    return compose_active_risk_parameters_via_orchestrator(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    """Risk budget covering active sectors plus directional rules."""
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            current_value=18.5,
            limit_value=25.0,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_semis",
            rule_label="Semis sector concentration",
            current_value=12.0,
            limit_value=25.0,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_financials",
            rule_label="Financials sector concentration",
            current_value=8.5,
            limit_value=25.0,
        ),
        _make_budget_entry(
            rule_id="sector_concentration_energy",
            rule_label="Energy sector concentration",
            current_value=4.0,
            limit_value=25.0,
        ),
        _make_budget_entry(
            rule_id="net_long_pct",
            rule_label="Net long exposure",
            current_value=42.0,
            limit_value=60.0,
        ),
        _make_budget_entry(
            rule_id="gross_exposure_pct",
            rule_label="Gross exposure",
            current_value=78.0,
            limit_value=120.0,
        ),
    )
    return RiskBudgetConsumption(entries=entries)


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=money(8200.0),
        total_unrealized_pnl_pct_of_portfolio=0.82,
        daily_realized_pnl_usd=money(300.0),
        daily_total_pnl_usd=money(1200.0),
        cumulative_realized_pnl_usd=money(10000.0),
        rolling_realized_pnl={
            "1d": money(300.0),
            "3d": money(600.0),
            "5d": money(1200.0),
            "20d": money(3000.0),
        },
        win_rate_pct=55.0,
        average_win_size_usd=money(200.0),
        average_loss_size_usd=money(150.0),
        profit_factor=1.4,
    )


def _make_drawdown() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=2.0,
        equity_high_water_mark_usd=1_010_000.0,
        drawdown_duration_hours=8.0,
        lifetime_max_drawdown_pct=10.0,
        intraday_drawdown_pct=0.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(420_000.0),
        total_short_delta_adjusted_usd=money(0.0),
        net_directional_pct_of_portfolio=42.0,
        gross_pct_of_portfolio=78.0,
    )


def _make_position_record(
    *,
    position_id: str = "POS-NVDA-001",
    ticker: str = "NVDA",
    weight_pct: float = 4.5,
    unrealized_pnl_pct: float = 5.0,
    unrealized_pnl_usd: float = 8200.0,
    age_hours: float = 36.4,
    share_count: float = 200.0,
    avg_cost: float = 800.0,
) -> PositionView:
    market_value = weight_pct * _TOTAL_PORTFOLIO_VALUE_USD / 100.0
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=avg_cost,
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_TIMESTAMP,
        fill_price=price(avg_cost),
        fill_quantity=share_count,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_ENTRY_TIMESTAMP,
        details=equity,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(market_value),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=weight_pct,
        position_age_hours=age_hours,
        notional_exposure_usd=money(market_value),
        delta_adjusted_exposure_usd=signed_money(market_value),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_strategy_position_view(
    *,
    position_id: str = "POS-SPY-STRAT-001",
    strategy_type_label: str = "bull_call_spread",
) -> PositionView:
    """Build a held multi-leg strategy ``PositionView`` for direction-read tests.

    A strategy carries no position-level direction (``position_direction()``
    returns ``None``); the renderers must show *strategy_type_label* instead and
    omit the R/R figure.
    """
    leg_long = StrategyLeg(
        leg_id="leg-long-call",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("SPY"),
            strike_price=520.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=10.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=4.20,
            greeks=OptionGreeks(delta=0.45, gamma=0.04, theta=-0.03, vega=0.20),
        ),
    )
    leg_short = StrategyLeg(
        leg_id="leg-short-call",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("SPY"),
            strike_price=530.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=10.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.10,
            greeks=OptionGreeks(delta=0.30, gamma=0.03, theta=-0.025, vega=0.18),
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label=strategy_type_label,
        legs=(leg_long, leg_short),
        net_premium_usd=2100.0,
        max_profit_usd=7900.0,
        max_loss_usd=2100.0,
        breakeven_levels=(522.10,),
        strategy_greeks=OptionGreeks(delta=0.15, gamma=0.01, theta=-0.005, vega=0.02),
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_TIMESTAMP,
        fill_price=price(2.10),
        fill_quantity=10.0,
        slippage=signed_money(0.03),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_ENTRY_TIMESTAMP,
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(21_000.0),
        unrealized_pnl_usd=signed_money(500.0),
        unrealized_pnl_pct=2.4,
        position_weight_pct=4.5,
        position_age_hours=12.0,
        notional_exposure_usd=money(21_000.0),
        delta_adjusted_exposure_usd=signed_money(3_000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_thesis(
    *,
    thesis_id: str = "TH-NVDA-001",
    position_id: str = "POS-NVDA-001",
) -> ThesisRecord:
    def _comp(ctype: ThesisComponentType, cid: str, narrative: str) -> ThesisComponent:
        return ThesisComponent(
            component_id=cid,
            thesis_id=ThesisId(thesis_id),
            component_type=ctype,
            linked_bracket_leg_type=None,
            instrument_reference="NVDA",
            narrative=narrative,
            key_assumptions=(
                KeyAssumption(
                    text="Microsoft Q1 capex guide >= $24B (vs. consensus $22.8B)",
                    outcome=None,
                ),
                KeyAssumption(text="AI demand persistence", outcome=None),
            ),
            generation_timestamp=_ENTRY_TIMESTAMP,
            resolution_outcome=None,
            resolution_notes=None,
        )

    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Hyperscaler capex acceleration drives Q1 revenue beat...",
        components=(
            _comp(
                ThesisComponentType.ENTRY_RATIONALE,
                "comp-entry",
                "Entry: capex acceleration thesis",
            ),
            _comp(
                ThesisComponentType.TARGET_RATIONALE,
                "comp-target",
                "Target: Q1 print fully prices in",
            ),
            _comp(
                ThesisComponentType.INVALIDATION_RATIONALE,
                "comp-inv",
                "Invalidation: MSFT guides AI capex lower than consensus",
            ),
        ),
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_ENTRY_TIMESTAMP,
        time_expectation_hours=48.0,
        age_hours=36.4,
        expected_resolution_at=datetime(2026, 5, 5, 14, 0, 0, tzinfo=UTC),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="MSFT Q1 capex guide",
    )


def _make_bracket(
    *,
    bracket_id: str = "BRK-NVDA-001",
    position_id: str = "POS-NVDA-001",
) -> BracketRecord:
    target_leg = BracketLeg(
        leg_id="leg-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId("ord-target"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=189.00, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    stop_leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=167.00, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    time_leg = BracketLeg(
        leg_id="leg-time",
        leg_type=BracketLegType.TIME_EXPIRATION,
        order_id=OrderId("ord-time"),
        trigger=TimeTrigger(deadline=datetime(2026, 4, 25, 16, 0, tzinfo=UTC)),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    event_leg = BracketLeg(
        leg_id="leg-event",
        leg_type=BracketLegType.EVENT_INVALIDATION,
        order_id=None,
        trigger=EventTrigger(description="MSFT guides AI capex lower than consensus"),
        enforcement=BracketLegEnforcement.ADVISORY,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(target_leg, stop_leg, time_leg, event_leg),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_pending_order(
    *,
    order_id: str = "ORD-LIMIT-4",
    position_id: str = "POS-NVDA-001",
    ticker: str = "NVDA",
    limit_price: float = 380.0,
    age_hours: float = 36.0,
    direction: OrderDirection | None = OrderDirection.BUY,
    instrument_spec: InstrumentSpec | None = None,
    order_class: OrderClass = OrderClass.SIMPLE,
) -> OrderRecord:
    spec = instrument_spec or EquityInstrumentSpec(ticker=Symbol(ticker))
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId("BRK-NVDA-001"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=direction,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(
            limit_price=price(str(limit_price)), stop_trigger_price=None
        ),
        quantity=10.0,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-1"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-1"),),
        submission_timestamp=_ENTRY_TIMESTAMP,
        last_update_timestamp=_ENTRY_TIMESTAMP,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=age_hours,
        order_class=order_class,
    )


def _make_modification_trail_entry(
    *,
    entry_id: str = "ALE-MOD-1",
    position_id: str = "POS-NVDA-001",
) -> ActivityLogEntry:
    detail = BracketModifiedDetail(
        source=BracketModificationSource.PM,
        field_changed="stop_price",
        old_value="165.00",
        new_value="167.00",
        rationale="PM tightened stop on at-risk thesis",
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_ENTRY_TIMESTAMP,
        event_type=EventType.BRACKET_MODIFIED,
        event_group=EventGroup.BRACKET,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.BRACKET_MANAGER,
        detail=detail,
    )


def _make_pm_decision_entry(
    *,
    entry_id: str = "ALE-PM-1",
    envelope_id: str = "ENV-1",
) -> ActivityLogEntry:
    detail = PMDecisionDetail(
        envelope_id=envelope_id,
        source_provenance_json={},
        evaluation_json={"rationale": "approved on conviction"},
        modifications_json=[],
        resulting_command_ids=("cmd-1",),
        verdict=PMVerdict.APPROVE,
        originating_proposal_json={},
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_ENTRY_TIMESTAMP,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def _make_intra_invocation_changelog_entry(
    *,
    entry_id: str = "ALE-CHG-1",
    position_id: str = "POS-NVDA-001",
) -> ActivityLogEntry:
    detail = BracketModifiedDetail(
        source=BracketModificationSource.PM,
        field_changed="target_price",
        old_value="185.00",
        new_value="189.00",
        rationale="PM raised target on momentum",
    )
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        event_type=EventType.BRACKET_MODIFIED,
        event_group=EventGroup.BRACKET,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.BRACKET_MANAGER,
        detail=detail,
    )


def _make_position_view(
    *,
    position_id: str = "POS-NVDA-001",
    ticker: str = "NVDA",
    with_thesis: bool = True,
    with_bracket: bool = True,
    with_pending_orders: bool = True,
    with_modification_trail: bool = True,
) -> StrategistPositionView:
    return StrategistPositionView(
        position=_make_position_record(position_id=position_id, ticker=ticker),
        thesis=_make_thesis(position_id=position_id) if with_thesis else None,
        bracket=(
            _make_bracket(position_id=position_id, bracket_id=BracketId(f"BRK-{position_id}"))
            if with_bracket
            else None
        ),
        pending_orders=(
            (_make_pending_order(position_id=position_id, ticker=ticker),)
            if with_pending_orders
            else ()
        ),
        modification_trail=(
            (_make_modification_trail_entry(position_id=position_id),)
            if with_modification_trail
            else ()
        ),
    )


def _make_strategist_view(
    *,
    positions: tuple[StrategistPositionView, ...] | None = None,
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    abandoned_openings: tuple[AnalystAbandonedOpening, ...] = (),
    abandoned_actions: tuple[StrategistAbandonedAction, ...] = (),
    between_invocation_closures: tuple[BetweenInvocationClosure, ...] = (),
) -> StrategistView:
    if positions is None:
        positions = (_make_position_view(),)
    return StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        risk_budget=_make_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=recent_pm_decision_log,
        abandoned_openings=abandoned_openings,
        abandoned_actions=abandoned_actions,
        between_invocation_closures=between_invocation_closures,
    )


def _sector_resolver(position: PositionRecord) -> str | None:
    sector_by_ticker = {
        "NVDA": "tech",
        "AAPL": "tech",
        "AMD": "semis",
    }
    if isinstance(position.details, EquityPositionDetails):
        return sector_by_ticker.get(position.details.ticker)
    return None


def _current_price_lookup(ticker: str) -> float:
    """Return a deterministic 'current price' for known tickers; raise KeyError otherwise."""
    prices = {
        "NVDA": 862.0,  # avg cost 800 → +7.75% above avg
        "AAPL": 175.0,
        "AMD": 145.0,
        "SPY": 525.0,
    }
    return prices[ticker]


def _make_halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )


def _normal_kwargs(
    *,
    strategist_view: StrategistView | None = None,
) -> dict[str, Any]:
    return {
        "strategist_view": strategist_view or _make_strategist_view(),
        "invocation_id": _INVOCATION_ID,
        "timestamp": _TIMESTAMP,
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis", "financials", "energy"),
        "state_delivery_config": _make_state_delivery_config(),
        "sector_resolver": _sector_resolver,
        "total_portfolio_value_usd": money(_TOTAL_PORTFOLIO_VALUE_USD),
        "available_for_new_positions_usd": money(_AVAILABLE_FOR_NEW_POSITIONS_USD),
        "current_price_lookup": _current_price_lookup,
        "synthesizer_brief_text": _SYNTHESIZER_BRIEF,
        "tool_names": _TOOL_NAMES,
        "position_zones": _DEFAULT_POSITION_ZONES,
    }


def _defensive_kwargs(
    *,
    strategist_view: StrategistView | None = None,
) -> dict[str, Any]:
    kwargs = _normal_kwargs(strategist_view=strategist_view)
    kwargs["halt_state"] = _make_halt_state()
    return kwargs


# ---------------------------------------------------------------------------
# Golden full-render — normal mode with representative fixture
# ---------------------------------------------------------------------------

_NORMAL_GOLDEN = (
    "=== GUARDRAIL STATE (invocation inv-strat-001, 2026-05-04T14:32:05Z) ===\n"
    "Regime: normal [unchanged]\n"
    "\n"
    "Capital:\n"
    "  Available for new positions: $200,000 (20.0% of portfolio)\n"
    "  Per-position max size: $50,000 (5.0% of portfolio, normal regime)\n"
    "\n"
    "Sector headroom (delta-adjusted):\n"
    "  Tech:       18.5% / 25.0% — room: 6.5% [NORMAL]\n"
    "  Semis:      12.0% / 25.0% — room: 13.0% [NORMAL]\n"
    "  Financials: 8.5% / 25.0% — room: 16.5% [NORMAL]\n"
    "  Energy:     4.0% / 25.0% — room: 21.0% [NORMAL]\n"
    "\n"
    "Directional headroom:\n"
    "  Net long:  42.0% / 60.0% — room: 18.0%\n"
    "  Gross:     78.0% / 120.0% — room: 42.0%\n"
    "\n"
    "Position-level constraint proximity:\n"
    "  POS-NVDA-001:  4.5% of portfolio (max 5.0%) — P/L: +5.0% of cost [\U0001f534 CRITICAL: size]\n"  # noqa: E501
    "\n"
    "Sector exposure breakdown (per position):\n"
    "  Tech (18.5% / 25.0%):\n"
    "    POS-NVDA-001: 4.5% (delta-adj)\n"
    "  Semis (12.0% / 25.0%):\n"
    "    (no positions)\n"
    "  Financials (8.5% / 25.0%):\n"
    "    (no positions)\n"
    "  Energy (4.0% / 25.0%):\n"
    "    (no positions)\n"
    "\n"
    "Drawdown state:\n"
    "  Daily:      0.5% / 2.5% [NORMAL]\n"
    "  Cumulative: 2.0% / 10.0% [NORMAL]\n"
    "\n"
    "Abandoned openings from prior invocation (portfolio awareness; analyst owns re-evaluation):\n"
    "  None\n"
    "\n"
    "Abandoned position actions from prior invocation (decide on current grounds whether to re-propose):\n"  # noqa: E501
    "  None\n"
    "\n"
    "Hard blocks (do NOT recommend):\n"
    "  Options: DISABLED for this portfolio\n"
    "  Short selling: DISABLED for this portfolio\n"
    "===\n"
    "\n"
    "=== AVAILABLE TOOLS ===\n"
    "- mcp__alphamind_decision_validation__validate_guardrail\n"
    "- mcp__alphamind_synthesizer_retrieval__retrieve_brief\n"
    "\n"
    "=== PORTFOLIO STATE ===\n"
    "\n"
    "Aggregate:\n"
    "  Portfolio P/L: intraday $1,200, cumulative realized $10,000\n"
    "  Trade stats: win rate: 55.0%; profit factor: 1.40; avg win: $200; avg loss: $150\n"
    "  Drawdown: daily 0.5% [NORMAL], cumulative 2.0% [NORMAL]\n"
    "  Net long: $420,000 (42.0% of portfolio)\n"
    "  Gross:    $420,000 (78.0% of portfolio)\n"
    "\n"
    "Per-position records:\n"
    "\n"
    "POS-NVDA-001\n"
    "  Underlying:    NVDA (instrument: equity, direction: long)\n"
    "  Size:          200 shares  $45,000  (4.5% of portfolio)\n"
    "  P/L:           +$8,200 since open (+5.0%)\n"
    "  Age:           36.4 hours (placed 2026-05-03T14:00:00Z)\n"
    "  Distance:      target -78.1%  /  stop -80.6%  /  R/R at-current -1.0:1\n"
    "  Bracket:\n"
    "    target: NVDA GTE $189.0\n"
    "    stop: NVDA LTE $167.0\n"
    "    time deadline: 2026-04-25T16:00:00+00:00\n"
    '    event invalidation: "MSFT guides AI capex lower than consensus"\n'
    "  Thesis (TH-NVDA-001):\n"
    '    Summary:    "Hyperscaler capex acceleration drives Q1 revenue beat..."\n'
    "    Entry rationale: Entry: capex acceleration thesis\n"
    "    Target rationale: Target: Q1 print fully prices in\n"
    "    Invalidation: Invalidation: MSFT guides AI capex lower than consensus\n"
    "    Key assumptions:\n"
    '      - "Microsoft Q1 capex guide >= $24B (vs. consensus $22.8B)"\n'
    '      - "AI demand persistence"\n'
    "  Pending orders for this position:\n"
    "    ORD-LIMIT-4: ENTRY @ $380.00, age 36.0h, 55.9% from market — away from fill / low fill-likelihood\n"  # noqa: E501
    "  Modification trail (recent):\n"
    "  [2026-05-03T14:00:00Z] BRACKET_MODIFIED: stop_price: 165.00 → 167.00 (PM tightened stop on at-risk thesis)  (position=POS-NVDA-001)\n"  # noqa: E501
    "\n"
    "=== ACTIVITY LOG (between-invocation closures) ===\n"
    "  None\n"
    "\n"
    "=== ACTIVITY LOG (intra-invocation) ===\n"
    "  None\n"
    "\n"
    "=== ACTIVITY LOG (recent PM decisions) ===\n"
    "  None\n"
    "\n"
    "=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ===\n"
    "## Cross-domain market snapshot\n"
    "\n"
    "Tech tape mixed [SA-TECH-3]; financials grinding higher [SA-FIN-1]; "
    "energy quiet ahead of inventory print [SA-ENERGY-2]."
)


def test_normal_mode_full_golden_render() -> None:
    """One exact pin of the full normal-mode render against the representative fixture.

    Subsumes per-section substring assertions: section ordering, envelope header,
    brief tail, tool reminder, per-position P/L / age / distance / R/R, thesis block,
    pending-order row, modification trail, aggregate block, activity-log empty sections.
    When this test breaks a reviewer knows *exactly* what changed.
    """
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    assert out == _NORMAL_GOLDEN


# ---------------------------------------------------------------------------
# Tool reminder — defensive-posture branches
# ---------------------------------------------------------------------------


def test_defensive_posture_tool_reminder_surfaces_both_tools() -> None:
    """Strategist still uses validate_guardrail in defensive_posture mode (parent decision (I))."""
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    assert f"- {_VALIDATE_GUARDRAIL_TOOL}" in out
    assert f"- {_RETRIEVE_BRIEF_TOOL}" in out


def test_defensive_posture_tool_reminder_includes_posture_note() -> None:
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    tool_section = out[tools_idx:portfolio_idx]
    assert "defensive posture" in tool_section.lower()
    assert "add" in tool_section.lower()


def test_normal_mode_tool_reminder_omits_posture_note() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    tool_section = out[tools_idx:portfolio_idx]
    assert "defensive posture" not in tool_section.lower()


# ---------------------------------------------------------------------------
# Defensive-posture mode — banner from halt-mode header is present
# ---------------------------------------------------------------------------


def test_defensive_posture_bundle_includes_halt_banner() -> None:
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in out
    assert "DEFENSIVE POSTURE" in out


def test_defensive_posture_section_ordering() -> None:
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    envelope_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ===")
    assert envelope_idx < tools_idx < portfolio_idx < brief_idx


# ---------------------------------------------------------------------------
# Per-position derived fields — conditional branches only
# (P/L absolute/pct, age, distance, R/R for normal equity case are pinned by the golden render)
# ---------------------------------------------------------------------------


def test_held_strategy_position_renders_strategy_type_label() -> None:
    """A held multi-leg strategy renders without a fabricated long/short.

    ``position_direction()`` returns ``None`` for a strategy; the underlying
    line shows the strategy-type label in place of LONG/SHORT.
    """
    strategy_view = StrategistPositionView(
        position=_make_strategy_position_view(),
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )
    view = _make_strategist_view(positions=(strategy_view,))
    out = assemble_input_bundle_normal(**{**_normal_kwargs(), "strategist_view": view})
    assert "direction: bull_call_spread" in out
    assert "instrument: strategy" in out
    assert "direction: long" not in out
    assert "direction: short" not in out


def test_held_strategy_position_omits_risk_reward() -> None:
    """A strategy has no position-level direction, so R/R at-current is omitted.

    The strategy still gets a bracket with target / stop legs; ``_risk_reward``
    must return ``None`` (rendered ``—``) rather than fabricating a direction.
    """
    strategy_view = StrategistPositionView(
        position=_make_strategy_position_view(),
        thesis=None,
        bracket=_make_bracket(bracket_id="BRK-SPY-STRAT-001", position_id="POS-SPY-STRAT-001"),
        pending_orders=(),
        modification_trail=(),
    )
    view = _make_strategist_view(positions=(strategy_view,))
    out = assemble_input_bundle_normal(**{**_normal_kwargs(), "strategist_view": view})
    assert "R/R at-current —" in out


# ---------------------------------------------------------------------------
# Thesis block rendering — conditional branches
# (full-thesis render for the populated case is pinned by the golden render)
# ---------------------------------------------------------------------------


def test_thesis_block_renders_prior_status_from_snapshot() -> None:
    """Prior status renders from a passed-in ThesisHealthSnapshot (story ALP-351)."""
    from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot

    prior_snap = ThesisHealthSnapshot(
        thesis_id=ThesisId("TH-NVDA-001"),
        invocation_id="prior-inv-000",
        snapshot_timestamp=_TIMESTAMP,
        health_status=ThesisStatus.AT_RISK,
        prior_health_status=None,
        component_health=(),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),
        sector_label_display=_SECTOR_LABELS,
        prior_health_snapshots=(prior_snap,),
    )
    assert "Prior status: AT_RISK" in out


def test_thesis_block_omits_prior_status_when_no_snapshot() -> None:
    """When no prior snapshot is supplied, no Prior-status line is rendered."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Prior status:" not in out


def test_position_without_thesis_renders_pending_marker() -> None:
    view = _make_strategist_view(positions=(_make_position_view(with_thesis=False),))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Thesis: NONE — pending position" in out


def test_position_without_bracket_renders_none_marker() -> None:
    view = _make_strategist_view(positions=(_make_position_view(with_bracket=False),))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Bracket: none" in out


# ---------------------------------------------------------------------------
# Activity-log surfaces — three sections
# ---------------------------------------------------------------------------


def test_intra_invocation_changelog_section_present() -> None:
    view = _make_strategist_view(
        intra_invocation_changelog=(_make_intra_invocation_changelog_entry(),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "=== ACTIVITY LOG (intra-invocation) ===" in out
    assert "PM raised target on momentum" in out


def _make_monitor_entry(
    *, entry_id: str, event_type: EventType, detail: object
) -> ActivityLogEntry:
    """Build an ActivityLogEntry for a continuous-monitor RISK_AND_GUARDRAIL event."""
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        event_type=event_type,
        event_group=EventGroup.RISK_AND_GUARDRAIL,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=detail,
    )


def test_halt_activated_summary_surfaces_drawdown_and_limit() -> None:
    """``HALT_ACTIVATED`` entries render with halt_type + drawdown + limit."""
    entry = _make_monitor_entry(
        entry_id="ALE-HALT-1",
        event_type=EventType.HALT_ACTIVATED,
        detail=HaltActivatedDetail(
            halt_type="daily_drawdown",
            current_drawdown_pct=0.045,
            limit_pct=0.04,
            detected_at=_TIMESTAMP,
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "daily_drawdown halt activated at drawdown=4.50% limit=4.00%" in out
    assert "HaltActivatedDetail" not in out  # not the bare class name


def test_halt_lifted_summary_surfaces_halt_type_and_drawdown() -> None:
    """``HALT_LIFTED`` entries render with halt_type + current drawdown."""
    entry = _make_monitor_entry(
        entry_id="ALE-HALT-2",
        event_type=EventType.HALT_LIFTED,
        detail=HaltLiftedDetail(
            halt_type="cumulative_drawdown_tier3",
            current_drawdown_pct=0.08,
            lifted_at=_TIMESTAMP,
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "cumulative_drawdown_tier3 halt lifted at drawdown=8.00%" in out
    assert "HaltLiftedDetail" not in out


def test_greeks_refresh_failed_summary_surfaces_symbol_and_reason() -> None:
    """``GREEKS_REFRESH_FAILED`` entries render with occ_symbol + failure_reason."""
    entry = _make_monitor_entry(
        entry_id="ALE-GRF-1",
        event_type=EventType.GREEKS_REFRESH_FAILED,
        detail=GreeksRefreshFailedDetail(
            underlying_ticker=Symbol("AAPL"),
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_no_row",
            prior_as_of=_TIMESTAMP,
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "greeks refresh failed: O:AAPL260619C00200000 (iv_fetch_no_row)" in out
    assert "GreeksRefreshFailedDetail" not in out


def test_emergency_invocation_requested_summary_surfaces_trigger() -> None:
    """``EMERGENCY_INVOCATION_REQUESTED`` entries render with trigger_type + reason."""
    entry = _make_monitor_entry(
        entry_id="ALE-EMT-1",
        event_type=EventType.EMERGENCY_INVOCATION_REQUESTED,
        detail=EmergencyInvocationRequestedDetail(
            trigger_type="regime_jump",
            trigger_reason="NORMAL → CRISIS",
            cooldown_remaining_seconds=0,
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "emergency invocation requested: regime_jump — NORMAL → CRISIS" in out
    assert "EmergencyInvocationRequestedDetail" not in out


def test_reconciliation_alert_summary_surfaces_sources_and_delta() -> None:
    """``RECONCILIATION_ALERT`` entries surface domain, field, local↔alpaca, and delta."""
    entry = ActivityLogEntry(
        entry_id="ALE-REC-1",
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        event_type=EventType.RECONCILIATION_ALERT,
        event_group=EventGroup.RECONCILIATION,
        position_id="debug-pos-07",
        order_id=None,
        thesis_id=None,
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
        detail=ReconciliationAlertDetail(
            domain="position",
            field_name="share_count",
            local_value=100.0,
            alpaca_value=99.5,
            delta_description="MSFT: local share_count=100.0 vs Alpaca qty=99.5",
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "ReconciliationAlertDetail" not in out
    assert "position share_count mismatch" in out
    assert "local=100.0 vs alpaca=99.5" in out
    assert "MSFT: local share_count=100.0 vs Alpaca qty=99.5" in out
    assert "position=debug-pos-07" in out


def _make_reconciliation_alert(
    *,
    position_id: str | None,
    entry_id: str = "ALE-REC-PNL",
) -> ActivityLogEntry:
    """Build a RECONCILIATION_ALERT entry for a share-count mismatch."""
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        event_type=EventType.RECONCILIATION_ALERT,
        event_group=EventGroup.RECONCILIATION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
        detail=ReconciliationAlertDetail(
            domain="position",
            field_name="share_count",
            local_value=200.0,
            alpaca_value=199.0,
            delta_description="NVDA: local share_count=200.0 vs Alpaca qty=199.0",
        ),
    )


def _pnl_line(rendered: str) -> str:
    # "since open" is unique to the per-position record's P/L line, isolating
    # it from the state-delivery header's per-position constraint summary.
    line = next((ln for ln in rendered.splitlines() if "since open" in ln), None)
    assert line is not None, "no per-position P/L line found in rendered bundle"
    return line


def test_pnl_line_annotated_when_position_flagged_by_reconciliation_alert() -> None:
    """ALP-582: a position named by a RECONCILIATION_ALERT carries an
    unreliable-pending-reconciliation marker on its P/L line."""
    view = _make_strategist_view(
        intra_invocation_changelog=(_make_reconciliation_alert(position_id="POS-NVDA-001"),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    pnl_line = _pnl_line(out)
    assert "unreliable" in pnl_line.lower()
    assert "reconciliation" in pnl_line.lower()


def test_pnl_line_not_annotated_without_reconciliation_alert() -> None:
    """ALP-582: a position with no RECONCILIATION_ALERT keeps a clean P/L line."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "unreliable" not in _pnl_line(out).lower()


def test_pnl_line_not_annotated_for_position_id_less_reconciliation_alert() -> None:
    """ALP-582: an `alpaca_only_position` alert carries no position_id (there is
    no matching local position) — it must not annotate an unrelated local
    position that merely shares the underlying ticker."""
    view = _make_strategist_view(
        intra_invocation_changelog=(_make_reconciliation_alert(position_id=None),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "unreliable" not in _pnl_line(out).lower()


def test_distillation_config_change_summary_surfaces_key_old_new() -> None:
    """``DISTILLATION_CONFIG_CHANGE`` entries surface the key_path and old/new values."""
    detail = DistillationConfigChangeDetail(
        prior_hash="b" * 64,
        new_hash="a" * 64,
        changes=(
            DistillationConfigChange(
                key_path="anomaly_detection.volume_anomaly_sigma",
                old_value=2.0,
                new_value=2.5,
            ),
        ),
        git_sha="abc1234",
    )
    entry = ActivityLogEntry(
        entry_id="ALE-CFG-1",
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        event_type=EventType.DISTILLATION_CONFIG_CHANGE,
        event_group=EventGroup.CONFIGURATION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.CONFIG_RELOAD,
        detail=detail,
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "DistillationConfigChangeDetail" not in out
    assert "anomaly_detection.volume_anomaly_sigma: 2.0 → 2.5" in out


def test_pm_decision_log_section_present() -> None:
    view = _make_strategist_view(
        recent_pm_decision_log=(_make_pm_decision_entry(),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "=== ACTIVITY LOG (recent PM decisions) ===" in out
    assert "ENV-1" in out
    assert "approve" in out.lower()


# ---------------------------------------------------------------------------
# Missing current-price lookup raises ValueError
# ---------------------------------------------------------------------------


def test_missing_current_price_raises_value_error_with_ticker_and_position_id() -> None:
    def _empty_lookup(ticker: str) -> float:
        raise KeyError(ticker)

    kwargs = _normal_kwargs()
    kwargs["current_price_lookup"] = _empty_lookup
    with pytest.raises(ValueError) as excinfo:
        assemble_input_bundle_normal(
            **kwargs,
            sector_label_display=_SECTOR_LABELS,
        )
    msg = str(excinfo.value)
    assert "NVDA" in msg
    assert "POS-NVDA-001" in msg


# ---------------------------------------------------------------------------
# Aggregate-PnL deferred-field rendering (ALP-654) — None branch
# (populated-metrics render is pinned by the golden render)
# ---------------------------------------------------------------------------


def test_aggregate_pnl_block_frames_deferred_metrics_explicitly() -> None:
    """When the feedback-loop metrics are None (deferred), the Aggregate
    block names each by name with explicit "not yet computed" framing
    instead of silently omitting them."""
    deferred_pnl = PortfolioPnL(
        total_unrealized_pnl_usd=money(8200.0),
        total_unrealized_pnl_pct_of_portfolio=0.82,
        daily_realized_pnl_usd=money(300.0),
        daily_total_pnl_usd=money(1200.0),
        cumulative_realized_pnl_usd=money(10000.0),
        rolling_realized_pnl={
            "1d": money(300.0),
            "3d": money(600.0),
            "5d": money(1200.0),
            "20d": money(3000.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )
    populated_view = _make_strategist_view()
    view_deferred = StrategistView(
        positions=populated_view.positions,
        recent_thesis_resolutions=populated_view.recent_thesis_resolutions,
        portfolio_pnl=deferred_pnl,
        drawdown=populated_view.drawdown,
        sector_exposure=populated_view.sector_exposure,
        directional_exposure=populated_view.directional_exposure,
        risk_budget=populated_view.risk_budget,
        active_risk_parameters=populated_view.active_risk_parameters,
        intra_invocation_changelog=populated_view.intra_invocation_changelog,
        recent_pm_decision_log=populated_view.recent_pm_decision_log,
        abandoned_openings=populated_view.abandoned_openings,
        abandoned_actions=populated_view.abandoned_actions,
        between_invocation_closures=populated_view.between_invocation_closures,
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view_deferred),
        sector_label_display=_SECTOR_LABELS,
    )
    aggregate_section = out[out.index("=== PORTFOLIO STATE ===") :]
    # Lock the two-line layout (Portfolio P/L + Trade stats) here too.
    assert "\n  Trade stats:" in aggregate_section
    assert "win rate: not yet computed" in aggregate_section
    assert "avg win: not yet computed" in aggregate_section
    assert "avg loss: not yet computed" in aggregate_section
    assert "profit factor: not yet computed" in aggregate_section


# ---------------------------------------------------------------------------
# Between-invocation closures section (story 04c / ALP-440)
# ---------------------------------------------------------------------------


def _make_closure(
    *,
    position_id: str = "POS-CL-001",
    ticker: str = "NVDA",
    origin: str = "bracket_manager",
    exit_method: PositionExitMethod = PositionExitMethod.STOP_TRIGGERED,
    rationale: str = "price-based stop fired: NVDA $848 < $865",
) -> BetweenInvocationClosure:
    return BetweenInvocationClosure(
        position_id=position_id,
        ticker=ticker,
        instrument_type=InstrumentType.OPTIONS,
        closed_at=_TIMESTAMP,
        closing_order_id="MON.mon-001.1.0",
        exit_method=exit_method,
        origin=origin,  # type: ignore[arg-type]
        rationale=rationale,
    )


def test_between_invocation_closures_section_present() -> None:
    closure = _make_closure()
    view = _make_strategist_view(between_invocation_closures=(closure,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),
        sector_label_display=_SECTOR_LABELS,
    )
    assert "=== ACTIVITY LOG (between-invocation closures) ===" in out
    assert "NVDA" in out
    assert "STOP_TRIGGERED" in out
    assert "price-based stop fired" in out


# ---------------------------------------------------------------------------
# _render_pending_order_row — marketability-aware fill-likelihood label
# ALP-773: fix inverted distance sign for buy-limits above market
# ---------------------------------------------------------------------------


def _make_options_spec() -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=Symbol("TST"),
        strike=100.0,
        expiration=date(2026, 7, 17),
        contract_type=OptionContractType.CALL,
        contract_multiplier=100.0,
    )


def test_render_pending_order_row_buy_above_market_is_marketable() -> None:
    order = _make_pending_order(direction=OrderDirection.BUY, limit_price=105.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" in result
    assert "high fill-likelihood" in result
    assert "away from fill" not in result
    assert "low fill-likelihood" not in result


def test_render_pending_order_row_buy_below_market_is_low_fill() -> None:
    order = _make_pending_order(direction=OrderDirection.BUY, limit_price=95.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "away from fill" in result
    assert "low fill-likelihood" in result
    assert "marketable" not in result


def test_render_pending_order_row_sell_below_market_is_marketable() -> None:
    order = _make_pending_order(direction=OrderDirection.SELL, limit_price=95.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" in result
    assert "high fill-likelihood" in result
    assert "away from fill" not in result
    assert "low fill-likelihood" not in result


def test_render_pending_order_row_sell_above_market_is_low_fill() -> None:
    order = _make_pending_order(direction=OrderDirection.SELL, limit_price=105.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "away from fill" in result
    assert "low fill-likelihood" in result
    assert "marketable" not in result


def test_render_pending_order_row_buy_at_market_is_marketable() -> None:
    """Limit == current_price is the marketable boundary — must render as marketable."""
    order = _make_pending_order(direction=OrderDirection.BUY, limit_price=100.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" in result


def test_render_pending_order_row_sell_at_market_is_marketable() -> None:
    """Limit == current_price is the marketable boundary for sells too (<=)."""
    order = _make_pending_order(direction=OrderDirection.SELL, limit_price=100.0)
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" in result


def test_render_pending_order_row_dvn_preopen_snapshot() -> None:
    """DVN 2026-06-01 pre-open: limit 46.46 with current ~44.00.

    The old code rendered this as negative distance ("below underlying — won't fill").
    The fix must render it as marketable since the buy-limit is above market.
    """
    order = _make_pending_order(
        direction=OrderDirection.BUY,
        limit_price=46.46,
        order_id="ORD-DVN-1",
    )
    result = _render_pending_order_row(order, current_price=44.00)
    assert "marketable" in result
    assert "high fill-likelihood" in result
    assert "away from fill" not in result


def test_render_pending_order_row_options_does_not_claim_marketability() -> None:
    """An options limit is a premium, not comparable to the underlying spot.

    A $4.00 call premium vs a $100 underlying must NOT be labelled
    "away from fill" just because 4.00 < 100.00 — fall back to the
    direction-neutral distance instead of a false marketability claim.
    """
    order = _make_pending_order(
        direction=OrderDirection.BUY,
        limit_price=4.00,
        instrument_spec=_make_options_spec(),
    )
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" not in result
    assert "fill-likelihood" not in result
    assert "from underlying" in result


def test_render_pending_order_row_mleg_falls_back_to_distance() -> None:
    """MLEG strategy orders (direction=None) keep the direction-neutral label."""
    spec = StrategyInstrumentSpec(legs=(_make_options_spec(),))
    order = _make_pending_order(
        direction=None,
        limit_price=2.50,
        instrument_spec=spec,
        order_class=OrderClass.MLEG,
    )
    result = _render_pending_order_row(order, current_price=100.0)
    assert "marketable" not in result
    assert "fill-likelihood" not in result
    assert "from underlying" in result
