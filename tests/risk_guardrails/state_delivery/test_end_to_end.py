"""End-to-end verification of the state-delivery rendering layer (story 08).

Exercises every state-delivery surface against a single hand-built master
fixture and confirms the renderers, halt-mode wrappers, emergency wrapper, and
guardrail validation tool compose into a coherent, design-conformant header
set.

The committed ``.txt`` fixture files under ``fixtures/`` anchor the format
spec; the tests confirm renderer output matches those files line-for-line.
"""

from __future__ import annotations

import dataclasses
import pathlib
from datetime import UTC, date, datetime
from itertools import pairwise
from types import MappingProxyType

import pytest

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    AnalystAvailableCapital,
    AnalystHeldPosition,
    AnalystView,
)
from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    StrategistView,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import (
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.snapshot import DirectionalExposure, PortfolioPnL
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    EmergencyContext,
    EmergencyTrigger,
    HaltState,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    IvQuote,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import (
    CorrelationState,
    CrossConstraintImpact,
    CrossConstraintImpactPerRule,
    DependencyRiskFlag,
    ProjectedDelta,
    ValidationAction,
    ValidationInstrument,
    ValidationRequest,
    ValidationResult,
    ValidationSize,
    ValidationToolState,
    prepend_emergency_block,
    render_analyst_header,
    render_analyst_header_halt_mode,
    render_emergency_block,
    render_pm_header,
    render_pm_header_halt_mode,
    render_strategist_header,
    render_strategist_header_halt_mode,
    validate_guardrail,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

_INVOCATION_ID = "inv-e2e-001"
_TIMESTAMP = datetime(2026, 4, 28, 14, 32, 5, tzinfo=UTC)
_ENTRY_TIMESTAMP = datetime(2026, 4, 27, 14, 0, 0, tzinfo=UTC)
_ABANDONED_TIMESTAMP = datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC)

_TOTAL_PORTFOLIO_VALUE_USD = 50_000.0
_AVAILABLE_FOR_NEW_POSITIONS_USD = 5_000.0
_PER_POSITION_MAX_PCT = 5.0

_FULL_SECTOR_LABELS = {
    "tech": "Tech",
    "semis": "Semis",
    "financials": "Financials",
    "energy": "Energy",
}

# ---------------------------------------------------------------------------
# Helper: line-by-line comparison
# ---------------------------------------------------------------------------


_DEFAULT_POSITION_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def _assert_lines_equal(actual: str, expected: str) -> None:
    """Compare two strings line-by-line and produce a diff-friendly failure."""
    assert actual.splitlines() == expected.splitlines()


def _read_fixture(name: str) -> str:
    """Read a fixture file from ``tests/risk_guardrails/state_delivery/fixtures/``."""
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Builders for typed records
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
    """18-entry active parameter set, regime NORMAL, transition STABLE."""
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
        _make_param_entry(
            rule_id="position_max_loss_equity_pct",
            rule_label="Equity max loss",
            value=30.0,
        ),
        _make_param_entry(
            rule_id="position_max_loss_options_pct",
            rule_label="Options max loss",
            value=80.0,
        ),
        _make_param_entry(
            rule_id="net_long_pct",
            rule_label="Net long exposure",
            value=60.0,
        ),
        _make_param_entry(
            rule_id="net_short_pct",
            rule_label="Net short exposure",
            value=30.0,
        ),
        _make_param_entry(
            rule_id="gross_exposure_pct",
            rule_label="Gross exposure",
            value=120.0,
        ),
        _make_param_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            value=25.0,
        ),
        _make_param_entry(
            rule_id="sector_concentration_semis",
            rule_label="Semis sector concentration",
            value=25.0,
        ),
        _make_param_entry(
            rule_id="sector_concentration_financials",
            rule_label="Financials sector concentration",
            value=25.0,
        ),
        _make_param_entry(
            rule_id="sector_concentration_energy",
            rule_label="Energy sector concentration",
            value=25.0,
        ),
        _make_param_entry(
            rule_id="options_delta_pct",
            rule_label="Options delta exposure",
            value=40.0,
        ),
        _make_param_entry(
            rule_id="portfolio_theta_pct_per_day",
            rule_label="Portfolio theta",
            value=0.18,
            unit="% of portfolio per day",
        ),
        _make_param_entry(
            rule_id="portfolio_vega_pct_per_iv_point",
            rule_label="Portfolio vega",
            value=1.0,
            unit="% of portfolio per vol point",
        ),
        _make_param_entry(
            rule_id="total_short_pct",
            rule_label="Total short exposure",
            value=30.0,
        ),
        _make_param_entry(
            rule_id="single_short_max_pct",
            rule_label="Single short max",
            value=5.0,
        ),
        _make_param_entry(
            rule_id="min_cash_reserve_pct",
            rule_label="Min cash reserve",
            value=10.0,
        ),
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=entries,
        active_overlays=(),
    )


def _make_full_risk_budget() -> RiskBudgetConsumption:
    """18-entry risk budget; sector_concentration_tech WARNING, gross CRITICAL."""
    entries = (
        _make_budget_entry(
            rule_id="sector_concentration_tech",
            rule_label="Tech sector concentration",
            current_value=18.5,
            limit_value=25.0,
            zone=RiskZone.WARNING,
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
            rule_id="net_short_pct",
            rule_label="Net short exposure",
            current_value=10.0,
            limit_value=30.0,
        ),
        _make_budget_entry(
            rule_id="gross_exposure_pct",
            rule_label="Gross exposure",
            current_value=78.0,
            limit_value=120.0,
        ),
        _make_budget_entry(
            rule_id="options_delta_pct",
            rule_label="Options delta exposure",
            current_value=22.0,
            limit_value=40.0,
        ),
        _make_budget_entry(
            rule_id="portfolio_theta_pct_per_day",
            rule_label="Portfolio theta",
            current_value=0.10,
            limit_value=0.18,
            unit="% of portfolio per day",
        ),
        _make_budget_entry(
            rule_id="portfolio_vega_pct_per_iv_point",
            rule_label="Portfolio vega",
            current_value=0.6,
            limit_value=1.0,
            unit="% of portfolio per vol point",
        ),
        _make_budget_entry(
            rule_id="total_short_pct",
            rule_label="Total short exposure",
            current_value=8.0,
            limit_value=30.0,
        ),
        _make_budget_entry(
            rule_id="single_short_max_pct",
            rule_label="Single short max",
            current_value=2.0,
            limit_value=5.0,
        ),
        _make_budget_entry(
            rule_id="daily_drawdown_pct",
            rule_label="Daily drawdown",
            current_value=0.5,
            limit_value=2.5,
        ),
        _make_budget_entry(
            rule_id="cumulative_drawdown_pct",
            rule_label="Cumulative drawdown",
            current_value=2.0,
            limit_value=10.0,
        ),
        _make_budget_entry(
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.0,
            limit_value=5.0,
        ),
        _make_budget_entry(
            rule_id="position_max_loss_equity_pct",
            rule_label="Equity max loss",
            current_value=15.0,
            limit_value=30.0,
        ),
        _make_budget_entry(
            rule_id="position_max_loss_options_pct",
            rule_label="Options max loss",
            current_value=20.0,
            limit_value=80.0,
        ),
        _make_budget_entry(
            rule_id="min_cash_reserve_pct",
            rule_label="Min cash reserve",
            current_value=10.0,
            limit_value=10.0,
        ),
    )
    return RiskBudgetConsumption(entries=entries)


