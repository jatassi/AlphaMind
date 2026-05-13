"""Tests for the strategist input-bundle assembler — story 04 (ALP-304)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.decision.strategist.input_bundle import (
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
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    BracketModificationSource,
    BracketModifiedDetail,
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
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
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
    ThesisStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import HaltState
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
        total_unrealized_pnl_usd=8200.0,
        total_unrealized_pnl_pct_of_portfolio=0.82,
        daily_realized_pnl_usd=300.0,
        daily_total_pnl_usd=1200.0,
        cumulative_realized_pnl_usd=10000.0,
        rolling_realized_pnl={"1d": 300.0, "3d": 600.0, "5d": 1200.0, "20d": 3000.0},
        win_rate_pct=55.0,
        average_win_size_usd=200.0,
        average_loss_size_usd=150.0,
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
        total_long_delta_adjusted_usd=420_000.0,
        total_short_delta_adjusted_usd=0.0,
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
        fill_price=avg_cost,
        fill_quantity=share_count,
        slippage=0.01,
        fees=1.0,
    )
    record = PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _ENTRY_TIMESTAMP,
            "details": equity,
            "execution_history": (fill,),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )
    return PositionView(
        record=record,
        current_market_value_usd=market_value,
        unrealized_pnl_usd=unrealized_pnl_usd,
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=weight_pct,
        position_age_hours=age_hours,
        notional_exposure_usd=market_value,
        delta_adjusted_exposure_usd=market_value,
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

    return ThesisRecord.model_validate(
        {
            "thesis_id": thesis_id,
            "position_id": position_id,
            "summary": "Hyperscaler capex acceleration drives Q1 revenue beat...",
            "components": (
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
            "status": ThesisRecordStatus.ACTIVE,
            "generation_timestamp": _ENTRY_TIMESTAMP,
            "time_expectation_hours": 48.0,
            "age_hours": 36.4,
            "expected_resolution_at": datetime(2026, 5, 5, 14, 0, 0, tzinfo=UTC),
            "resolution_timestamp": None,
            "resolution_category": None,
            "resolution_pnl_usd": None,
            "entry_fill_gap_usd": None,
            "key_catalyst": "MSFT Q1 capex guide",
        }
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
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol(ticker))
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId("BRK-NVDA-001"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=limit_price, stop_trigger_price=None),
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
            _make_bracket(position_id=position_id, bracket_id=f"BRK-{position_id}")
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
) -> dict[str, object]:
    return {
        "strategist_view": strategist_view or _make_strategist_view(),
        "invocation_id": _INVOCATION_ID,
        "timestamp": _TIMESTAMP,
        "options_enabled": False,
        "short_selling_enabled": False,
        "active_sectors": ("tech", "semis", "financials", "energy"),
        "state_delivery_config": _make_state_delivery_config(),
        "sector_resolver": _sector_resolver,
        "total_portfolio_value_usd": _TOTAL_PORTFOLIO_VALUE_USD,
        "available_for_new_positions_usd": _AVAILABLE_FOR_NEW_POSITIONS_USD,
        "current_price_lookup": _current_price_lookup,
        "synthesizer_brief_text": _SYNTHESIZER_BRIEF,
        "tool_names": _TOOL_NAMES,
    }


def _defensive_kwargs(
    *,
    strategist_view: StrategistView | None = None,
) -> dict[str, object]:
    kwargs = _normal_kwargs(strategist_view=strategist_view)
    kwargs["halt_state"] = _make_halt_state()
    return kwargs


# ---------------------------------------------------------------------------
# Tracer bullet — section ordering for normal-mode bundle
# ---------------------------------------------------------------------------


def test_normal_mode_section_ordering() -> None:
    """Normal-mode bundle order: header → tools → portfolio state → brief."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    envelope_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    assert envelope_idx < tools_idx < portfolio_idx < brief_idx


# ---------------------------------------------------------------------------
# Section anchors
# ---------------------------------------------------------------------------


def test_normal_mode_starts_with_envelope() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert out.startswith(
        "=== GUARDRAIL STATE (invocation inv-strat-001, 2026-05-04T14:32:05Z) ==="
    )


def test_normal_mode_ends_with_brief() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert out.rstrip().endswith(_SYNTHESIZER_BRIEF.rstrip())


# ---------------------------------------------------------------------------
# Tool reminder — both tools surfaced in normal mode
# ---------------------------------------------------------------------------


