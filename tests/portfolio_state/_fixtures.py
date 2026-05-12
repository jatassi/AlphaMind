"""Fixture builders for portfolio-state end-to-end tests (story 09).

Each builder returns a 5-tuple:
    (RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime)

The tuple is ready to construct StubPortfolioStateRepository and StubCurrentPriceProvider.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.pricing import PriceQuote, PriceSource
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
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.repository import (
    CurrentInvocationMetadata,
    PortfolioPnLInputs,
    PriorInvocationContext,
    RepositoryFixture,
)

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_PHASE1_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_NOW = datetime(2025, 6, 1, 9, 30, 0, tzinfo=UTC)
_ENTRY_AT = datetime(2025, 6, 1, 7, 0, 0, tzinfo=UTC)
_INV_ID = "inv-e2e-001"

# ---------------------------------------------------------------------------
# Primitive builders (shared across fixtures)
# ---------------------------------------------------------------------------


def _make_config() -> PortfolioStateConfig:
    return PortfolioStateConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 5,
            "thesis_resolutions_lookback_trading_days": 10,
            "thesis_quality_aggregates_trailing_windows_days": (5, 20),
            "snapshot_freshness_max_phase1_to_snapshot_seconds": 300.0,
            "snapshot_freshness_max_price_age_seconds": 900.0,  # 15 min
        }
    )


def _make_cash_ledger(
    current_cash: float = 100_000.0,
    settled_cash: float | None = None,
    reserved: float = 0.0,
    margin_held: float = 0.0,
) -> CashLedger:
    settled = settled_cash if settled_cash is not None else current_cash
    return CashLedger.model_validate(
        {
            "current_cash_usd": current_cash,
            "settled_cash_usd": settled,
            "reserved_capital_usd": reserved,
            "available_buying_power_usd": current_cash,
            "margin_held_usd": margin_held,
            "unsettled_proceeds": [],
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )


def _make_drawdown_state(current_drawdown_pct: float = 0.0) -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": current_drawdown_pct,
            "equity_high_water_mark_usd": 100_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": current_drawdown_pct,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _make_pnl_inputs() -> PortfolioPnLInputs:
    return PortfolioPnLInputs.model_validate(
        {
            "daily_realized_pnl_usd": 0.0,
            "cumulative_realized_pnl_usd": 0.0,
            "rolling_realized_pnl": {"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
            "win_rate_pct": None,
            "average_win_size_usd": None,
            "average_loss_size_usd": None,
            "profit_factor": None,
        }
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption.model_validate({"entries": []})


def _make_active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet.model_validate(
        {
            "regime_label": RegimeLabel.NORMAL,
            "transition_state": RegimeTransitionState.STABLE,
            "transition_invocations_remaining": 0,
            "parameter_change_flag": False,
            "entries": [
                ActiveRiskParameterEntry.model_validate(
                    {
                        "rule_id": "max_position_size_usd",
                        "rule_label": "Max position size",
                        "value": 25_000.0,
                        "unit": "USD",
                        "regime_multiplier_applied": 1.0,
                        "base_value": 25_000.0,
                    }
                )
            ],
            "active_overlays": [],
        }
    )


def _make_thesis_quality_aggregates(now: datetime = _NOW) -> ThesisQualityAggregate:
    return ThesisQualityAggregate.model_validate(
        {
            "as_of_timestamp": now,
            "resolution_counts_by_window": [],
            "duration_stats_by_window": [],
            "invalidation_timing_stats_by_window": [],
            "signal_hit_rates": [],
            "signal_to_thesis_conversions": [],
            "conviction_calibration": [],
            "conviction_sizing_deviation_by_window": [],
            "performance_attribution": [],
            "alpha_beta_decomposition_by_window": [],
        }
    )


def _make_invocation_metadata(
    invocation_id: str = _INV_ID,
    phase1_at: datetime = _PHASE1_AT,
) -> CurrentInvocationMetadata:
    return CurrentInvocationMetadata.model_validate(
        {
            "invocation_id": invocation_id,
            "phase1_committed_at": phase1_at,
            "pipeline_invocation_started_at": None,
        }
    )


def _make_prior_context() -> PriorInvocationContext:
    return PriorInvocationContext.model_validate(
        {
            "prior_invocation_id": None,
            "prior_active_risk_parameters": None,
            "prior_phase1_committed_at": None,
        }
    )


def _make_equity_position(
    position_id: str,
    ticker: str,
    share_count: float,
    cost_per_share: float,
    direction: Direction,
    status: PositionStatus = PositionStatus.OPEN,
    entry_timestamp: datetime = _ENTRY_AT,
    bracket_id: str | None = None,
    thesis_id: str | None = None,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    equity = EquityPositionDetails(
        ticker=ticker,
        share_count=share_count,
        average_cost_basis_per_share=cost_per_share,
        borrow_rate_pct=0.5 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=1_000.0 if is_short else None,
    )
    execution_history: tuple[PositionFill, ...]
    if status == PositionStatus.OPEN:
        execution_history = (
            PositionFill(
                fill_timestamp=entry_timestamp,
                fill_price=cost_per_share,
                fill_quantity=share_count,
                slippage=0.01,
                fees=1.0,
            ),
        )
    else:
        execution_history = ()
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": status,
            "direction": direction,
            "entry_timestamp": entry_timestamp if status == PositionStatus.OPEN else None,
            "details": equity,
            "execution_history": execution_history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_bracket(bracket_id: str, position_id: str) -> BracketRecord:
    stop_leg = BracketLeg(
        leg_id=f"{bracket_id}-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=f"ord-stop-{bracket_id}",
        trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=400.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    target_leg = BracketLeg(
        leg_id=f"{bracket_id}-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=f"ord-target-{bracket_id}",
        trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=600.0, direction="GTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord.model_validate(
        {
            "bracket_id": bracket_id,
            "position_id": position_id,
            "status": BracketStatus.ACTIVE,
            "entry_order_id": f"ord-entry-{bracket_id}",
            "protective_legs": (stop_leg, target_leg),
            "modification_history": (),
            "corporate_action_cancellation_reason": None,
        }
    )


def _make_pending_equity_order(
    order_id: str,
    position_id: str,
    ticker: str,
    quantity: float,
    limit_price: float,
    submission_timestamp: datetime = _ENTRY_AT,
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=ticker)
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": f"brk-{order_id}",
            "role": OrderRole.ENTRY,
            "instrument_spec": spec,
            "direction": OrderDirection.BUY,
            "order_type": OrderType.LIMIT,
            "price_parameters": PriceParameters(limit_price=limit_price, stop_trigger_price=None),
            "quantity": quantity,
            "duration": OrderDuration.GTC,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{order_id}",
            "alpaca_order_id_chain": (f"alp-{order_id}",),
            "submission_timestamp": submission_timestamp,
            "last_update_timestamp": submission_timestamp,
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": quantity,
            "modification_count": 0,
            "originating_thesis_id": None,
            "originating_pm_command_id": None,
            "age_hours": 0.0,
        }
    )


def _make_thesis(
    thesis_id: str,
    position_id: str,
    now: datetime = _NOW,
) -> ThesisRecord:
    entry_component = ThesisComponent.model_validate(
        {
            "component_id": f"{thesis_id}-entry",
            "thesis_id": thesis_id,
            "component_type": ThesisComponentType.ENTRY_RATIONALE,
            "linked_bracket_leg_type": None,
            "instrument_reference": "NVDA",
            "narrative": "Entry rationale text.",
            "key_assumptions": (),
            "generation_timestamp": now,
            "resolution_outcome": None,
            "resolution_notes": None,
        }
    )
    target_component = ThesisComponent.model_validate(
        {
            "component_id": f"{thesis_id}-target",
            "thesis_id": thesis_id,
            "component_type": ThesisComponentType.TARGET_RATIONALE,
            "linked_bracket_leg_type": BracketLegType.TAKE_PROFIT,
            "instrument_reference": "NVDA",
            "narrative": "Target rationale text.",
            "key_assumptions": (),
            "generation_timestamp": now,
            "resolution_outcome": None,
            "resolution_notes": None,
        }
    )
    inval_component = ThesisComponent.model_validate(
        {
            "component_id": f"{thesis_id}-inval",
            "thesis_id": thesis_id,
            "component_type": ThesisComponentType.INVALIDATION_RATIONALE,
            "linked_bracket_leg_type": BracketLegType.PRICE_STOP,
            "instrument_reference": "NVDA",
            "narrative": "Invalidation rationale text.",
            "key_assumptions": (),
            "generation_timestamp": now,
            "resolution_outcome": None,
            "resolution_notes": None,
        }
    )
    return ThesisRecord.model_validate(
        {
            "thesis_id": thesis_id,
            "position_id": position_id,
            "summary": "Test thesis summary",
            "components": (entry_component, target_component, inval_component),
            "status": ThesisRecordStatus.ACTIVE,
            "generation_timestamp": now,
            "time_expectation_hours": 48.0,
            "age_hours": 0.5,
            "expected_resolution_at": now + timedelta(hours=48),
            "resolution_timestamp": None,
            "resolution_category": None,
            "resolution_pnl_usd": None,
            "entry_fill_gap_usd": None,
            "key_catalyst": "Earnings catalyst",
        }
    )


def _make_pm_decision_log_entry(
    entry_id: str,
    invocation_id: str = _INV_ID,
    position_id: str | None = None,
    timestamp: datetime = _NOW,
) -> ActivityLogEntry:
    detail = PMDecisionDetail(
        envelope_id=f"env-{entry_id}",
        source_provenance_json={},
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=(),
        verdict=PMVerdict.APPROVE,
    )
    return ActivityLogEntry.model_validate(
        {
            "entry_id": entry_id,
            "invocation_id": invocation_id,
            "timestamp": timestamp,
            "event_type": EventType.PM_DECISION,
            "event_group": EventGroup.PM_DECISION,
            "position_id": position_id,
            "order_id": None,
            "thesis_id": None,
            "source": EventSource.COMMAND_EXECUTOR,
            "detail": detail,
        }
    )


def _make_position_opened_entry(
    entry_id: str,
    invocation_id: str = _INV_ID,
    position_id: str = "POS-001",
    ticker: str = "NVDA",
    timestamp: datetime = _NOW,
) -> ActivityLogEntry:
    detail = PositionOpenedDetail(
        ticker=ticker,
        direction="LONG",
        fill_price=500.0,
        quantity=100.0,
        thesis_id=None,
        bracket_id=None,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )
    return ActivityLogEntry.model_validate(
        {
            "entry_id": entry_id,
            "invocation_id": invocation_id,
            "timestamp": timestamp,
            "event_type": EventType.POSITION_OPENED,
            "event_group": EventGroup.POSITION_LIFECYCLE,
            "position_id": position_id,
            "order_id": None,
            "thesis_id": None,
            "source": EventSource.FILL_PROCESSOR,
            "detail": detail,
        }
    )


def _make_fresh_quote(ticker: str, price: float, now: datetime = _NOW) -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price_usd=price,
        as_of_timestamp=now,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )


def _make_stale_quote(ticker: str, now: datetime = _NOW) -> PriceQuote:
    return PriceQuote(
        ticker=ticker,
        price_usd=100.0,
        as_of_timestamp=now - timedelta(hours=1),
        source=PriceSource.STALE_FALLBACK,
        is_stale=True,
    )


def _make_options_position(
    position_id: str,
    underlying_ticker: str,
    contract_type: OptionContractType,
    delta: float,
    entry_timestamp: datetime = _ENTRY_AT,
) -> PositionRecord:
    premium = 10.0
    options = OptionsPositionDetails(
        underlying_ticker=underlying_ticker,
        strike_price=500.0,
        expiration_date=date(2026, 1, 16),
        contract_type=contract_type,
        contract_count=2.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=premium,
        greeks=OptionGreeks(delta=delta, gamma=0.01, theta=-0.5, vega=0.2),
    )
    fill = PositionFill(
        fill_timestamp=entry_timestamp,
        fill_price=premium,
        fill_quantity=2.0,
        slippage=0.01,
        fees=1.0,
    )
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": entry_timestamp,
            "details": options,
            "execution_history": (fill,),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_base_fixture(
    open_positions: tuple[PositionRecord, ...] = (),
    pending_positions: tuple[PositionRecord, ...] = (),
    brackets: tuple[BracketRecord, ...] = (),
    pending_orders: tuple[OrderRecord, ...] = (),
    active_theses: tuple[ThesisRecord, ...] = (),
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]] | None = None,
    cash_ledger: CashLedger | None = None,
    invocation_metadata: CurrentInvocationMetadata | None = None,
    now: datetime = _NOW,
) -> RepositoryFixture:
    return RepositoryFixture.model_validate(
        {
            "open_positions": open_positions,
            "pending_positions": pending_positions,
            "drawdown_state": _make_drawdown_state(),
            "portfolio_pnl_inputs": _make_pnl_inputs(),
            "active_theses": active_theses,
            "recent_thesis_resolutions": (),
            "cash_ledger": cash_ledger or _make_cash_ledger(),
            "pending_orders": pending_orders,
            "risk_budget": _make_risk_budget(),
            "active_risk_parameters": _make_active_risk_parameters(),
            "intra_invocation_changelog": intra_invocation_changelog,
            "recent_pm_decision_log": recent_pm_decision_log,
            "position_modification_trail": position_modification_trail or {},
            "thesis_quality_aggregates": _make_thesis_quality_aggregates(now),
            "brackets": brackets,
            "current_invocation_metadata": invocation_metadata or _make_invocation_metadata(),
            "prior_invocation_context": _make_prior_context(),
        }
    )


def _make_sector_resolver(mapping: dict[str, str]) -> Callable[[PositionRecord], str | None]:
    def _resolve(pos: PositionRecord) -> str | None:
        details = pos.details
        if isinstance(details, EquityPositionDetails):
            return mapping.get(details.ticker)
        if isinstance(details, OptionsPositionDetails):
            return mapping.get(details.underlying_ticker)
        return None

    return _resolve


# ---------------------------------------------------------------------------
# Public fixture builders
# ---------------------------------------------------------------------------


def build_minimal_snapshot_inputs() -> tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]:
    """Empty portfolio: zero positions, $100,000 settled cash, zero drawdown."""
    fixture = _make_base_fixture(
        cash_ledger=_make_cash_ledger(current_cash=100_000.0),
    )
    quotes: dict[str, PriceQuote] = {}
    sector_resolver = _make_sector_resolver({})
    config = _make_config()
    return fixture, quotes, sector_resolver, config, _NOW


def build_multi_position_snapshot_inputs() -> tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]:
    """Three open equity positions + one pending, theses, brackets, activity log entries.

    Positions:
        NVDA long  100 shares @ $500  → cost $50,000
        AMD  short 250 shares @ $120  → cost $30,000
        JPM  long  100 shares @ $200  → cost $20,000
    Pending:
        AAPL long  50 shares  @ $180  (limit order at $180)
    Cash: $50,000 settled
    Prices:
        NVDA $510  AMD $115  JPM $195  AAPL $185
    Sector:
        NVDA/AMD → tech  JPM → financials  AAPL → tech
    """
    nvda = _make_equity_position(
        "POS-NVDA",
        "NVDA",
        100.0,
        500.0,
        Direction.LONG,
        bracket_id="BRK-NVDA",
        thesis_id="THESIS-NVDA",
    )
    amd = _make_equity_position(
        "POS-AMD",
        "AMD",
        250.0,
        120.0,
        Direction.SHORT,
        bracket_id="BRK-AMD",
        thesis_id="THESIS-AMD",
    )
    jpm = _make_equity_position(
        "POS-JPM",
        "JPM",
        100.0,
        200.0,
        Direction.LONG,
        bracket_id="BRK-JPM",
        thesis_id="THESIS-JPM",
    )
    aapl_pending = _make_equity_position(
        "POS-AAPL",
        "AAPL",
        50.0,
        180.0,
        Direction.LONG,
        status=PositionStatus.PENDING,
        bracket_id=None,
    )

    brackets = (
        _make_bracket("BRK-NVDA", "POS-NVDA"),
        _make_bracket("BRK-AMD", "POS-AMD"),
        _make_bracket("BRK-JPM", "POS-JPM"),
    )

    aapl_order = _make_pending_equity_order("ORD-AAPL", "POS-AAPL", "AAPL", 50.0, 180.0, _ENTRY_AT)

    theses = (
        _make_thesis("THESIS-NVDA", "POS-NVDA"),
        _make_thesis("THESIS-AMD", "POS-AMD"),
        _make_thesis("THESIS-JPM", "POS-JPM"),
    )

    changelog_entry = _make_pm_decision_log_entry("entry-chg-001", _INV_ID, "POS-NVDA")
    pm_log_entry = _make_pm_decision_log_entry("entry-pm-001", _INV_ID, "POS-NVDA")

    pos_opened_nvda = _make_position_opened_entry("entry-opened-NVDA", _INV_ID, "POS-NVDA", "NVDA")
    pos_opened_amd = _make_position_opened_entry("entry-opened-AMD", _INV_ID, "POS-AMD", "AMD")

    fixture = _make_base_fixture(
        open_positions=(nvda, amd, jpm),
        pending_positions=(aapl_pending,),
        brackets=brackets,
        pending_orders=(aapl_order,),
        active_theses=theses,
        intra_invocation_changelog=(changelog_entry,),
        recent_pm_decision_log=(pm_log_entry,),
        position_modification_trail={
            "POS-NVDA": (pos_opened_nvda,),
            "POS-AMD": (pos_opened_amd,),
        },
        cash_ledger=_make_cash_ledger(
            current_cash=50_000.0,
            settled_cash=50_000.0,
            reserved=5_000.0,
            margin_held=1_000.0,
        ),
    )

    quotes: dict[str, PriceQuote] = {
        "NVDA": _make_fresh_quote("NVDA", 510.0),
        "AMD": _make_fresh_quote("AMD", 115.0),
        "JPM": _make_fresh_quote("JPM", 195.0),
        "AAPL": _make_fresh_quote("AAPL", 185.0),
    }

    sector_resolver = _make_sector_resolver(
        {"NVDA": "tech", "AMD": "tech", "JPM": "financials", "AAPL": "tech"}
    )
    config = _make_config()
    return fixture, quotes, sector_resolver, config, _NOW


def build_options_position_snapshot_inputs() -> tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]:
    """One long call on NVDA (positive delta) and one long put on TSLA (negative delta).

    Tests directional-bucketing behavior from story 05c.
    """
    nvda_call = _make_options_position("OPT-NVDA-CALL", "NVDA", OptionContractType.CALL, delta=0.6)
    tsla_put = _make_options_position("OPT-TSLA-PUT", "TSLA", OptionContractType.PUT, delta=-0.4)

    fixture = _make_base_fixture(
        open_positions=(nvda_call, tsla_put),
        cash_ledger=_make_cash_ledger(100_000.0),
    )
    quotes: dict[str, PriceQuote] = {
        "NVDA": _make_fresh_quote("NVDA", 510.0),
        "TSLA": _make_fresh_quote("TSLA", 250.0),
    }
    sector_resolver = _make_sector_resolver({"NVDA": "tech", "TSLA": "tech"})
    config = _make_config()
    return fixture, quotes, sector_resolver, config, _NOW


def build_stale_pricing_snapshot_inputs() -> tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]:
    """Two open positions; all price quotes are 60 minutes old (> 15-min threshold)."""
    nvda = _make_equity_position("POS-NVDA-STALE", "NVDA", 100.0, 500.0, Direction.LONG)
    jpm = _make_equity_position("POS-JPM-STALE", "JPM", 50.0, 200.0, Direction.LONG)

    fixture = _make_base_fixture(
        open_positions=(nvda, jpm),
        cash_ledger=_make_cash_ledger(50_000.0),
    )

    # Quotes timestamped 60 minutes before NOW — older than max_price_age_seconds=900
    quotes: dict[str, PriceQuote] = {
        "NVDA": _make_stale_quote("NVDA"),
        "JPM": _make_stale_quote("JPM"),
    }
    sector_resolver = _make_sector_resolver({"NVDA": "tech", "JPM": "financials"})
    config = _make_config()
    return fixture, quotes, sector_resolver, config, _NOW


def build_unknown_ticker_snapshot_inputs() -> tuple[
    RepositoryFixture, dict[str, PriceQuote], SectorResolver, PortfolioStateConfig, datetime
]:
    """Two positions; price provider omits TICKER-X entirely (unknown ticker)."""
    known = _make_equity_position("POS-KNOWN", "NVDA", 100.0, 500.0, Direction.LONG)
    unknown = _make_equity_position("POS-UNKNOWN", "TICKER-X", 50.0, 100.0, Direction.LONG)

    fixture = _make_base_fixture(
        open_positions=(known, unknown),
        cash_ledger=_make_cash_ledger(50_000.0),
    )

    # Only NVDA provided — TICKER-X is intentionally omitted
    quotes: dict[str, PriceQuote] = {
        "NVDA": _make_fresh_quote("NVDA", 510.0),
    }
    sector_resolver = _make_sector_resolver({"NVDA": "tech"})
    config = _make_config()
    return fixture, quotes, sector_resolver, config, _NOW