def _make_drawdown() -> DrawdownState:
    """Normal drawdown — daily NORMAL, cumulative NORMAL, no tier."""
    return DrawdownState(
        current_drawdown_pct=2.0,
        equity_high_water_mark_usd=51_000.0,
        drawdown_duration_hours=2.0,
        lifetime_max_drawdown_pct=10.0,
        intraday_drawdown_pct=0.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_drawdown_with_tier(tier: DrawdownTier) -> DrawdownState:
    """Drawdown with a cumulative tier set — for parity tests across renderers."""
    return DrawdownState(
        current_drawdown_pct=8.5,
        equity_high_water_mark_usd=51_000.0,
        drawdown_duration_hours=2.0,
        lifetime_max_drawdown_pct=10.0,
        intraday_drawdown_pct=0.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.WARNING,
        cumulative_tier=tier,
        drawdown_by_source_pct={},
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=signed_money(-200.0),
        total_unrealized_pnl_pct_of_portfolio=-0.4,
        daily_realized_pnl_usd=signed_money(-50.0),
        daily_total_pnl_usd=signed_money(-250.0),
        cumulative_realized_pnl_usd=money(1_000.0),
        rolling_realized_pnl={
            "1d": signed_money(-50.0),
            "3d": money(100.0),
            "5d": money(200.0),
            "20d": money(500.0),
        },
        win_rate_pct=55.0,
        average_win_size_usd=money(200.0),
        average_loss_size_usd=money(150.0),
        profit_factor=1.4,
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=money(21_000.0),
        total_short_delta_adjusted_usd=money(5_000.0),
        net_directional_pct_of_portfolio=32.0,
        gross_pct_of_portfolio=78.0,
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


# ---------------------------------------------------------------------------
# Position fixtures: 12 positions across 4 sectors + 1 unclassified.
# Tuple shape per row: position_id, ticker, direction, sector, weight_pct,
# pnl_pct, instrument. The "MISC" sector key routes into the unclassified
# group at render time.
# ---------------------------------------------------------------------------

_POSITION_SPEC: tuple[
    tuple[str, str, Direction, str, float, float, InstrumentType],
    ...,
] = (
    # Tech: 3 positions including the WARNING-zone position at 4.0% (0.80 of 5.0 max).
    ("POS-NVDA-001", "NVDA", Direction.LONG, "tech", 4.0, -2.0, InstrumentType.EQUITY),
    ("POS-AAPL-002", "AAPL", Direction.LONG, "tech", 3.5, 4.0, InstrumentType.EQUITY),
    ("POS-MSFT-003", "MSFT", Direction.LONG, "tech", 2.0, 1.5, InstrumentType.OPTIONS),
    # Semis: 3 positions
    ("POS-AMD-004", "AMD", Direction.LONG, "semis", 3.0, 5.0, InstrumentType.EQUITY),
    ("POS-MU-005", "MU", Direction.SHORT, "semis", 1.5, -1.0, InstrumentType.EQUITY),
    ("POS-AVGO-006", "AVGO", Direction.LONG, "semis", 2.5, 0.5, InstrumentType.OPTIONS),
    # Financials: 3 positions
    ("POS-JPM-007", "JPM", Direction.LONG, "financials", 3.8, -1.0, InstrumentType.EQUITY),
    ("POS-GS-008", "GS", Direction.LONG, "financials", 2.2, 2.5, InstrumentType.EQUITY),
    ("POS-BAC-009", "BAC", Direction.SHORT, "financials", 1.5, 1.0, InstrumentType.EQUITY),
    # Energy: 2 positions
    ("POS-XOM-010", "XOM", Direction.LONG, "energy", 2.5, 8.0, InstrumentType.EQUITY),
    ("POS-CVX-011", "CVX", Direction.LONG, "energy", 2.0, -3.0, InstrumentType.EQUITY),
    # Unclassified: 1 position
    ("POS-MISC-012", "MISC", Direction.LONG, "MISC", 0.5, 0.0, InstrumentType.EQUITY),
)


def _build_equity_position(
    *,
    position_id: str,
    ticker: str,
    direction: Direction,
    weight_pct: float,
    unrealized_pnl_pct: float,
) -> PositionView:
    is_short = direction == Direction.SHORT
    market_value = weight_pct * _TOTAL_PORTFOLIO_VALUE_USD / 100.0
    if is_short:
        market_value = -market_value
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=0.5 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=200.0 if is_short else None,
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_TIMESTAMP,
        fill_price=price(100.0),
        fill_quantity=10.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
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
        unrealized_pnl_usd=signed_money(unrealized_pnl_pct * 10.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(abs(market_value)),
        delta_adjusted_exposure_usd=signed_money(market_value),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _build_option_position(
    *,
    position_id: str,
    ticker: str,
    weight_pct: float,
    unrealized_pnl_pct: float,
) -> PositionView:
    market_value = weight_pct * _TOTAL_PORTFOLIO_VALUE_USD / 100.0
    options = OptionsPositionDetails(
        underlying_ticker=Symbol(ticker),
        strike_price=100.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=10.0,
        greeks=OptionGreeks(delta=0.5, gamma=0.05, theta=-0.10, vega=0.20),
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_TIMESTAMP,
        fill_price=price(10.0),
        fill_quantity=2.0,
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
        details=options,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(market_value),
        unrealized_pnl_usd=signed_money(unrealized_pnl_pct * 2.0),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=weight_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(market_value),
        delta_adjusted_exposure_usd=signed_money(market_value * 0.5),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _build_all_positions() -> tuple[PositionView, ...]:
    records: list[PositionView] = []
    for position_id, ticker, direction, _sector, weight_pct, pnl_pct, instrument in _POSITION_SPEC:
        if instrument == InstrumentType.EQUITY:
            records.append(
                _build_equity_position(
                    position_id=position_id,
                    ticker=ticker,
                    direction=direction,
                    weight_pct=weight_pct,
                    unrealized_pnl_pct=pnl_pct,
                )
            )
        else:
            records.append(
                _build_option_position(
                    position_id=position_id,
                    ticker=ticker,
                    weight_pct=weight_pct,
                    unrealized_pnl_pct=pnl_pct,
                )
            )
    return tuple(records)


def _build_sector_resolver() -> SectorResolver:
    """Sector resolver matching the master fixture's position spec.

    Returns the sector for known position IDs; ``None`` for the unclassified
    ``POS-MISC-012`` entry — exercising the unclassified group code path in
    the strategist's sector-breakdown block.
    """
    sector_by_position_id = {
        position_id: sector
        for position_id, _ticker, _direction, sector, _w, _p, _i in _POSITION_SPEC
        if sector != "MISC"
    }

    def _resolver(position: PositionRecord) -> str | None:
        return sector_by_position_id.get(position.position_id)

    return _resolver


def _wrap_position(view: PositionView) -> StrategistPositionView:
    return StrategistPositionView(
        position=view,
        thesis=None,
        bracket=None,
        pending_orders=(),
        modification_trail=(),
    )


# ---------------------------------------------------------------------------
# Held positions / abandoned blocks for analyst, strategist, PM
# ---------------------------------------------------------------------------


def _build_analyst_held_positions() -> tuple[AnalystHeldPosition, ...]:
    return tuple(
        AnalystHeldPosition(
            position_id=position_id,
            ticker=ticker,
            direction=direction,
            sector=sector if sector != "MISC" else "UNCLASSIFIED",
            size_pct=weight_pct,
            instrument_type=instrument,
            strategy_type_label=None,
        )
        for position_id, ticker, direction, sector, weight_pct, _p, instrument in _POSITION_SPEC
    )


def _build_abandoned_opening() -> AnalystAbandonedOpening:
    return AnalystAbandonedOpening(
        envelope_id="ENV-REC-1",
        direction=Direction.LONG,
        ticker=Symbol("GOOG"),
        instrument_type=InstrumentType.EQUITY,
        size_pct=3.0,
        abandoned_at=_ABANDONED_TIMESTAMP,
        failure_reason="broker rejected: stale price",
    )


def _build_abandoned_actions() -> tuple[StrategistAbandonedAction, ...]:
    return (
        StrategistAbandonedAction(
            envelope_id="ENV-SA-1",
            command_type="ADD",
            position_id=PositionId("POS-NVDA-001"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 35, 0, tzinfo=UTC),
            failure_reason="insufficient buying power",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-2",
            command_type="ADJUST",
            position_id=PositionId("POS-AAPL-002"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 36, 0, tzinfo=UTC),
            failure_reason="market closed",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-3",
            command_type="CLOSE",
            position_id=PositionId("POS-AMD-004"),
            order_id=None,
            abandoned_at=datetime(2026, 4, 28, 13, 37, 0, tzinfo=UTC),
            failure_reason="route timeout",
        ),
        StrategistAbandonedAction(
            envelope_id="ENV-SA-ORD-4",
            command_type="CANCEL",
            position_id=None,
            order_id=OrderId("ORD-9001"),
            abandoned_at=datetime(2026, 4, 28, 13, 38, 0, tzinfo=UTC),
            failure_reason="order already filled",
        ),
    )


def _build_engine_action_entry() -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id="ALE-ENGINE-1",
        invocation_id=_INVOCATION_ID,
        timestamp=datetime(2026, 4, 28, 14, 0, 0, tzinfo=UTC),
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=PositionId("POS-LEGACY-001"),
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            exit_price=money("120.0"),
            realized_pnl_usd=signed_money("-310.0"),
            thesis_resolution_category="position_level_max_loss",
        ),
    )


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------


def _build_analyst_view() -> AnalystView:
    return AnalystView(
        held_positions=_build_analyst_held_positions(),
        active_thesis_summaries=(),
        available_capital=AnalystAvailableCapital(
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            available_for_new_positions_pct=10.0,
            per_position_max_size_usd=_PER_POSITION_MAX_PCT * _TOTAL_PORTFOLIO_VALUE_USD / 100.0,
            per_position_max_size_pct=_PER_POSITION_MAX_PCT,
        ),
        pending_orders=(),
        abandoned_openings=(_build_abandoned_opening(),),
    )


def _build_strategist_view() -> StrategistView:
    positions = tuple(_wrap_position(p) for p in _build_all_positions())
    return StrategistView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        risk_budget=_make_full_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(_build_abandoned_opening(),),
        abandoned_actions=_build_abandoned_actions(),
    )


def _build_pm_view() -> PortfolioManagerView:
    positions = tuple(_wrap_position(p) for p in _build_all_positions())
    return PortfolioManagerView(
        positions=positions,
        recent_thesis_resolutions=(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        risk_budget=_make_full_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=(_build_engine_action_entry(),),
        recent_pm_decision_log=(),
        abandoned_openings=(_build_abandoned_opening(),),
        abandoned_actions=_build_abandoned_actions(),
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        position_modification_trail={},
    )


# ---------------------------------------------------------------------------
# Typed inputs the audience renderers need beyond the snapshot
# ---------------------------------------------------------------------------


def _build_cross_constraint_impact() -> CrossConstraintImpact:
    return CrossConstraintImpact(
        per_rule=(
            CrossConstraintImpactPerRule(
                rule_id="net_long_pct",
                rule_label="Net long",
                current=42.0,
                projected_after=45.0,
                limit=60.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=15.0,
            ),
            CrossConstraintImpactPerRule(
                rule_id="gross_exposure_pct",
                rule_label="Gross",
                current=78.0,
                projected_after=81.0,
                limit=120.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=39.0,
            ),
            CrossConstraintImpactPerRule(
                rule_id="sector_concentration_tech",
                rule_label="Tech sector",
                current=18.5,
                projected_after=20.5,
                limit=25.0,
                unit="% of portfolio",
                status="PASS",
                headroom_remaining=4.5,
            ),
        ),
        flagged_rule_ids=(),
        available_capital_before_usd=5_000.0,
        available_capital_after_usd=3_500.0,
    )


def _build_correlation_state() -> CorrelationState:
    return CorrelationState(
        weighted_avg_correlation=0.45,
        correlation_limit=0.60,
        zone=RiskZone.NORMAL,
        highest_pairwise_position_a="POS-NVDA-001",
        highest_pairwise_position_b="POS-AAPL-002",
        highest_pairwise_value=0.78,
    )


def _build_dependency_risk_flag() -> DependencyRiskFlag:
    return DependencyRiskFlag(
        max_catalyst_failure_exposure_pct=12.0,
        catalyst_failure_limit_pct=20.0,
        zone=RiskZone.NORMAL,
        effective_independent_thesis_count=8,
        worst_shared_catalyst_label="Q2 earnings",
        worst_shared_catalyst_position_ids=("POS-NVDA-001", "POS-AAPL-002"),
    )


# ---------------------------------------------------------------------------
# Renderer call wrappers (uniform invocation across tests)
# ---------------------------------------------------------------------------


def _render_normal_analyst_header() -> str:
    return render_analyst_header(
        analyst_view=_build_analyst_view(),
        risk_budget=_make_full_risk_budget(),
        active_risk_parameters=_make_active_risk_parameters(),
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
    )


def _render_normal_strategist_header(
    *,
    drawdown: DrawdownState | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str:
    view = _build_strategist_view()
    if drawdown is not None:
        view = dataclasses.replace(view, drawdown=drawdown)
    return render_strategist_header(
        strategist_view=view,
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
        sector_resolver=_build_sector_resolver(),
        total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
        available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
        regime_transition_breaches=regime_transition_breaches,
        position_zones=_DEFAULT_POSITION_ZONES,
    )


def _render_normal_pm_header(
    *,
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    drawdown: DrawdownState | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
) -> str:
    view = _build_pm_view()
    if drawdown is not None:
        view = dataclasses.replace(view, drawdown=drawdown)
    return render_pm_header(
        pm_view=view,
        invocation_id=_INVOCATION_ID,
        timestamp=_TIMESTAMP,
        options_enabled=True,
        short_selling_enabled=True,
        active_sectors=("tech", "semis", "financials", "energy"),
        config=_make_state_delivery_config(),
        sector_label_display=_FULL_SECTOR_LABELS,
        sector_resolver=_build_sector_resolver(),
        total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
        available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
        cross_constraint_impact=_build_cross_constraint_impact(),
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
        regime_transition_breaches=regime_transition_breaches,
        position_zones=_DEFAULT_POSITION_ZONES,
    )


# ---------------------------------------------------------------------------
# Halt-mode helpers
# ---------------------------------------------------------------------------


def _make_halt_state() -> HaltState:
    return HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=2.6,
        daily_drawdown_limit_pct=2.5,
    )


def _build_pending_order() -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol("NVDA"))
    return OrderRecord(
        order_id=OrderId("ORD-PENDING-1"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-PENDING-1"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        price_parameters=PriceParameters(limit_price=510.0, stop_trigger_price=None),
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
        age_hours=2.0,
    )


def _price_lookup(ticker: str) -> float:
    prices = {"NVDA": 520.0}
    if ticker not in prices:
        raise KeyError(ticker)
    return prices[ticker]


# ---------------------------------------------------------------------------
# ValidationToolState fixtures (uses the real evaluate_proposals library)
# ---------------------------------------------------------------------------


def _build_library_config(
    *,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
) -> LibraryConfig:
    zones = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
    effective_limits = {
        "position_max_size_pct": _PER_POSITION_MAX_PCT,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 30.0,
        "gross_exposure_pct": 120.0,
        "options_delta_pct": 40.0,
        "portfolio_theta_pct_per_day": 0.18,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType({k: zones for k in effective_limits}),
        feature_flags=FeatureFlagsView(
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
        ),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )


def _build_library_snapshot() -> PortfolioStateSnapshot:
    # ``position_max_size_pct`` is the largest current position weight; new
    # proposals would push it higher only when the proposal itself exceeds
    # the existing max. Using 4.0 (the master fixture's WARNING-zone position)
    # leaves room for small proposals to PASS.
    return PortfolioStateSnapshot(
        portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
        cash_usd=10_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 18.5, "semis": 12.0, "financials": 8.5, "energy": 4.0}
        ),
        net_long_pct=42.0,
        net_short_pct=10.0,
        gross_pct=78.0,
        options_delta_pct=10.0,
        portfolio_theta_pct_per_day=0.10,
        portfolio_vega_pct_per_iv_point=0.6,
        total_short_pct=8.0,
        single_short_max_pct=2.0,
        daily_borrow_cost_pct=0.01,
        position_max_size_pct=4.0,
        existing_positions=MappingProxyType({}),
    )