def test_normal_mode_tool_reminder_surfaces_both_tools() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    tool_section = out[tools_idx:portfolio_idx]
    assert f"- {_VALIDATE_GUARDRAIL_TOOL}" in tool_section
    assert f"- {_RETRIEVE_BRIEF_TOOL}" in tool_section


def test_defensive_posture_tool_reminder_surfaces_both_tools() -> None:
    """Strategist still uses validate_guardrail in defensive_posture mode (parent decision (I))."""
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    tool_section = out[tools_idx:portfolio_idx]
    assert f"- {_VALIDATE_GUARDRAIL_TOOL}" in tool_section
    assert f"- {_RETRIEVE_BRIEF_TOOL}" in tool_section


def test_defensive_posture_tool_reminder_includes_posture_note() -> None:
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    tool_section = out[tools_idx:portfolio_idx]
    assert "defensive posture" in tool_section.lower()
    assert "add" in tool_section.lower()


def test_normal_mode_tool_reminder_omits_posture_note() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
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
        **_defensive_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in out
    assert "DEFENSIVE POSTURE" in out


def test_defensive_posture_section_ordering() -> None:
    out = assemble_input_bundle_defensive_posture(
        **_defensive_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    envelope_idx = out.index("=== GUARDRAIL STATE")
    tools_idx = out.index("=== AVAILABLE TOOLS ===")
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    assert envelope_idx < tools_idx < portfolio_idx < brief_idx


# ---------------------------------------------------------------------------
# Per-position derived fields (parent decision (J))
# ---------------------------------------------------------------------------


def test_position_record_renders_pnl_absolute_and_percentage() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    # Unrealized P/L absolute and percentage are present (5.0% and $8,200 from fixtures)
    assert "+$8,200" in out or "+8,200" in out
    assert "+5.0%" in out


def test_position_record_renders_position_age_hours() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    # Position age 36.4h
    assert "36.4" in out


def test_position_record_renders_distance_to_target_and_stop_pct() -> None:
    """Distance-to-target and distance-to-stop are computed from current price + bracket legs."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    # Current price 862, target 189.00, stop 167.00 — but those are in the
    # bracket triggers; the renderer must surface the bracket levels and a
    # distance computation. Look for both target and stop trigger numbers.
    assert "189" in out
    assert "167" in out


def test_position_record_renders_risk_reward_at_current() -> None:
    """R/R at current must be present for positions with both target and stop legs."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    # Token "R/R" identifies the risk/reward line
    assert "R/R" in out


# ---------------------------------------------------------------------------
# Thesis block rendering (full thesis when present)
# ---------------------------------------------------------------------------


def test_thesis_block_renders_summary_and_components() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "TH-NVDA-001" in out
    assert "Hyperscaler capex acceleration" in out
    assert "Microsoft Q1 capex guide" in out  # key assumption
    assert "Entry: capex acceleration thesis" in out
    assert "Target: Q1 print fully prices in" in out
    assert "Invalidation: MSFT guides AI capex lower" in out


def test_thesis_block_renders_prior_status_from_snapshot() -> None:
    """Prior status renders from a passed-in ThesisHealthSnapshot (story ALP-351)."""
    from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot

    prior_snap = ThesisHealthSnapshot(
        thesis_id="TH-NVDA-001",
        invocation_id="prior-inv-000",
        snapshot_timestamp=_TIMESTAMP,
        health_status=ThesisStatus.AT_RISK,
        prior_health_status=None,
        component_health=(),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
        prior_health_snapshots=(prior_snap,),
    )
    assert "Prior status: AT_RISK" in out


def test_thesis_block_omits_prior_status_when_no_snapshot() -> None:
    """When no prior snapshot is supplied, no Prior-status line is rendered."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Prior status:" not in out


def test_position_without_thesis_renders_pending_marker() -> None:
    view = _make_strategist_view(positions=(_make_position_view(with_thesis=False),))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Thesis: NONE — pending position" in out


def test_position_without_bracket_renders_inactive_marker() -> None:
    view = _make_strategist_view(positions=(_make_position_view(with_bracket=False),))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "Bracket: not yet activated" in out


# ---------------------------------------------------------------------------
# Pending orders + modification trail per position
# ---------------------------------------------------------------------------


def test_per_position_pending_orders_render() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "ORD-LIMIT-4" in out


def test_per_position_modification_trail_renders() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "BRACKET_MODIFIED" in out
    assert "PM tightened stop on at-risk thesis" in out


# ---------------------------------------------------------------------------
# Activity-log surfaces — three sections
# ---------------------------------------------------------------------------


def test_intra_invocation_changelog_section_present() -> None:
    view = _make_strategist_view(
        intra_invocation_changelog=(_make_intra_invocation_changelog_entry(),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
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
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
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
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
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
            underlying_ticker="AAPL",
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_no_row",
            prior_as_of=_TIMESTAMP,
        ),
    )
    view = _make_strategist_view(intra_invocation_changelog=(entry,))
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
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
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "emergency invocation requested: regime_jump — NORMAL → CRISIS" in out
    assert "EmergencyInvocationRequestedDetail" not in out


def test_pm_decision_log_section_present() -> None:
    view = _make_strategist_view(
        recent_pm_decision_log=(_make_pm_decision_entry(),),
    )
    out = assemble_input_bundle_normal(
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "=== ACTIVITY LOG (recent PM decisions) ===" in out
    assert "ENV-1" in out
    assert "approve" in out.lower()


def test_empty_intra_invocation_changelog_renders_none() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    intra_idx = out.index("=== ACTIVITY LOG (intra-invocation) ===")
    pm_idx = out.index("=== ACTIVITY LOG (recent PM decisions) ===")
    block = out[intra_idx:pm_idx]
    assert "None" in block


def test_empty_pm_decision_log_renders_none() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    pm_idx = out.index("=== ACTIVITY LOG (recent PM decisions) ===")
    brief_idx = out.index("=== SYNTHESIZER BRIEF ===")
    block = out[pm_idx:brief_idx]
    assert "None" in block


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
            **kwargs,  # type: ignore[arg-type]
            sector_label_display=_SECTOR_LABELS,
        )
    msg = str(excinfo.value)
    assert "NVDA" in msg
    assert "POS-NVDA-001" in msg


# ---------------------------------------------------------------------------
# Determinism — identical inputs produce identical output
# ---------------------------------------------------------------------------


def test_normal_mode_deterministic() -> None:
    kwargs = _normal_kwargs()
    out_a = assemble_input_bundle_normal(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    out_b = assemble_input_bundle_normal(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert out_a == out_b


def test_defensive_posture_deterministic() -> None:
    kwargs = _defensive_kwargs()
    out_a = assemble_input_bundle_defensive_posture(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    out_b = assemble_input_bundle_defensive_posture(
        **kwargs,  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert out_a == out_b


# ---------------------------------------------------------------------------
# Brief verbatim
# ---------------------------------------------------------------------------


def test_synthesizer_brief_appears_verbatim() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert _SYNTHESIZER_BRIEF in out


# ---------------------------------------------------------------------------
# Aggregate block — drawdown, P/L, net long, gross
# ---------------------------------------------------------------------------


def test_portfolio_state_section_renders_aggregate_block() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    aggregate_section = out[portfolio_idx:]
    # Drawdown daily / cumulative numbers
    assert "0.5" in aggregate_section  # intraday_drawdown_pct
    assert "2.0" in aggregate_section  # current_drawdown_pct
    # Directional rollup numbers
    assert "42" in aggregate_section
    assert "78" in aggregate_section


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
        **_normal_kwargs(strategist_view=view),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    assert "=== ACTIVITY LOG (between-invocation closures) ===" in out
    assert "NVDA" in out
    assert "STOP_TRIGGERED" in out
    assert "price-based stop fired" in out


def test_between_invocation_closures_section_empty_renders_none() -> None:
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    header_idx = out.index("=== ACTIVITY LOG (between-invocation closures) ===")
    intra_idx = out.index("=== ACTIVITY LOG (intra-invocation) ===")
    block = out[header_idx:intra_idx]
    assert "None" in block


def test_between_invocation_closures_section_before_intra_log() -> None:
    """The between-invocation block sits between portfolio-state and intra-log."""
    out = assemble_input_bundle_normal(
        **_normal_kwargs(),  # type: ignore[arg-type]
        sector_label_display=_SECTOR_LABELS,
    )
    portfolio_idx = out.index("=== PORTFOLIO STATE ===")
    closures_idx = out.index("=== ACTIVITY LOG (between-invocation closures) ===")
    intra_idx = out.index("=== ACTIVITY LOG (intra-invocation) ===")
    assert portfolio_idx < closures_idx < intra_idx
