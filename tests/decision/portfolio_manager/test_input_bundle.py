"""Tests for the portfolio manager input-bundle assembler — story 04 (ALP-324)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import Price, money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.decision.portfolio_manager.input_bundle import (
    assemble_input_bundle_halt,
    assemble_input_bundle_normal,
)
from alphamind.decision.proposal_pre_processor.models import (
    AggregateObservations,
    AnalystSection,
    BasisSection,
    BookHealthSummary,
    ByRecommendedAction,
    ByThesisStatus,
    CombinedSetImpact,
    ConvictionDistribution,
    ConvictionHistogram,
    PerRuleEntry,
    ProposalPreProcessorBundle,
    StrategistSection,
)
from alphamind.decision.strategist.models import PortfolioLevelObservations
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import StrategistPositionView
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    BracketModificationSource,
    BracketModifiedDetail,
    DistillationConfigChange,
    DistillationConfigChangeDetail,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
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
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation.types import EscalationZones
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALIDATE_GUARDRAIL_TOOL = "mcp__alphamind_state_delivery_validation__validate_guardrail"
_RETRIEVE_BRIEF_TOOL = "mcp__alphamind_synthesizer_retrieval__retrieve_brief"
_GET_THESIS_COMPONENTS_TOOL = (
    "mcp__alphamind_portfolio_state_thesis_components__get_thesis_components"
)
_SUBMIT_ENVELOPE_TOOL = "mcp__alphamind_execution_oms_submit__submit_envelope"
_TOOL_NAMES: tuple[str, ...] = (
    _VALIDATE_GUARDRAIL_TOOL,
    _RETRIEVE_BRIEF_TOOL,
    _GET_THESIS_COMPONENTS_TOOL,
    _SUBMIT_ENVELOPE_TOOL,
)

_SYNTHESIZER_BRIEF = (
    "## Cross-domain market snapshot\n\n"
    "Tech tape mixed [SA-TECH-3]; financials grinding higher [SA-FIN-1]; "
    "energy quiet ahead of inventory print [SA-ENERGY-2]."
)

_INVOCATION_ID = "inv-pm-001"
_TIMESTAMP = datetime(2026, 5, 4, 14, 32, 5, tzinfo=UTC)
_ENTRY_TIMESTAMP = datetime(2026, 5, 3, 14, 0, 0, tzinfo=UTC)

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
        dependency_risk_flag_min_position_count=4,
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
        _make_budget_entry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            current_value=0.5,
            limit_value=2.5,
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
    returns ``None``); the renderers must show *strategy_type_label* instead.
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
                    text="Microsoft Q1 capex guide >= $24B",
                    outcome=None,
                ),
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
                "Invalidation: MSFT guides AI capex lower",
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
            underlying_ticker=Symbol("NVDA"), threshold_usd=880.00, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    stop_leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=760.00, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(target_leg, stop_leg),
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
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol(ticker))
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId("BRK-NVDA-001"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=OrderDirection.BUY_TO_OPEN,
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
    )


def _make_modification_trail_entry(
    *,
    entry_id: str = "ALE-MOD-1",
    position_id: str = "POS-NVDA-001",
    field_changed: str = "stop_price",
    old_value: str = "740.00",
    new_value: str = "760.00",
    rationale: str = "PM tightened stop on at-risk thesis",
) -> ActivityLogEntry:
    detail = BracketModifiedDetail(
        source=BracketModificationSource.PM,
        field_changed=field_changed,
        old_value=old_value,
        new_value=new_value,
        rationale=rationale,
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
        old_value="860.00",
        new_value="880.00",
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


def _make_thesis_quality_aggregates() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
        as_of_timestamp=_TIMESTAMP,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
        signal_hit_rates=(),
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


def _make_pm_view(
    *,
    positions: tuple[StrategistPositionView, ...] | None = None,
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]] | None = None,
) -> PortfolioManagerView:
    if positions is None:
        positions = (_make_position_view(),)
    return PortfolioManagerView(
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
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        position_modification_trail=position_modification_trail or {},
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


def _make_halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )


def _make_cross_constraint_impact() -> CrossConstraintImpact:
    return CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector concentration",
                current=18.5,
                projected_after=21.5,
                limit=25.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=3.5,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=money(200_000.0),
        available_capital_after_usd=money(180_000.0),
    )


def _make_pre_processor_bundle() -> ProposalPreProcessorBundle:
    basis = BasisSection(
        analyst_proposal_ids=("REC-1",),
        strategist_action_ids=("SA-1",),
        strategist_holds_excluded_count=2,
        snapshot_timestamp=_TIMESTAMP,
    )
    per_rule = PerRuleEntry(
        rule="sector_concentration_tech",
        status="PASS",
        current=18.5,
        limit=25.0,
        projected_after=21.5,
        headroom_remaining=3.5,
        unit="% of portfolio",
    )
    combined = CombinedSetImpact(basis=basis, per_rule=(per_rule,), breaches=())
    aggregate = AggregateObservations(
        combined_set_impact=combined,
        conviction_distribution=ConvictionDistribution(
            by_level=ConvictionHistogram.model_validate({"1": 0, "2": 1, "3": 3, "4": 1, "5": 0}),
            total=5,
        ),
        book_health_summary=BookHealthSummary(
            by_thesis_status=ByThesisStatus.model_validate(
                {
                    "on-track": 7,
                    "partially-realized": 0,
                    "at-risk": 0,
                    "stale": 0,
                    "invalidated": 0,
                }
            ),
            by_recommended_action=ByRecommendedAction.model_validate(
                {"hold": 7, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
            ),
            remedy_flagged_count=0,
            total=7,
        ),
    )
    portfolio_obs = PortfolioLevelObservations(
        aggregate_thesis_health="ok",
        sector_balance_shifts="balanced",
        thesis_dependency_warnings="none",
        capital_allocation_observations="within limits",
    )
    return ProposalPreProcessorBundle(
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        aggregate_observations=aggregate,
        strategist_section=StrategistSection(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=portfolio_obs,
        ),
        analyst_section=AnalystSection(mode="normal", recommendations=(), watchlist=None),
    )


def _current_price_lookup(ticker: str) -> Price:
    return price({"NVDA": "820.0", "AAPL": "175.0", "AMD": "145.0"}[ticker])


def _normal_kwargs(
    *,
    pm_view: PortfolioManagerView | None = None,
) -> dict[str, Any]:
    return {
        "pm_view": pm_view or _make_pm_view(),
        "pre_processor_bundle": _make_pre_processor_bundle(),
        "synthesizer_brief_text": _SYNTHESIZER_BRIEF,
        "invocation_id": _INVOCATION_ID,
        "timestamp": _TIMESTAMP,
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis", "financials", "energy"),
        "state_delivery_config": _make_state_delivery_config(),
        "sector_resolver": _sector_resolver,
        "total_portfolio_value_usd": money(_TOTAL_PORTFOLIO_VALUE_USD),
        "available_for_new_positions_usd": money(_AVAILABLE_FOR_NEW_POSITIONS_USD),
        "cross_constraint_impact": _make_cross_constraint_impact(),
        "tool_names": _TOOL_NAMES,
        "position_zones": _DEFAULT_POSITION_ZONES,
        "sector_label_display": _SECTOR_LABELS,
    }


def _halt_kwargs(
    *,
    pm_view: PortfolioManagerView | None = None,
) -> dict[str, Any]:
    kwargs = _normal_kwargs(pm_view=pm_view)
    kwargs["halt_state"] = _make_halt_state()
    kwargs["pending_orders"] = ()
    kwargs["current_price_lookup"] = _current_price_lookup
    return kwargs


# ---------------------------------------------------------------------------
# Tracer bullet — section ordering for normal mode
# ---------------------------------------------------------------------------


def test_normal_bundle_contains_all_seven_section_markers_in_order() -> None:
    """Normal-mode bundle order: header → tools → pre-processor bundle → brief → portfolio
    state → intra-invocation log → recent PM decision log."""
    out = assemble_input_bundle_normal(**_normal_kwargs())
    guardrail_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    pre_proc_idx = out.index("=== PROPOSAL PRE-PROCESSOR BUNDLE ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    intra_idx = out.index("=== ACTIVITY LOG (intra-invocation) ===")
    pm_log_idx = out.index("=== ACTIVITY LOG (recent PM decisions) ===")
    assert (
        guardrail_idx
        < tools_idx
        < pre_proc_idx
        < brief_idx
        < portfolio_idx
        < intra_idx
        < pm_log_idx
    )


# ---------------------------------------------------------------------------
# Halt mode — banner from render_pm_header_halt_mode
# ---------------------------------------------------------------------------


def test_halt_bundle_contains_halt_banner() -> None:
    out = assemble_input_bundle_halt(**_halt_kwargs())
    assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in out


def test_synthesizer_brief_verbatim() -> None:
    out = assemble_input_bundle_normal(**_normal_kwargs())
    assert _SYNTHESIZER_BRIEF in out


def test_position_record_renders_pnl_and_distances() -> None:
    """Per-position rendering surfaces P/L (absolute + %), distances, age, thesis summary."""
    out = assemble_input_bundle_normal(**_normal_kwargs())
    # P/L absolute and percentage from fixture: $8,200 +5.0%
    assert "+$8,200" in out
    assert "+5.0%" in out
    # Position age 36.4h
    assert "36.4" in out
    # Thesis summary verbatim
    assert "Hyperscaler capex acceleration drives Q1 revenue beat" in out
    # Bracket leg conditions surface
    assert "880" in out
    assert "760" in out


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
    view = _make_pm_view(positions=(strategy_view,))
    out = assemble_input_bundle_normal(**{**_normal_kwargs(), "pm_view": view})
    # Underlying line carries the strategy-type label, not a direction word.
    assert "direction: bull_call_spread" in out
    assert "instrument: strategy" in out
    assert "direction: long" not in out
    assert "direction: short" not in out


def test_activity_log_blocks_match_view_entries() -> None:
    intra = (_make_intra_invocation_changelog_entry(),)
    pm_log = (_make_pm_decision_entry(),)
    view = _make_pm_view(intra_invocation_changelog=intra, recent_pm_decision_log=pm_log)
    out = assemble_input_bundle_normal(
        **{**_normal_kwargs(), "pm_view": view},
    )
    assert "PM raised target on momentum" in out
    assert "ENV-1" in out
    assert "approve" in out.lower()


def test_reconciliation_alert_entry_surfaces_sources_and_delta() -> None:
    """``RECONCILIATION_ALERT`` entries render structured detail, not the class name."""
    entry = ActivityLogEntry(
        entry_id="ALE-REC-1",
        invocation_id=_INVOCATION_ID,
        timestamp=_ENTRY_TIMESTAMP,
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
    view = _make_pm_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(**{**_normal_kwargs(), "pm_view": view})
    assert "ReconciliationAlertDetail" not in out
    assert "position share_count mismatch" in out
    assert "local=100.0 vs alpaca=99.5" in out
    assert "MSFT: local share_count=100.0 vs Alpaca qty=99.5" in out
    assert "position=debug-pos-07" in out


def test_distillation_config_change_entry_surfaces_key_old_new() -> None:
    """``DISTILLATION_CONFIG_CHANGE`` entries render the key_path and old/new values."""
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
        timestamp=_ENTRY_TIMESTAMP,
        event_type=EventType.DISTILLATION_CONFIG_CHANGE,
        event_group=EventGroup.CONFIGURATION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.CONFIG_RELOAD,
        detail=detail,
    )
    view = _make_pm_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(**{**_normal_kwargs(), "pm_view": view})
    assert "DistillationConfigChangeDetail" not in out
    assert "anomaly_detection.volume_anomaly_sigma: 2.0 → 2.5" in out


def test_position_modification_trail_inline() -> None:
    """A position with two modification-trail entries renders both inline."""
    pid = "POS-NVDA-001"
    trail_entries = (
        _make_modification_trail_entry(
            entry_id="ALE-MOD-1",
            position_id=pid,
            field_changed="stop_price",
            old_value="740.00",
            new_value="760.00",
            rationale="first tighten",
        ),
        _make_modification_trail_entry(
            entry_id="ALE-MOD-2",
            position_id=pid,
            field_changed="target_price",
            old_value="860.00",
            new_value="880.00",
            rationale="second raise",
        ),
    )
    view = _make_pm_view(
        positions=(_make_position_view(with_modification_trail=False),),
        position_modification_trail={pid: trail_entries},
    )
    out = assemble_input_bundle_normal(
        **{**_normal_kwargs(), "pm_view": view},
    )
    assert "first tighten" in out
    assert "second raise" in out


def test_pure_no_io_deterministic() -> None:
    """Identical inputs produce byte-equal output."""
    kwargs = _normal_kwargs()
    out_a = assemble_input_bundle_normal(**kwargs)
    out_b = assemble_input_bundle_normal(**kwargs)
    assert out_a == out_b


def test_halt_pure_no_io_deterministic() -> None:
    kwargs = _halt_kwargs()
    out_a = assemble_input_bundle_halt(**kwargs)
    out_b = assemble_input_bundle_halt(**kwargs)
    assert out_a == out_b


def test_recent_thesis_resolutions_and_abandoned_blocks_present() -> None:
    out = assemble_input_bundle_normal(**_normal_kwargs())
    assert "=== RECENT THESIS RESOLUTIONS ===" in out
    assert "=== ABANDONED OPENINGS / ACTIONS ===" in out
    assert "=== THESIS QUALITY AGGREGATE ===" in out


def test_pre_processor_bundle_serialized_byte_for_byte() -> None:
    """The rendered pre-processor JSON, when re-parsed, equals the input bundle."""
    bundle_in = _make_pre_processor_bundle()
    out = assemble_input_bundle_normal(
        **{**_normal_kwargs(), "pre_processor_bundle": bundle_in},
    )
    pre_idx = out.index("=== PROPOSAL PRE-PROCESSOR BUNDLE ===")
    end_idx = out.index("=== END PROPOSAL PRE-PROCESSOR BUNDLE ===")
    json_block = out[pre_idx:end_idx].split("\n", 1)[1].rstrip()
    bundle_out = ProposalPreProcessorBundle.model_validate_json(json_block)
    assert bundle_out == bundle_in


def test_halt_bundle_contains_all_seven_section_markers_in_order() -> None:
    out = assemble_input_bundle_halt(**_halt_kwargs())
    guardrail_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    pre_proc_idx = out.index("=== PROPOSAL PRE-PROCESSOR BUNDLE ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF PREVIEW (full brief via retrieve_brief) ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    intra_idx = out.index("=== ACTIVITY LOG (intra-invocation) ===")
    pm_log_idx = out.index("=== ACTIVITY LOG (recent PM decisions) ===")
    assert (
        guardrail_idx
        < tools_idx
        < pre_proc_idx
        < brief_idx
        < portfolio_idx
        < intra_idx
        < pm_log_idx
    )