def _build_market() -> MarketInputs:
    expiration = date(2026, 5, 28)
    iv_provider = FixtureIvProvider(
        surface={
            "AAPL": IvSurfaceEntry(
                underlying=Symbol("AAPL"),
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=expiration,
                        contract_type=ContractType.CALL,
                        implied_volatility=0.30,
                    ),
                    IvQuote(
                        strike=100.0,
                        expiration=expiration,
                        contract_type=ContractType.PUT,
                        implied_volatility=0.30,
                    ),
                ),
            ),
        },
        realized_vol={},
    )
    return MarketInputs(
        underlying_prices=MappingProxyType({"AAPL": 100.0, "NVDA": 500.0, "GOOG": 150.0}),
        risk_free_rate=0.045,
        iv_provider=iv_provider,
        as_of=_TIMESTAMP,
    )


def _sector_for_ticker(ticker: str) -> str:
    mapping = {
        "NVDA": "tech",
        "AAPL": "tech",
        "MSFT": "tech",
        "AMD": "semis",
        "MU": "semis",
        "AVGO": "semis",
        "JPM": "financials",
        "GS": "financials",
        "BAC": "financials",
        "XOM": "energy",
        "CVX": "energy",
        "GOOG": "tech",
    }
    return mapping.get(ticker, "tech")


