"""Tests for the library-shape snapshot translator (ALP-402).

Tests cover field-by-field derivation rules from §2 and the existing_positions
map construction from §3 of the story spec.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime

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
from alphamind.portfolio_state.records.cash import CashLedger
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
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    AssetType,
    Greeks,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    Direction as LibDirection,
)
from alphamind.risk_guardrails.library_snapshot import to_library_snapshot

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
_PHASE1 = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)
_INV_ID = "inv-test-001"

_OPTION_EXPIRY = date(2026, 2, 21)


def _sector_resolver(ticker: str) -> str:
    mapping = {"AAPL": "tech", "NVDA": "semis", "JPM": "financials", "XOM": "energy"}
    return mapping.get(ticker, "UNKNOWN")


def _borrow_cost_resolver(ticker: str) -> float:
    # Fixed daily borrow cost in USD per ticker
    costs = {"XOM": 5.0, "JPM": 2.0}
    return costs.get(ticker, 0.0)


# ---------------------------------------------------------------------------
# Pydantic PortfolioStateSnapshot builders
# ---------------------------------------------------------------------------


def _make_pydantic_snapshot(
    open_positions: Sequence[PositionView] = (),
    pending_positions: Sequence[PositionView] = (),
    sector_exposure: Sequence[SectorExposureEntry] = (),
    directional_exposure: DirectionalExposure | None = None,
    cash_ledger: CashLedger | None = None,
    active_risk_parameters: ActiveRiskParameterSet | None = None,
    pending_orders: Sequence[OrderRecord] = (),
) -> PortfolioStateSnapshot:
    """Build a minimal PortfolioStateSnapshot for testing."""
    if directional_exposure is None:
        directional_exposure = DirectionalExposure(
            total_long_delta_adjusted_usd=money(0.0),
            total_short_delta_adjusted_usd=money(0.0),
            net_directional_pct_of_portfolio=0.0,
            gross_pct_of_portfolio=0.0,
        )

    if cash_ledger is None:
        cash_ledger = CashLedger(
            current_cash_usd=80_000.0,
            settled_cash_usd=80_000.0,
            reserved_capital_usd=0.0,
            available_buying_power_usd=80_000.0,
            margin_held_usd=0.0,
            unsettled_proceeds=(),
            cash_pct_of_portfolio=80.0,
            true_deployable_capital_usd=80_000.0,
            regt_excess_trailing_30d_usd=0.0,
            regt_excess_trailing_90d_usd=0.0,
            regt_excess_lifetime_usd=0.0,
        )

    if active_risk_parameters is None:
        active_risk_parameters = ActiveRiskParameterSet(
            regime_label=RegimeLabel.NORMAL,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(
                ActiveRiskParameterEntry(
                    rule_id="position_max_size_pct",
                    rule_label="Max position size",
                    value=5.0,
                    unit="%",
                    regime_multiplier_applied=1.0,
                    base_value=5.0,
                ),
            ),
            active_overlays=(),
        )

    pnl = PortfolioPnL(
        total_unrealized_pnl_usd=money(0.0),
        total_unrealized_pnl_pct_of_portfolio=0.0,
        daily_realized_pnl_usd=money(0.0),
        daily_total_pnl_usd=money(0.0),
        cumulative_realized_pnl_usd=money(0.0),
        rolling_realized_pnl={
            "1d": money(0.0),
            "3d": money(0.0),
            "5d": money(0.0),
            "20d": money(0.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )
    drawdown = DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )
    risk_budget = RiskBudgetConsumption(
        entries=(
            RiskBudgetEntry(
                rule_id="position_max_size_pct",
                rule_label="Max position size",
                current_value=0.0,
                limit_value=5.0,
                headroom=5.0,
                headroom_pct_of_limit=100.0,
                zone=RiskZone.NORMAL,
                unit="%",
                cumulative_invocation_impact_value=0.0,
            ),
        ),
    )
    thesis_quality = ThesisQualityAggregate(
        as_of_timestamp=_NOW,
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
    return PortfolioStateSnapshot(
        invocation_id=_INV_ID,
        phase1_committed_at=_PHASE1,
        snapshot_assembled_at=_NOW,
        open_positions=tuple(open_positions),
        pending_positions=tuple(pending_positions),
        sector_exposure=tuple(sector_exposure),
        directional_exposure=directional_exposure,
        portfolio_pnl=pnl,
        drawdown=drawdown,
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=cash_ledger,
        pending_orders=tuple(pending_orders),
        risk_budget=risk_budget,
        active_risk_parameters=active_risk_parameters,
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=thesis_quality,
        brackets=(),
    )


def _make_equity_position_view(
    position_id: str,
    ticker: str,
    direction: Direction,
    share_count: float,
    market_value_usd: float,
    notional_usd: float,
    delta_adjusted_usd: float,
    position_weight_pct: float,
    status: PositionStatus | None = None,
    borrow_rate_pct: float | None = None,
    locate_status: LocateStatus | None = None,
    margin_held_usd: float | None = None,
    execution_history: tuple[PositionFill, ...] | None = None,
) -> PositionView:
    if status is None:
        status = PositionStatus.OPEN

    if direction == Direction.SHORT:
        # Short positions require borrow fields
        details: EquityPositionDetails = EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=share_count,
            average_cost_basis_per_share=market_value_usd / max(share_count, 1),
            borrow_rate_pct=borrow_rate_pct if borrow_rate_pct is not None else 0.5,
            locate_status=locate_status if locate_status is not None else LocateStatus.LOCATED,
            margin_held_usd=margin_held_usd if margin_held_usd is not None else notional_usd * 0.5,
        )
    else:
        details = EquityPositionDetails(
            ticker=Symbol(ticker),
            share_count=share_count,
            average_cost_basis_per_share=market_value_usd / max(share_count, 1),
        )

    resolved_history: tuple[PositionFill, ...]
    if execution_history is None:
        if status == PositionStatus.OPEN:
            resolved_history = (
                PositionFill(
                    fill_timestamp=_PHASE1,
                    fill_price=price(market_value_usd / max(share_count, 1)),
                    fill_quantity=share_count,
                    slippage=signed_money(0.0),
                    fees=money(0.0),
                ),
            )
        else:
            resolved_history = ()
    else:
        resolved_history = execution_history

    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=_PHASE1 if status == PositionStatus.OPEN else None,
        details=details,
        execution_history=resolved_history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(market_value_usd),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=1.0,
        notional_exposure_usd=money(notional_usd),
        delta_adjusted_exposure_usd=signed_money(delta_adjusted_usd),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_options_position_view(
    position_id: str,
    underlying: str,
    direction: Direction,
    contract_count: float,
    market_value_usd: float,
    notional_usd: float,
    delta_adjusted_usd: float,
    position_weight_pct: float,
    greeks_delta: float = 0.5,
    greeks_gamma: float = 0.02,
    greeks_theta: float = -0.10,
    greeks_vega: float = 0.30,
    status: PositionStatus | None = None,
) -> PositionView:
    if status is None:
        status = PositionStatus.OPEN

    greeks = OptionGreeks(
        delta=greeks_delta,
        gamma=greeks_gamma,
        theta=greeks_theta,
        vega=greeks_vega,
        as_of_timestamp=_NOW,
        iv_used=0.30,
    )
    details_opt = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying),
        strike_price=100.0,
        expiration_date=_OPTION_EXPIRY,
        contract_type=OptionContractType.CALL,
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=greeks,
    )
    exec_hist: tuple[PositionFill, ...] = (
        (
            PositionFill(
                fill_timestamp=_PHASE1,
                fill_price=price(5.0),
                fill_quantity=contract_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        )
        if status == PositionStatus.OPEN
        else ()
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=_PHASE1 if status == PositionStatus.OPEN else None,
        details=details_opt,
        execution_history=exec_hist,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(market_value_usd),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=1.0,
        notional_exposure_usd=money(notional_usd),
        delta_adjusted_exposure_usd=signed_money(delta_adjusted_usd),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


# ---------------------------------------------------------------------------
# Test 1 — portfolio-level scalar fields (AC: portfolio_value_usd, cash_usd,
# reserved_for_pending_orders_usd mirror Pydantic snapshot derivation rules)
# ---------------------------------------------------------------------------


def test_portfolio_value_cash_reserved_scalars() -> None:
    """AC: portfolio_value_usd, cash_usd, reserved_for_pending_orders_usd match §2 rules."""
    cash_ledger = CashLedger(
        current_cash_usd=70_000.0,
        settled_cash_usd=70_000.0,
        reserved_capital_usd=5_000.0,
        available_buying_power_usd=65_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=70.0,
        true_deployable_capital_usd=65_000.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )

    long_pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=17_500.0,
        notional_usd=17_500.0,
        delta_adjusted_usd=17_500.0,
        position_weight_pct=17.5,
    )
    short_pos = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )

    snapshot = _make_pydantic_snapshot(
        open_positions=[long_pos, short_pos],
        cash_ledger=cash_ledger,
    )
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # portfolio_value_usd = sum(market values) + cash
    expected_portfolio_value = 17_500.0 + 5_500.0 + 70_000.0
    assert lib.portfolio_value_usd == pytest.approx(expected_portfolio_value)
    assert lib.cash_usd == pytest.approx(70_000.0)
    assert lib.reserved_for_pending_orders_usd == pytest.approx(5_000.0)


# ---------------------------------------------------------------------------
# Test 2 — sector_exposure_pct (AC: gross long+short per sector)
# ---------------------------------------------------------------------------


def test_sector_exposure_pct_gross() -> None:
    """AC: sector_exposure_pct[sector] = long_pct + short_pct for that sector."""
    sector_entries = [
        SectorExposureEntry(
            sector="tech",
            long_delta_adjusted_usd=money(8_000.0),
            short_delta_adjusted_usd=money(0.0),
            long_pct_of_portfolio=8.0,
            short_pct_of_portfolio=0.0,
            long_short_ratio=None,
        ),
        SectorExposureEntry(
            sector="energy",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(3_000.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=3.0,
            long_short_ratio=None,
        ),
    ]
    snapshot = _make_pydantic_snapshot(sector_exposure=sector_entries)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.sector_exposure_pct["tech"] == pytest.approx(8.0)
    assert lib.sector_exposure_pct["energy"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Test 3 — net_long_pct / net_short_pct non-negative and difference = net
# ---------------------------------------------------------------------------


def test_net_long_pct_net_short_pct() -> None:
    """AC: net_long_pct and net_short_pct non-negative; difference = directional_net."""
    # net short scenario: net_directional = -12.0
    dir_exp = DirectionalExposure(
        total_long_delta_adjusted_usd=money(5_000.0),
        total_short_delta_adjusted_usd=money(17_000.0),
        net_directional_pct_of_portfolio=-12.0,
        gross_pct_of_portfolio=22.0,
    )
    snapshot = _make_pydantic_snapshot(directional_exposure=dir_exp)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.net_long_pct >= 0.0
    assert lib.net_short_pct >= 0.0
    # net_long_pct - net_short_pct should equal the directional net
    assert lib.net_long_pct - lib.net_short_pct == pytest.approx(-12.0)

    # net long scenario
    dir_exp_long = DirectionalExposure(
        total_long_delta_adjusted_usd=money(15_000.0),
        total_short_delta_adjusted_usd=money(3_000.0),
        net_directional_pct_of_portfolio=12.0,
        gross_pct_of_portfolio=18.0,
    )
    snapshot2 = _make_pydantic_snapshot(directional_exposure=dir_exp_long)
    lib2 = to_library_snapshot(snapshot2, sector_resolver=_sector_resolver)

    assert lib2.net_long_pct >= 0.0
    assert lib2.net_short_pct >= 0.0
    assert lib2.net_long_pct - lib2.net_short_pct == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# Test 4 — gross_pct passes through directional_exposure
# ---------------------------------------------------------------------------


def test_gross_pct_passthrough() -> None:
    """AC: gross_pct = directional_exposure.gross_pct_of_portfolio."""
    dir_exp = DirectionalExposure(
        total_long_delta_adjusted_usd=money(20_000.0),
        total_short_delta_adjusted_usd=money(5_000.0),
        net_directional_pct_of_portfolio=15.0,
        gross_pct_of_portfolio=25.0,
    )
    snapshot = _make_pydantic_snapshot(directional_exposure=dir_exp)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.gross_pct == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Test 5 — position_max_size_pct reads the rule_id and raises on absence
# ---------------------------------------------------------------------------


def test_position_max_size_pct_reads_rule() -> None:
    """AC: position_max_size_pct matches the rule entry; ValueError if absent."""
    params = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Max position size",
                value=4.2,
                unit="%",
                regime_multiplier_applied=1.0,
                base_value=4.2,
            ),
        ),
        active_overlays=(),
    )
    snapshot = _make_pydantic_snapshot(active_risk_parameters=params)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    assert lib.position_max_size_pct == pytest.approx(4.2)


def test_position_max_size_pct_missing_raises() -> None:
    """AC: ValueError raised when position_max_size_pct rule is absent."""
    params = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),  # no entries at all
        active_overlays=(),
    )
    snapshot = _make_pydantic_snapshot(active_risk_parameters=params)
    with pytest.raises(ValueError, match="position_max_size_pct"):
        to_library_snapshot(snapshot, sector_resolver=_sector_resolver)


# ---------------------------------------------------------------------------
# Test 6 — existing_positions: one entry per resolvable position, keyed by position_id
# ---------------------------------------------------------------------------


def test_existing_positions_map_keys() -> None:
    """AC: existing_positions has one entry per resolvable position."""
    pos1 = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=17_500.0,
        notional_usd=17_500.0,
        delta_adjusted_usd=17_500.0,
        position_weight_pct=17.5,
    )
    pos2 = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos1, pos2])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert set(lib.existing_positions.keys()) == {"POS-AAPL", "POS-XOM"}


def test_existing_positions_equity_long_fields() -> None:
    """AC: ExistingPosition fields for an equity LONG position are correct."""
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=17_500.0,
        notional_usd=17_500.0,
        delta_adjusted_usd=17_500.0,
        position_weight_pct=17.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    ep = lib.existing_positions["POS-AAPL"]
    assert ep.position_id == "POS-AAPL"
    assert ep.underlying == "AAPL"
    assert ep.sector == "tech"
    assert ep.direction == LibDirection.LONG
    assert ep.asset_type == AssetType.EQUITY
    assert ep.notional_usd == pytest.approx(17_500.0)
    assert ep.delta_adjusted_exposure_usd == pytest.approx(17_500.0)
    assert ep.current_greeks is None  # equity has no greeks
    assert ep.daily_borrow_cost_usd is None  # LONG has no borrow cost
    assert ep.quantity == pytest.approx(100.0)  # share_count


# ---------------------------------------------------------------------------
# Test 7 — SHORT equity: borrow_cost_resolver populates daily_borrow_cost_usd
# and daily_borrow_cost_pct reflects the contribution
# ---------------------------------------------------------------------------


def test_short_equity_borrow_cost() -> None:
    """AC: ExistingPosition.daily_borrow_cost_usd non-None for SHORT equity with resolver."""
    pos = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    # Only open position + cash => portfolio_value = 5500 + 80000
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(
        snapshot,
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=_borrow_cost_resolver,
    )

    ep = lib.existing_positions["POS-XOM"]
    assert ep.daily_borrow_cost_usd is not None
    assert ep.daily_borrow_cost_usd == pytest.approx(5.0)  # _borrow_cost_resolver("XOM") = 5.0

    # daily_borrow_cost_pct = sum(borrow costs) / portfolio_value * 100 — must match
    # the library's _borrow_cost_contribute formula (cost_usd / portfolio_value * 100.0)
    # so the read scale matches the write scale exactly.
    portfolio_value = 5_500.0 + 80_000.0
    expected_pct = 5.0 / portfolio_value * 100
    assert lib.daily_borrow_cost_pct == pytest.approx(expected_pct)


def test_short_equity_no_borrow_resolver() -> None:
    """AC: daily_borrow_cost_usd is None when borrow_cost_resolver is None."""
    pos = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)  # no resolver

    ep = lib.existing_positions["POS-XOM"]
    assert ep.daily_borrow_cost_usd is None
    assert lib.daily_borrow_cost_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 8 — options position: current_greeks non-None with delta/gamma/theta/vega
# ---------------------------------------------------------------------------


def test_options_position_greeks() -> None:
    """AC: ExistingPosition.current_greeks non-None for OPTIONS with greeks present."""
    opt_pos = _make_options_position_view(
        "POS-OPT",
        "NVDA",
        Direction.LONG,
        contract_count=2.0,
        market_value_usd=1_000.0,
        notional_usd=1_000.0,
        delta_adjusted_usd=800.0,
        position_weight_pct=1.0,
        greeks_delta=0.60,
        greeks_gamma=0.03,
        greeks_theta=-0.15,
        greeks_vega=0.40,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[opt_pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    ep = lib.existing_positions["POS-OPT"]
    assert ep.current_greeks is not None
    assert isinstance(ep.current_greeks, Greeks)
    assert ep.current_greeks.delta == pytest.approx(0.60)
    assert ep.current_greeks.gamma == pytest.approx(0.03)
    assert ep.current_greeks.theta == pytest.approx(-0.15)
    assert ep.current_greeks.vega == pytest.approx(0.40)
    assert ep.asset_type == AssetType.OPTION
    assert ep.quantity == pytest.approx(2.0)  # contract_count


# ---------------------------------------------------------------------------
# Test 9 — total_short_pct and single_short_max_pct
# ---------------------------------------------------------------------------


def test_total_short_pct_and_single_short_max_pct() -> None:
    """AC: total_short_pct = sum of short_pct; single_short_max_pct = max SHORT weight."""
    # Two short positions: XOM at -5.5%, JPM at -2.0%
    pos_xom = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,  # always-positive (compute_position_weight_pct uses abs)
    )
    pos_jpm = _make_equity_position_view(
        "POS-JPM",
        "JPM",
        Direction.SHORT,
        share_count=25.0,
        market_value_usd=5_000.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=-5_000.0,
        position_weight_pct=2.0,
    )
    sector_entries = [
        SectorExposureEntry(
            sector="energy",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(5_500.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=5.5,
            long_short_ratio=None,
        ),
        SectorExposureEntry(
            sector="financials",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(5_000.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=2.0,
            long_short_ratio=None,
        ),
    ]
    snapshot = _make_pydantic_snapshot(
        open_positions=[pos_xom, pos_jpm],
        sector_exposure=sector_entries,
    )
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.total_short_pct == pytest.approx(5.5 + 2.0)
    # single_short_max_pct = max |position_weight_pct| for SHORT positions
    assert lib.single_short_max_pct == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# Test 10 — unresolvable position is skipped with WARNING log
# ---------------------------------------------------------------------------


def test_unresolvable_position_skipped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """AC: Positions with no ticker are skipped; WARNING is logged."""
    # A STRATEGY position with empty legs — resolve_ticker returns None
    strategy_details = StrategyPositionDetails(
        strategy_type_label="iron_condor",
        legs=(),  # empty → no ticker extractable
        net_premium_usd=-100.0,
        max_profit_usd=200.0,
        max_loss_usd=-500.0,
        breakeven_levels=(),
        strategy_greeks=OptionGreeks(
            delta=0.0,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
        ),
    )
    record = PositionRecord(
        position_id=PositionId("POS-STRATEGY-EMPTY"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PHASE1,
        details=strategy_details,
        execution_history=(
            PositionFill(
                fill_timestamp=_PHASE1,
                fill_price=price(1.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    pos = PositionView(
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

    # Also add a valid equity position
    valid_pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=10.0,
        market_value_usd=1_750.0,
        notional_usd=1_750.0,
        delta_adjusted_usd=1_750.0,
        position_weight_pct=1.75,
    )

    snapshot = _make_pydantic_snapshot(open_positions=[pos, valid_pos])

    with caplog.at_level(logging.WARNING, logger="alphamind.risk_guardrails.library_snapshot"):
        lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # Unresolvable position is not in the map
    assert "POS-STRATEGY-EMPTY" not in lib.existing_positions
    # Valid position IS in the map
    assert "POS-AAPL" in lib.existing_positions
    # A WARNING was emitted
    assert any("POS-STRATEGY-EMPTY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 11 — purity: calling twice with same inputs returns equal results
# ---------------------------------------------------------------------------


def test_purity_equal_outputs() -> None:
    """AC: to_library_snapshot is pure — same inputs produce equal results."""
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=17_500.0,
        notional_usd=17_500.0,
        delta_adjusted_usd=17_500.0,
        position_weight_pct=17.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])

    lib1 = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    lib2 = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib1.portfolio_value_usd == lib2.portfolio_value_usd
    assert lib1.cash_usd == lib2.cash_usd
    assert lib1.existing_positions == lib2.existing_positions
    # The full snapshots should be equal (dataclass equality)
    assert lib1 == lib2


# ---------------------------------------------------------------------------
# Test 12 — LibrarySnapshot is exported as TypeAlias from library_snapshot module
# ---------------------------------------------------------------------------


def test_library_snapshot_type_alias_exported() -> None:
    """AC: LibrarySnapshot is exported from alphamind.risk_guardrails.library_snapshot."""
    import alphamind.risk_guardrails.library_snapshot as mod

    assert "LibrarySnapshot" in dir(mod)
    assert "to_library_snapshot" in dir(mod)
    assert hasattr(mod, "__all__")
    assert "LibrarySnapshot" in mod.__all__
    assert "to_library_snapshot" in mod.__all__


# ---------------------------------------------------------------------------
# Test 13 — Full normal scenario: equity LONG, equity SHORT, options position
# (covers the verification scenario in the acceptance criteria)
# ---------------------------------------------------------------------------


def test_full_normal_scenario() -> None:
    """AC: Full scenario covering equity LONG, SHORT, options; field-by-field check."""
    # Positions
    aapl_long = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=17_500.0,
        notional_usd=17_500.0,
        delta_adjusted_usd=17_500.0,
        position_weight_pct=17.5,
    )
    xom_short = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    nvda_opt = _make_options_position_view(
        "POS-NVDA-OPT",
        "NVDA",
        Direction.LONG,
        contract_count=3.0,
        market_value_usd=900.0,
        notional_usd=900.0,
        delta_adjusted_usd=700.0,
        position_weight_pct=0.9,
        greeks_delta=0.55,
        greeks_gamma=0.025,
        greeks_theta=-0.12,
        greeks_vega=0.35,
    )

    cash_ledger = CashLedger(
        current_cash_usd=70_000.0,
        settled_cash_usd=70_000.0,
        reserved_capital_usd=1_000.0,
        available_buying_power_usd=69_000.0,
        margin_held_usd=2_750.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=73.9,
        true_deployable_capital_usd=67_000.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    dir_exp = DirectionalExposure(
        total_long_delta_adjusted_usd=money(18_200.0),
        total_short_delta_adjusted_usd=money(5_500.0),
        net_directional_pct_of_portfolio=12.7,
        gross_pct_of_portfolio=23.7,
    )
    sector_entries = [
        SectorExposureEntry(
            sector="tech",
            long_delta_adjusted_usd=money(17_500.0),
            short_delta_adjusted_usd=money(0.0),
            long_pct_of_portfolio=18.45,
            short_pct_of_portfolio=0.0,
            long_short_ratio=None,
        ),
        SectorExposureEntry(
            sector="energy",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(5_500.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=5.79,
            long_short_ratio=None,
        ),
        SectorExposureEntry(
            sector="semis",
            long_delta_adjusted_usd=money(700.0),
            short_delta_adjusted_usd=money(0.0),
            long_pct_of_portfolio=0.74,
            short_pct_of_portfolio=0.0,
            long_short_ratio=None,
        ),
    ]
    params = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="position_max_size_pct",
                rule_label="Max position size",
                value=5.0,
                unit="%",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )

    snapshot = _make_pydantic_snapshot(
        open_positions=[aapl_long, xom_short, nvda_opt],
        sector_exposure=sector_entries,
        directional_exposure=dir_exp,
        cash_ledger=cash_ledger,
        active_risk_parameters=params,
    )

    lib = to_library_snapshot(
        snapshot,
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=_borrow_cost_resolver,
    )

    # existing_positions keys
    assert set(lib.existing_positions.keys()) == {"POS-AAPL", "POS-XOM", "POS-NVDA-OPT"}

    # AAPL long equity
    aapl = lib.existing_positions["POS-AAPL"]
    assert aapl.direction == LibDirection.LONG
    assert aapl.asset_type == AssetType.EQUITY
    assert aapl.current_greeks is None
    assert aapl.daily_borrow_cost_usd is None
    assert aapl.quantity == pytest.approx(100.0)

    # XOM short equity with borrow cost
    xom = lib.existing_positions["POS-XOM"]
    assert xom.direction == LibDirection.SHORT
    assert xom.asset_type == AssetType.EQUITY
    assert xom.current_greeks is None
    assert xom.daily_borrow_cost_usd is not None
    assert xom.daily_borrow_cost_usd == pytest.approx(5.0)

    # NVDA options
    nvda = lib.existing_positions["POS-NVDA-OPT"]
    assert nvda.direction == LibDirection.LONG
    assert nvda.asset_type == AssetType.OPTION
    assert nvda.current_greeks is not None
    assert nvda.current_greeks.delta == pytest.approx(0.55)
    assert nvda.quantity == pytest.approx(3.0)

    # Portfolio scalars
    expected_pv = 17_500.0 + 5_500.0 + 900.0 + 70_000.0
    assert lib.portfolio_value_usd == pytest.approx(expected_pv)
    assert lib.cash_usd == pytest.approx(70_000.0)
    assert lib.reserved_for_pending_orders_usd == pytest.approx(1_000.0)
    assert lib.position_max_size_pct == pytest.approx(5.0)

    # Directional
    assert lib.net_long_pct >= 0.0
    assert lib.net_short_pct >= 0.0
    assert lib.gross_pct == pytest.approx(23.7)
    assert lib.net_long_pct - lib.net_short_pct == pytest.approx(12.7)

    # Sector exposure (gross = long + short per sector)
    assert lib.sector_exposure_pct["tech"] == pytest.approx(18.45 + 0.0)
    assert lib.sector_exposure_pct["energy"] == pytest.approx(0.0 + 5.79)

    # Short metrics
    assert lib.total_short_pct == pytest.approx(5.79)
    assert lib.single_short_max_pct == pytest.approx(5.5)

    # Borrow cost pct — matches library's _borrow_cost_contribute scale
    expected_borrow_pct = 5.0 / expected_pv * 100
    assert lib.daily_borrow_cost_pct == pytest.approx(expected_borrow_pct)


# ---------------------------------------------------------------------------
# Test 14 — pending positions are included in existing_positions and portfolio value
# ---------------------------------------------------------------------------


def test_pending_positions_included() -> None:
    """AC: pending_positions contribute to existing_positions and portfolio_value_usd."""
    pending_pos = _make_equity_position_view(
        "POS-PENDING",
        "JPM",
        Direction.LONG,
        share_count=10.0,
        market_value_usd=2_000.0,
        notional_usd=2_000.0,
        delta_adjusted_usd=2_000.0,
        position_weight_pct=2.0,
        status=PositionStatus.PENDING,
        execution_history=(),
    )

    snapshot = _make_pydantic_snapshot(pending_positions=[pending_pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # Pending position included in existing_positions
    assert "POS-PENDING" in lib.existing_positions

    # And in portfolio_value_usd
    expected_pv = 2_000.0 + 80_000.0  # position MV + cash
    assert lib.portfolio_value_usd == pytest.approx(expected_pv)


# ---------------------------------------------------------------------------
# Test 15 — strategy position with legs: ticker extracted from first leg
# ---------------------------------------------------------------------------


def test_strategy_position_ticker_and_greeks() -> None:
    """AC: Strategy position ticker from first leg; current_greeks from strategy_greeks."""
    leg_options = OptionsPositionDetails(
        underlying_ticker=Symbol("NVDA"),
        strike_price=100.0,
        expiration_date=_OPTION_EXPIRY,
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(delta=0.5, gamma=0.02, theta=-0.10, vega=0.30, as_of_timestamp=_NOW),
    )
    leg = StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=leg_options)
    strategy_greeks = OptionGreeks(
        delta=0.3,
        gamma=0.01,
        theta=-0.05,
        vega=0.20,
        as_of_timestamp=_NOW,
    )
    details = StrategyPositionDetails(
        strategy_type_label="bull_spread",
        legs=(leg,),
        net_premium_usd=-200.0,
        max_profit_usd=800.0,
        max_loss_usd=-200.0,
        breakeven_levels=(102.0,),
        strategy_greeks=strategy_greeks,
    )
    record = PositionRecord(
        position_id=PositionId("POS-STRAT"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_PHASE1,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=_PHASE1,
                fill_price=price(2.0),
                fill_quantity=2.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    pos = PositionView(
        record=record,
        current_market_value_usd=signed_money(400.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=0.5,
        position_age_hours=1.0,
        notional_exposure_usd=money(400.0),
        delta_adjusted_exposure_usd=signed_money(300.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )

    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert "POS-STRAT" in lib.existing_positions
    ep = lib.existing_positions["POS-STRAT"]
    assert ep.underlying == "NVDA"
    assert ep.asset_type == AssetType.STRATEGY
    assert ep.current_greeks is not None
    assert ep.current_greeks.delta == pytest.approx(0.3)
    assert ep.current_greeks.vega == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# ALP-506 — reserves_capital_usd derivation from pending entry/add-entry orders
# ---------------------------------------------------------------------------


def _make_pending_entry_order(
    *,
    order_id: str,
    position_id: str,
    ticker: str,
    role: OrderRole,
    limit_price: float | None = 100.0,
    stop_trigger_price: float | None = None,
    remaining_quantity: float = 50.0,
    filled_quantity: float = 0.0,
    order_type: OrderType = OrderType.LIMIT,
) -> OrderRecord:
    pp = PriceParameters(limit_price=limit_price, stop_trigger_price=stop_trigger_price)
    quantity = filled_quantity + remaining_quantity
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(f"BRK-{order_id}"),
        role=role,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=OrderDirection.BUY,
        order_type=order_type,
        price_parameters=pp,
        quantity=quantity,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_PHASE1,
        last_update_timestamp=_PHASE1,
        filled_quantity=filled_quantity,
        avg_fill_price=None,
        remaining_quantity=remaining_quantity,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )


def test_existing_positions_reserves_capital_from_pending_entry_limit() -> None:
    """ALP-506: a PENDING position with a pending entry LIMIT order gets its
    ``reserves_capital_usd`` populated as ``limit_price * remaining_quantity``,
    mirroring the OMS-side ``_order_notional_estimate`` formula in
    ``execution/write_paths/phase2/cancel.py``.
    """
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=50.0,
        market_value_usd=0.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=5_000.0,
        position_weight_pct=5.0,
        status=PositionStatus.PENDING,
    )
    order = _make_pending_entry_order(
        order_id="ORD-1",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ENTRY,
        limit_price=100.0,
        remaining_quantity=50.0,
    )
    snapshot = _make_pydantic_snapshot(pending_positions=[pos], pending_orders=[order])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    ep = lib.existing_positions["POS-AAPL"]
    assert ep.reserves_capital_usd == pytest.approx(5_000.0)


def test_existing_positions_reserves_capital_open_position_with_add_entry() -> None:
    """ADD_ENTRY pending limit on an OPEN position contributes the same way."""
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=10_000.0,
        notional_usd=10_000.0,
        delta_adjusted_usd=10_000.0,
        position_weight_pct=10.0,
    )
    order = _make_pending_entry_order(
        order_id="ORD-ADD-1",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ADD_ENTRY,
        limit_price=105.0,
        remaining_quantity=20.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos], pending_orders=[order])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    ep = lib.existing_positions["POS-AAPL"]
    assert ep.reserves_capital_usd == pytest.approx(2_100.0)


def test_existing_positions_reserves_capital_protective_legs_ignored() -> None:
    """Protective legs (TAKE_PROFIT/PRICE_STOP/TIME_STOP/CLOSE) reserve no
    capital — ``_release_capital`` is only invoked for ENTRY / ADD_ENTRY.
    """
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=10_000.0,
        notional_usd=10_000.0,
        delta_adjusted_usd=10_000.0,
        position_weight_pct=10.0,
    )
    take_profit = _make_pending_entry_order(
        order_id="ORD-TP",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.TAKE_PROFIT,
        limit_price=120.0,
        remaining_quantity=100.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos], pending_orders=[take_profit])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(0.0)


def test_existing_positions_reserves_capital_market_order_zero() -> None:
    """MARKET entry orders carry no price parameters → no reservation."""
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=50.0,
        market_value_usd=0.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=5_000.0,
        position_weight_pct=5.0,
        status=PositionStatus.PENDING,
    )
    order = _make_pending_entry_order(
        order_id="ORD-MKT",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ENTRY,
        limit_price=None,
        stop_trigger_price=None,
        remaining_quantity=50.0,
        order_type=OrderType.MARKET,
    )
    snapshot = _make_pydantic_snapshot(pending_positions=[pos], pending_orders=[order])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(0.0)


def test_existing_positions_reserves_capital_stop_trigger_price_fallback() -> None:
    """STOP_LIMIT and STOP entry orders fall back to ``stop_trigger_price``
    when ``limit_price`` is absent, matching ``_order_notional_estimate``.
    """
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=50.0,
        market_value_usd=0.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=5_000.0,
        position_weight_pct=5.0,
        status=PositionStatus.PENDING,
    )
    order = _make_pending_entry_order(
        order_id="ORD-STOP",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ENTRY,
        limit_price=None,
        stop_trigger_price=98.0,
        remaining_quantity=50.0,
        order_type=OrderType.STOP,
    )
    snapshot = _make_pydantic_snapshot(pending_positions=[pos], pending_orders=[order])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(4_900.0)


def test_existing_positions_reserves_capital_multiple_orders_summed() -> None:
    """Multiple entry-class pending orders on the same position sum together."""
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=10_000.0,
        notional_usd=10_000.0,
        delta_adjusted_usd=10_000.0,
        position_weight_pct=10.0,
    )
    order_a = _make_pending_entry_order(
        order_id="ORD-A",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ADD_ENTRY,
        limit_price=100.0,
        remaining_quantity=20.0,
    )
    order_b = _make_pending_entry_order(
        order_id="ORD-B",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ADD_ENTRY,
        limit_price=110.0,
        remaining_quantity=10.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos], pending_orders=[order_a, order_b])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # 100*20 + 110*10 = 2000 + 1100 = 3100
    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(3_100.0)


def test_cancel_contribution_releases_reserved_capital_end_to_end() -> None:
    """ALP-506 integration: a CANCEL proposal on a position backed by a pending
    entry limit order contributes the expected release magnitude to
    ``pending_order_capital_pct`` — the upstream zero-stamping bug that made
    this contribution silently 0.0.
    """
    from types import MappingProxyType

    from alphamind.risk_guardrails.guardrail_evaluation import (
        Action,
        DeltaAdjustedExposure,
        EscalationZones,
        FeatureFlagsView,
        LibraryConfig,
        ProposedDelta,
        build_active_specs,
    )

    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=50.0,
        market_value_usd=0.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=5_000.0,
        position_weight_pct=5.0,
        status=PositionStatus.PENDING,
    )
    order = _make_pending_entry_order(
        order_id="ORD-1",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ENTRY,
        limit_price=100.0,
        remaining_quantity=50.0,
    )
    cash_ledger = CashLedger(
        current_cash_usd=95_000.0,
        settled_cash_usd=95_000.0,
        reserved_capital_usd=5_000.0,
        available_buying_power_usd=90_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=95.0,
        true_deployable_capital_usd=90_000.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    snapshot = _make_pydantic_snapshot(
        pending_positions=[pos],
        pending_orders=[order],
        cash_ledger=cash_ledger,
    )
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    keys = ("min_cash_reserve_pct", "pending_order_capital_pct")
    config = LibraryConfig(
        effective_limits=MappingProxyType({k: 20.0 for k in keys}),
        escalation_zones=MappingProxyType(
            {k: EscalationZones(warning=70.0, critical=85.0, hard_block=95.0) for k in keys}
        ),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech",),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )
    spec = next(s for s in build_active_specs(config) if s.rule_id == "pending_order_capital_pct")
    proposal = ProposedDelta(
        id="P-CANCEL-1",
        underlying=Symbol("AAPL"),
        sector="tech",
        direction=LibDirection.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=0.0,
        quantity=50.0,
        option_legs=None,
        action=Action.CANCEL,
        existing_position_id="POS-AAPL",
    )
    dae = DeltaAdjustedExposure(
        proposal_id="P-CANCEL-1",
        signed_notional_usd=0.0,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )

    # Expected contribution releases the full reserved amount, scaled by
    # portfolio value (PENDING position has zero market value, so the cash
    # ledger is the entire denominator).
    expected_pct = -5_000.0 / 95_000.0 * 100.0
    assert spec.contribute(proposal, dae, lib, config) == pytest.approx(expected_pct)