def _build_validation_tool_state(
    *,
    options_enabled: bool = True,
    short_selling_enabled: bool = True,
    accumulated_deltas: tuple[ProjectedDelta, ...] = (),
) -> ValidationToolState:
    config = _build_library_config(
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
    )
    return ValidationToolState(
        invocation_id=_INVOCATION_ID,
        starting_snapshot=_build_library_snapshot(),
        starting_risk_budget=_make_full_risk_budget(),
        starting_active_risk_parameters=_make_active_risk_parameters(),
        profile_feature_flags=config.feature_flags,
        library_config=config,
        library_market=_build_market(),
        sector_resolver=_sector_for_ticker,
        accumulated_deltas=accumulated_deltas,
    )


def _equity_request(
    *,
    ticker: str = "AAPL",
    direction: Direction = Direction.LONG,
    quantity: int = 10,
    dollar_value: float = 1_000.0,
    action: ValidationAction = ValidationAction.OPEN,
) -> ValidationRequest:
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker=ticker,
            asset_type=InstrumentType.EQUITY,
            direction=direction,
        ),
        size=ValidationSize(quantity=quantity, dollar_value=dollar_value),
        action=action,
    )


def _option_request() -> ValidationRequest:
    return ValidationRequest(
        instrument=ValidationInstrument(
            ticker=Symbol("AAPL"),
            asset_type=InstrumentType.OPTIONS,
            direction=Direction.LONG,
            strike=100.0,
            expiration=datetime(2026, 5, 28, tzinfo=UTC),
            contract_type="call",
        ),
        size=ValidationSize(quantity=2, dollar_value=2_000.0, premium_at_risk_usd=2_000.0),
        action=ValidationAction.OPEN,
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestNormalHeaderRendering:
    """Three audience headers against the master fixture; cross-block invariants."""

    def test_analyst_header_full_system(self) -> None:
        rendered = _render_normal_analyst_header()
        expected = _read_fixture("analyst_normal.txt")
        _assert_lines_equal(rendered, expected)

    def test_strategist_header_full_system(self) -> None:
        rendered = _render_normal_strategist_header()
        expected = _read_fixture("strategist_normal.txt")
        _assert_lines_equal(rendered, expected)

    def test_pm_header_full_system(self) -> None:
        rendered = _render_normal_pm_header()
        expected = _read_fixture("pm_normal.txt")
        _assert_lines_equal(rendered, expected)

    def test_pm_header_full_system_with_correlation_and_dependency(self) -> None:
        """Exercise the PM header with both correlation_state and dependency_risk_flag populated.

        The non-populated path is covered by ``test_pm_header_full_system``; this
        case anchors the byte-format of the threshold-gated correlation and
        dependency-risk blocks against a committed fixture.
        """
        rendered = _render_normal_pm_header(
            correlation_state=_build_correlation_state(),
            dependency_risk_flag=_build_dependency_risk_flag(),
        )
        expected = _read_fixture("pm_normal_with_correlation.txt")
        _assert_lines_equal(rendered, expected)

    def test_analyst_header_block_inventory(self) -> None:
        rendered = _render_normal_analyst_header()
        # Envelope, regime, capital, sector headroom, directional, options,
        # held positions, abandoned openings, hard blocks (for WARNING+CRITICAL
        # rendering, no breaching entries here since none are BLOCKED zone).
        assert "=== GUARDRAIL STATE" in rendered
        assert "Regime: normal [unchanged]" in rendered
        assert "Capital:" in rendered
        assert "Sector headroom (delta-adjusted):" in rendered
        assert "Directional headroom:" in rendered
        assert "Options headroom:" in rendered
        held_positions_header = (
            "Held positions (dedup — skip same underlying + direction; "
            "strategist owns hold/add/reduce):"
        )
        assert held_positions_header in rendered
        # Count held-position rows by walking from the header until the next blank line.
        lines = rendered.splitlines()
        held_idx = lines.index(held_positions_header)
        held_count = 0
        for line in lines[held_idx + 1 :]:
            if line == "":
                break
            held_count += 1
        assert held_count == 12

    def test_strategist_header_block_inventory(self) -> None:
        rendered = _render_normal_strategist_header()
        assert "Position-level constraint proximity:" in rendered
        assert "[⚠ WARNING: size]" in rendered  # POS-NVDA-001 at 4.0% / 5.0% = 0.80 size ratio
        # Sector breakdown: 4 active sectors + 1 unclassified group
        assert "  Tech (" in rendered
        assert "  Semis (" in rendered
        assert "  Financials (" in rendered
        assert "  Energy (" in rendered
        assert "  Unclassified:" in rendered
        # Drawdown state — no cumulative tier line
        assert "Drawdown state:" in rendered
        assert "Cumulative tier:" not in rendered
        # Abandoned actions has 4 rows
        actions_header = (
            "Abandoned position actions from prior invocation "
            "(decide on current grounds whether to re-propose):"
        )
        assert actions_header in rendered
        lines = rendered.splitlines()
        actions_idx = lines.index(actions_header)
        action_count = 0
        for line in lines[actions_idx + 1 :]:
            if line == "" or line == "===" or not line.startswith("  ENV-"):
                break
            action_count += 1
        assert action_count == 4

    def test_pm_header_block_inventory(self) -> None:
        rendered = _render_normal_pm_header()
        assert "Cross-constraint impact summary:" in rendered
        assert "Recent engine-originated actions (since last invocation):" in rendered
        assert "Active regime overrides:" in rendered
        assert "Correlation state:" not in rendered  # correlation_state=None
        assert "Dependency risk flag:" not in rendered  # dependency_risk_flag=None

    def test_three_renderers_share_per_position_proximity_block(self) -> None:
        strategist = _render_normal_strategist_header()
        pm = _render_normal_pm_header()
        # Per-position proximity is a shared block produced by both renderers
        # via render_position_proximity_block. Confirm both surface the same
        # per-position rows by comparing the position_id ordering.
        strategist_proximity = _extract_block(strategist, "Position-level constraint proximity:")
        pm_proximity = _extract_block(pm, "Position-level constraint proximity:")
        strategist_ids = [
            row.strip().split(":")[0]
            for row in strategist_proximity[1:]
            if row.strip().startswith("POS-")
        ]
        pm_ids = [
            row.strip().split(":")[0] for row in pm_proximity[1:] if row.strip().startswith("POS-")
        ]
        assert strategist_ids == pm_ids
        assert len(strategist_ids) == 12

    def test_three_renderers_share_sector_breakdown_block(self) -> None:
        strategist = _render_normal_strategist_header()
        pm = _render_normal_pm_header()
        # Both surface a sector breakdown block listing positions per sector.
        strategist_block = _extract_block(strategist, "Sector exposure breakdown (per position):")
        pm_block = _extract_block(pm, "Sector exposure breakdown (per position):")
        # Both expose the same 4 sector groups via render_sector_breakdown_block.
        for label in ("  Tech (", "  Semis (", "  Financials (", "  Energy ("):
            assert any(row.startswith(label) for row in strategist_block)
            assert any(row.startswith(label) for row in pm_block)

    def test_strategist_and_pm_share_cumulative_tier_line(self) -> None:
        """Both renderers must emit the same Cumulative-tier line shape.

        The PM renderer used to emit ``Cumulative tier: CONSTRAINED`` (raw
        enum) while the strategist emitted ``Cumulative tier: constrained —
        max position size 3%, ...`` (lowercased label + restrictions). This
        test enforces line-by-line parity to trip future divergences.
        """
        drawdown = _make_drawdown_with_tier(DrawdownTier.CONSTRAINED)
        strategist = _render_normal_strategist_header(drawdown=drawdown)
        pm = _render_normal_pm_header(drawdown=drawdown)
        expected_line = (
            "  Cumulative tier: constrained — max position size 3%, "
            "max gross 80%, positions w/ unrealized loss > 10% flagged"
        )
        assert expected_line in strategist.splitlines()
        assert expected_line in pm.splitlines()

    def test_strategist_and_pm_share_regime_transition_breach_rows(self) -> None:
        """Both renderers must emit identical regime-transition-breach rows.

        Asserts (a) the ``[{rule_label}]`` suffix on per-position
        non-``position_max_size_pct`` breaches, (b) absence of the suffix on
        per-position ``position_max_size_pct`` breaches, and (c) byte-identical
        row text across both renderers.
        """
        per_pos_max = RegimeTransitionBreach(
            position_id=PositionId("POS-NVDA-001"),
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.2,
            new_limit_value=3.5,
            overage=0.7,
            unit="% of portfolio",
        )
        per_pos_other = RegimeTransitionBreach(
            position_id=PositionId("POS-AMD-004"),
            rule_id="single_short_max_pct",
            rule_label="Single short max size",
            current_value=3.2,
            new_limit_value=3.0,
            overage=0.2,
            unit="% of portfolio",
        )
        aggregate = RegimeTransitionBreach(
            position_id=None,
            rule_id="sector_concentration_tech",
            rule_label="Sector concentration (Tech)",
            current_value=28.0,
            new_limit_value=20.0,
            overage=8.0,
            unit="% of portfolio",
        )
        breaches = (per_pos_max, per_pos_other, aggregate)
        strategist = _render_normal_strategist_header(regime_transition_breaches=breaches)
        pm = _render_normal_pm_header(regime_transition_breaches=breaches)
        strategist_block = _extract_block(strategist, "Regime-transition breaches (if any):")
        pm_block = _extract_block(pm, "Regime-transition breaches (if any):")
        # Byte-identical: both renderers share the helper now.
        assert strategist_block == pm_block
        # Confirm the per-position non-max-size rule renders the [label] suffix.
        assert any("[Single short max size]" in row for row in strategist_block), (
            "per-position non-max-size breach must carry [{rule_label}] suffix"
        )
        assert any("[Single short max size]" in row for row in pm_block)
        # Confirm the per-position max_size rule does NOT carry the suffix.
        max_size_row = next(row for row in strategist_block if "POS-NVDA-001:" in row)
        assert "[Per-position max size]" not in max_size_row

    def test_pm_and_strategist_reject_non_percent_unit_breach(self) -> None:
        """The unit invariant must trip when a breach uses a non-percentage unit."""
        bad_breach = RegimeTransitionBreach(
            position_id=PositionId("POS-NVDA-001"),
            rule_id="position_max_size_pct",
            rule_label="Per-position max size",
            current_value=4.2,
            new_limit_value=3.5,
            overage=0.7,
            unit="USD",
        )
        with pytest.raises(ValueError, match="must be a percentage form"):
            _render_normal_strategist_header(regime_transition_breaches=(bad_breach,))
        with pytest.raises(ValueError, match="must be a percentage form"):
            _render_normal_pm_header(regime_transition_breaches=(bad_breach,))

    def test_no_renderer_introduces_double_blank_lines(self) -> None:
        for renderer in (
            _render_normal_analyst_header,
            _render_normal_strategist_header,
            _render_normal_pm_header,
        ):
            rendered = renderer()
            for prev_line, next_line in pairwise(rendered.splitlines()):
                assert not (prev_line == "" and next_line == ""), (
                    f"double blank line in {renderer.__name__}"
                )

    def test_no_renderer_emits_trailing_blank(self) -> None:
        for renderer in (
            _render_normal_analyst_header,
            _render_normal_strategist_header,
            _render_normal_pm_header,
        ):
            rendered = renderer()
            assert rendered.splitlines()[-1] == "==="
            assert not rendered.endswith("\n")


class TestHaltModeWrappers:
    """Three halt-mode wrappers; HaltState validator; pending-orders variants."""

    def test_analyst_halt_mode(self) -> None:
        halt_state = _make_halt_state()
        rendered = render_analyst_header_halt_mode(
            halt_state=halt_state,
            analyst_view=_build_analyst_view(),
            risk_budget=_make_full_risk_budget(),
            active_risk_parameters=_make_active_risk_parameters(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
        )
        # Banner present, halt-mode capital block present
        assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in rendered
        assert "Mode: WATCHLIST ONLY — do not generate trade proposals" in rendered
        assert "  New positions: BLOCKED (halt active)" in rendered
        assert "  Per-position max size: not applicable (halt mode)" in rendered
        # Other blocks identical (held positions, abandoned openings)
        assert "Held positions (dedup" in rendered
        assert "Abandoned openings from prior invocation" in rendered

    def test_strategist_halt_mode_defensive_posture(self) -> None:
        halt_state = _make_halt_state()
        rendered = render_strategist_header_halt_mode(
            halt_state=halt_state,
            strategist_view=_build_strategist_view(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
            sector_resolver=_build_sector_resolver(),
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            position_zones=_DEFAULT_POSITION_ZONES,
        )
        assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in rendered
        assert (
            "Mode: DEFENSIVE POSTURE — focus on risk reduction for existing positions" in rendered
        )
        # Blocks below the banner are the same as the normal strategist header.
        normal = _render_normal_strategist_header()
        # Normal lines minus the envelope-open are present in halt-mode output.
        for normal_line in normal.splitlines()[1:]:
            assert normal_line in rendered.splitlines()

    def test_pm_halt_mode_risk_reduction(self) -> None:
        halt_state = _make_halt_state()
        rendered = render_pm_header_halt_mode(
            halt_state=halt_state,
            pm_view=_build_pm_view(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
            sector_resolver=_build_sector_resolver(),
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            cross_constraint_impact=_build_cross_constraint_impact(),
            pending_orders=(_build_pending_order(),),
            current_price_lookup=_price_lookup,
            position_zones=_DEFAULT_POSITION_ZONES,
        )
        # Banner has three lines
        assert "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **" in rendered
        assert "Available actions: CLOSE, ADJUST, CANCEL only" in rendered
        assert "Blocked actions: OPEN, ADD" in rendered
        # Pending orders block inserted
        assert "Pending orders review:" in rendered
        # Cross-constraint scoped variant
        assert "Scoped to risk-reducing actions only (CLOSE, ADJUST, CANCEL)." in rendered
        # Hard blocks block has halt-mode action lines appended
        assert "  OPEN: BLOCKED (halt mode)" in rendered
        assert "  ADD: BLOCKED (halt mode)" in rendered

    def test_pm_halt_mode_no_pending_orders(self) -> None:
        halt_state = _make_halt_state()
        rendered = render_pm_header_halt_mode(
            halt_state=halt_state,
            pm_view=_build_pm_view(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
            sector_resolver=_build_sector_resolver(),
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            cross_constraint_impact=_build_cross_constraint_impact(),
            pending_orders=(),
            current_price_lookup=_price_lookup,
            position_zones=_DEFAULT_POSITION_ZONES,
        )
        # Pending-orders-review block has the None line
        lines = rendered.splitlines()
        review_idx = lines.index("Pending orders review:")
        assert lines[review_idx + 1] == "  None"

    def test_halt_state_validator(self) -> None:
        with pytest.raises(ValueError, match="at least one halt is active"):
            HaltState(
                daily_halt_active=False,
                cumulative_full_halt_active=False,
                daily_drawdown_pct=1.0,
                daily_drawdown_limit_pct=2.5,
            )


class TestEmergencyWrapper:
    """Emergency block on each kind of header; per-trigger rendering; minute formatting."""

    def test_emergency_block_appended_to_normal_analyst_header(self) -> None:
        normal = _render_normal_analyst_header()
        context = EmergencyContext(
            trigger=EmergencyTrigger.REGIME_JUMP,
            trigger_detail="Regime jump: low-vol → crisis (VIX 12 → 38)",
            minutes_since_last_invocation=27.0,
            normal_cadence_minutes=120.0,
        )
        composed = prepend_emergency_block(header=normal, context=context)
        lines = composed.splitlines()
        # Envelope-open at line 0; emergency block at lines 1-3; blank at 4.
        assert lines[0] == normal.splitlines()[0]
        assert lines[1] == "** EMERGENCY INVOCATION — trigger: regime_jump **"
        assert lines[2] == "Trigger detail: Regime jump: low-vol → crisis (VIX 12 → 38)"
        assert lines[3] == "Time since last invocation: 27m (normal cadence: ~120m)"
        assert lines[4] == ""
        # The rest of the normal header (from line 1 onward) follows verbatim.
        assert lines[5:] == normal.splitlines()[1:]

    def test_emergency_block_appended_to_halt_mode_pm_header(self) -> None:
        halt_state = _make_halt_state()
        halt_pm = render_pm_header_halt_mode(
            halt_state=halt_state,
            pm_view=_build_pm_view(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
            sector_resolver=_build_sector_resolver(),
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            cross_constraint_impact=_build_cross_constraint_impact(),
            pending_orders=(_build_pending_order(),),
            current_price_lookup=_price_lookup,
            position_zones=_DEFAULT_POSITION_ZONES,
        )
        context = EmergencyContext(
            trigger=EmergencyTrigger.MULTI_RULE_BREACH,
            trigger_detail="3 rules in CRITICAL zone",
            minutes_since_last_invocation=15.0,
            normal_cadence_minutes=60.0,
        )
        composed = prepend_emergency_block(header=halt_pm, context=context)
        lines = composed.splitlines()
        # Envelope-open, then emergency block, then blank, then halt-mode banner
        assert lines[0].startswith("=== GUARDRAIL STATE")
        assert lines[1] == "** EMERGENCY INVOCATION — trigger: multi_rule_breach **"
        assert lines[5] == "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **"

    @pytest.mark.parametrize("trigger", list(EmergencyTrigger))
    def test_emergency_block_for_each_trigger_type(self, trigger: EmergencyTrigger) -> None:
        context = EmergencyContext(
            trigger=trigger,
            trigger_detail=f"detail for {trigger.value}",
            minutes_since_last_invocation=20.0,
            normal_cadence_minutes=60.0,
        )
        rendered = render_emergency_block(context)
        assert f"trigger: {trigger.value}" in rendered
        assert f"detail for {trigger.value}" in rendered

    def test_emergency_block_minutes_formatting(self) -> None:
        context = EmergencyContext(
            trigger=EmergencyTrigger.MARGIN_CALL,
            trigger_detail="x",
            minutes_since_last_invocation=27.4,
            normal_cadence_minutes=120.0,
        )
        rendered = render_emergency_block(context)
        assert "Time since last invocation: 27m (normal cadence: ~120m)" in rendered

    def test_emergency_wrapper_rejects_missing_envelope_open(self) -> None:
        bad_header = "Regime: normal [unchanged]\nCapital:\n==="
        context = EmergencyContext(
            trigger=EmergencyTrigger.REGIME_JUMP,
            trigger_detail="x",
            minutes_since_last_invocation=15.0,
            normal_cadence_minutes=60.0,
        )
        with pytest.raises(ValueError, match="envelope-open"):
            prepend_emergency_block(header=bad_header, context=context)


class TestValidationTool:
    """Validation tool against the real ``evaluate_proposals`` library."""

    def test_validation_tool_pass_path(self) -> None:
        state = _build_validation_tool_state()
        result = validate_guardrail(request=_equity_request(dollar_value=500.0), state=state)
        assert result.overall == "PASS"
        assert result.proposal_index_in_invocation == 1
        assert result.cumulative_impact_note == (
            "This is proposal #1 in this invocation. "
            "No prior proposals affect headroom calculations."
        )
        assert result.failure_guidance is None

    def test_validation_tool_fail_path(self) -> None:
        # Construct a snapshot near the sector limit so a large tech proposal
        # triggers the sector-concentration FAIL path.
        state = _build_validation_tool_state()
        # Override snapshot to put tech sector near limit; use a low
        # ``position_max_size_pct`` so the ``position_max_size_pct`` rule does
        # not also fire (keeping this test focused on a single rule failure).
        snapshot = PortfolioStateSnapshot(
            portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            cash_usd=20_000.0,
            reserved_for_pending_orders_usd=0.0,
            sector_exposure_pct=MappingProxyType(
                {"tech": 22.0, "semis": 5.0, "financials": 5.0, "energy": 5.0}
            ),
            net_long_pct=37.0,
            net_short_pct=0.0,
            gross_pct=37.0,
            options_delta_pct=0.0,
            portfolio_theta_pct_per_day=0.0,
            portfolio_vega_pct_per_iv_point=0.0,
            total_short_pct=0.0,
            single_short_max_pct=0.0,
            daily_borrow_cost_pct=0.0,
            position_max_size_pct=2.0,
            existing_positions=MappingProxyType({}),
        )
        # Override config so position_max_size_pct does not also fire.
        config = _build_library_config()
        loose = dict(config.effective_limits)
        loose["position_max_size_pct"] = 50.0
        relaxed = LibraryConfig(
            effective_limits=MappingProxyType(loose),
            escalation_zones=config.escalation_zones,
            feature_flags=config.feature_flags,
            active_sectors=config.active_sectors,
            active_regime=config.active_regime,
            active_profile=config.active_profile,
            conservative_buffer_pct=config.conservative_buffer_pct,
        )
        state = state.model_copy(update={"starting_snapshot": snapshot, "library_config": relaxed})
        # Propose a 5K tech equity (10% of portfolio) — pushes tech 22% → 32%.
        request = _equity_request(ticker=Symbol("AAPL"), dollar_value=5_000.0)
        result = validate_guardrail(request=request, state=state)
        assert result.overall == "FAIL"
        assert result.failure_guidance is not None
        # Single-rule guidance contains "Reduce size by"; the rule label is
        # also present so the agent knows which rule to address.
        assert "Reduce size by" in result.failure_guidance
        assert "sector_concentration_tech" in result.failure_guidance
        # Equity proposal — greeks must be None
        assert result.greeks is None

    def test_validation_tool_cumulative_tracking(self) -> None:
        # Three sequential proposals: first two each 2% of portfolio, third 5%.
        # Tighten the net_long limit so the third (cumulative 39%) fails while
        # the first two (cumulative 34%) stay below the hard-block threshold.
        config = _build_library_config()
        lower = dict(config.effective_limits)
        lower["net_long_pct"] = 40.0
        lower["sector_concentration_pct"] = 100.0  # don't trip sector
        lower["position_max_size_pct"] = 50.0
        config = LibraryConfig(
            effective_limits=MappingProxyType(lower),
            escalation_zones=config.escalation_zones,
            feature_flags=config.feature_flags,
            active_sectors=config.active_sectors,
            active_regime=config.active_regime,
            active_profile=config.active_profile,
            conservative_buffer_pct=config.conservative_buffer_pct,
        )
        snapshot = PortfolioStateSnapshot(
            portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            cash_usd=20_000.0,
            reserved_for_pending_orders_usd=0.0,
            sector_exposure_pct=MappingProxyType(
                {"tech": 5.0, "semis": 5.0, "financials": 5.0, "energy": 5.0}
            ),
            net_long_pct=30.0,
            net_short_pct=0.0,
            gross_pct=30.0,
            options_delta_pct=0.0,
            portfolio_theta_pct_per_day=0.0,
            portfolio_vega_pct_per_iv_point=0.0,
            total_short_pct=0.0,
            single_short_max_pct=0.0,
            daily_borrow_cost_pct=0.0,
            position_max_size_pct=2.0,
            existing_positions=MappingProxyType({}),
        )
        state = ValidationToolState(
            invocation_id=_INVOCATION_ID,
            starting_snapshot=snapshot,
            starting_risk_budget=_make_full_risk_budget(),
            starting_active_risk_parameters=_make_active_risk_parameters(),
            profile_feature_flags=config.feature_flags,
            library_config=config,
            library_market=_build_market(),
            sector_resolver=_sector_for_ticker,
        )
        # Each 1K request is 2% of the 50K portfolio.
        request1 = _equity_request(ticker=Symbol("AAPL"), dollar_value=1_000.0)
        result1 = validate_guardrail(request=request1, state=state)
        assert result1.overall == "PASS"
        delta1 = ProjectedDelta(
            instrument=request1.instrument,
            size=request1.size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=result1.delta_adjusted_exposure,
            greeks=result1.greeks,
            proposal_index=1,
        )
        state = state.with_accepted_proposal(delta1)

        request2 = _equity_request(ticker=Symbol("NVDA"), dollar_value=1_000.0)
        result2 = validate_guardrail(request=request2, state=state)
        assert result2.overall == "PASS"
        delta2 = ProjectedDelta(
            instrument=request2.instrument,
            size=request2.size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=result2.delta_adjusted_exposure,
            greeks=result2.greeks,
            proposal_index=2,
        )
        state = state.with_accepted_proposal(delta2)
        assert len(state.accumulated_deltas) == 2

        # Third proposal: 5% (2_500 USD) → cumulative 30 + 2 + 2 + 5 = 39 > 35
        request3 = _equity_request(ticker=Symbol("GOOG"), dollar_value=2_500.0)
        result3 = validate_guardrail(request=request3, state=state)
        assert result3.overall == "FAIL"

    def test_validation_tool_feature_flag_early_exit(self) -> None:
        state = _build_validation_tool_state(options_enabled=False)
        result = validate_guardrail(request=_option_request(), state=state)
        assert result.overall == "FAIL"
        assert result.per_rule == ()
        assert result.failure_guidance == "Options trading is disabled for this portfolio profile."

    def test_validation_tool_state_immutability(self) -> None:
        state = _build_validation_tool_state()
        delta = ProjectedDelta(
            instrument=_equity_request().instrument,
            size=_equity_request().size,
            action=ValidationAction.OPEN,
            sector="tech",
            delta_adjusted_exposure=1_000.0,
            greeks=None,
            proposal_index=1,
        )
        new_state = state.with_accepted_proposal(delta)
        assert state.accumulated_deltas == ()
        assert new_state.accumulated_deltas == (delta,)
        assert new_state is not state

    def test_validation_tool_options_greeks_populated(self) -> None:
        state = _build_validation_tool_state()
        result = validate_guardrail(request=_option_request(), state=state)
        # Options proposal — greeks populated by the library.
        assert result.greeks is not None
        # Greeks dataclass exposes delta/gamma/theta/vega — sanity-check delta.
        assert result.greeks.delta != 0.0


class TestComposition:
    """Cross-cutting tests confirming surfaces compose correctly."""

    def test_emergency_plus_halt_mode_plus_pm_header(self) -> None:
        halt_state = _make_halt_state()
        halt_pm = render_pm_header_halt_mode(
            halt_state=halt_state,
            pm_view=_build_pm_view(),
            invocation_id=_INVOCATION_ID,
            timestamp=_TIMESTAMP,
            options_enabled=True,
            short_selling_enabled=True,
            active_sectors=("tech", "semis", "financials", "energy"),
            config=_make_state_delivery_config(),
            sector_label_display=_FULL_SECTOR_LABELS,
            sector_resolver=_build_sector_resolver(),
            total_portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            available_for_new_positions_usd=_AVAILABLE_FOR_NEW_POSITIONS_USD,
            cross_constraint_impact=_build_cross_constraint_impact(),
            pending_orders=(_build_pending_order(),),
            current_price_lookup=_price_lookup,
            position_zones=_DEFAULT_POSITION_ZONES,
        )
        context = EmergencyContext(
            trigger=EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY,
            trigger_detail="-2.6% in 30 minutes",
            minutes_since_last_invocation=30.0,
            normal_cadence_minutes=60.0,
        )
        composed = prepend_emergency_block(header=halt_pm, context=context)
        lines = composed.splitlines()
        # Order: envelope-open → emergency block → blank → halt-mode banner
        assert lines[0].startswith("=== GUARDRAIL STATE")
        assert lines[1] == "** EMERGENCY INVOCATION — trigger: daily_drawdown_velocity **"
        assert lines[2] == "Trigger detail: -2.6% in 30 minutes"
        assert lines[3] == "Time since last invocation: 30m (normal cadence: ~60m)"
        assert lines[4] == ""
        assert lines[5] == "** HALT MODE ACTIVE — daily drawdown 2.6% / 2.5% **"
        # No double blank lines anywhere
        for prev_line, next_line in pairwise(lines):
            assert not (prev_line == "" and next_line == "")

    def test_validation_tool_state_starts_from_snapshot_fixture(self) -> None:
        state = _build_validation_tool_state()
        assert state.starting_snapshot is not None
        assert state.starting_risk_budget is not None
        assert state.starting_active_risk_parameters is not None
        assert state.accumulated_deltas == ()
        assert state.profile_feature_flags.options_enabled is True
        assert state.profile_feature_flags.short_selling_enabled is True

    def test_three_audience_headers_use_same_snapshot(self) -> None:
        analyst = _render_normal_analyst_header()
        strategist = _render_normal_strategist_header()
        pm = _render_normal_pm_header()
        # All three headers share the envelope-open and regime line.
        a_lines = analyst.splitlines()
        s_lines = strategist.splitlines()
        p_lines = pm.splitlines()
        assert a_lines[0] == s_lines[0] == p_lines[0]  # envelope-open
        assert a_lines[1] == s_lines[1] == p_lines[1]  # regime line
        # Capital block header is identical across all three.
        for header in (analyst, strategist, pm):
            assert "Capital:" in header
            assert "  Available for new positions: $5,000 (10.0% of portfolio)" in header
            assert "  Per-position max size: $2,500 (5.0% of portfolio, normal regime)" in header

    def test_renderer_outputs_are_pure_strings(self) -> None:
        analyst = _render_normal_analyst_header()
        strategist = _render_normal_strategist_header()
        pm = _render_normal_pm_header()
        for rendered in (analyst, strategist, pm):
            assert isinstance(rendered, str)
            # No BOM
            assert not rendered.startswith("﻿")
            # UTF-8 round-trip
            assert rendered.encode("utf-8").decode("utf-8") == rendered
        # Concatenation is a string with no embedded BOMs
        joined = "\n\n".join((analyst, strategist, pm))
        assert isinstance(joined, str)
        assert "﻿" not in joined

    def test_full_invocation_simulation(self) -> None:
        # 1. Build snapshot (already done implicitly by builders)
        # 2. Build ValidationToolState
        config = _build_library_config()
        lower = dict(config.effective_limits)
        lower["net_long_pct"] = 40.0
        lower["sector_concentration_pct"] = 100.0
        lower["position_max_size_pct"] = 50.0
        config = LibraryConfig(
            effective_limits=MappingProxyType(lower),
            escalation_zones=config.escalation_zones,
            feature_flags=config.feature_flags,
            active_sectors=config.active_sectors,
            active_regime=config.active_regime,
            active_profile=config.active_profile,
            conservative_buffer_pct=config.conservative_buffer_pct,
        )
        snapshot = PortfolioStateSnapshot(
            portfolio_value_usd=_TOTAL_PORTFOLIO_VALUE_USD,
            cash_usd=20_000.0,
            reserved_for_pending_orders_usd=0.0,
            sector_exposure_pct=MappingProxyType(
                {"tech": 5.0, "semis": 5.0, "financials": 5.0, "energy": 5.0}
            ),
            net_long_pct=30.0,
            net_short_pct=0.0,
            gross_pct=30.0,
            options_delta_pct=0.0,
            portfolio_theta_pct_per_day=0.0,
            portfolio_vega_pct_per_iv_point=0.0,
            total_short_pct=0.0,
            single_short_max_pct=0.0,
            daily_borrow_cost_pct=0.0,
            position_max_size_pct=2.0,
            existing_positions=MappingProxyType({}),
        )
        state = ValidationToolState(
            invocation_id=_INVOCATION_ID,
            starting_snapshot=snapshot,
            starting_risk_budget=_make_full_risk_budget(),
            starting_active_risk_parameters=_make_active_risk_parameters(),
            profile_feature_flags=config.feature_flags,
            library_config=config,
            library_market=_build_market(),
            sector_resolver=_sector_for_ticker,
        )

        # 3. Three proposals — two accepted, one rejected
        results: list[ValidationResult] = []
        for ticker, dollars in (("AAPL", 1_000.0), ("NVDA", 1_000.0), ("GOOG", 2_500.0)):
            result = validate_guardrail(
                request=_equity_request(ticker=ticker, dollar_value=dollars),
                state=state,
            )
            results.append(result)
            if result.overall == "PASS":
                delta = ProjectedDelta(
                    instrument=ValidationInstrument(
                        ticker=ticker,
                        asset_type=InstrumentType.EQUITY,
                        direction=Direction.LONG,
                    ),
                    size=ValidationSize(quantity=10, dollar_value=dollars),
                    action=ValidationAction.OPEN,
                    sector="tech",
                    delta_adjusted_exposure=result.delta_adjusted_exposure,
                    greeks=result.greeks,
                    proposal_index=len(state.accumulated_deltas) + 1,
                )
                state = state.with_accepted_proposal(delta)
        assert results[0].overall == "PASS"
        assert results[1].overall == "PASS"
        assert results[2].overall == "FAIL"
        # cumulative_impact_note text changes after first proposal
        assert "No prior proposals affect headroom" in results[0].cumulative_impact_note
        assert "Cumulative impact of proposals #1-1" in results[1].cumulative_impact_note
        assert "Cumulative impact of proposals #1-2" in results[2].cumulative_impact_note

        # 4-6. Render all three headers
        analyst = _render_normal_analyst_header()
        strategist = _render_normal_strategist_header()
        pm_with_correlation = _render_normal_pm_header(
            correlation_state=_build_correlation_state(),
            dependency_risk_flag=_build_dependency_risk_flag(),
        )

        # 7. Confirm invariants
        for rendered in (analyst, strategist, pm_with_correlation):
            for prev_line, next_line in pairwise(rendered.splitlines()):
                assert not (prev_line == "" and next_line == "")
            assert rendered.splitlines()[-1] == "==="

        # The PM with correlation/dependency variants surface those blocks.
        assert "Correlation state:" in pm_with_correlation
        assert "Dependency risk flag:" in pm_with_correlation


# ---------------------------------------------------------------------------
# Internal helper used by inventory tests
# ---------------------------------------------------------------------------


def _extract_block(rendered: str, header: str) -> list[str]:
    """Return the header line plus all following lines until the next blank line."""
    lines = rendered.splitlines()
    idx = lines.index(header)
    block = [header]
    for line in lines[idx + 1 :]:
        if line == "":
            break
        block.append(line)
    return block
