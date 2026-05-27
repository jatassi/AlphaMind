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
from alphamind.execution.continuous_monitor.greeks_refresh.recompute import (
    recompute_strategy_greeks,
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
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
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
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.borrow_cost import daily_borrow_cost_usd
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    DeltaAdjustedExposure,
    EscalationZones,
    FeatureFlagsView,
    LibraryConfig,
    ProposedDelta,
    build_active_specs,
)
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


def _borrow_cost_resolver(ticker: str) -> float | None:
    # Annualized borrow fee rate (percent) per ticker; None for a ticker the
    # borrow-cost store has no row for (the iBorrowDesk store is sparse).
    rates = {"XOM": 5.0, "JPM": 2.0}
    return rates.get(ticker)


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
            accrued_borrow_cost_usd=0.0,
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
# Test 5 — position_max_size_pct is the actual max position size across
# open_positions + pending_positions, not the rule's limit value (ALP-624).
# ---------------------------------------------------------------------------


def test_position_max_size_pct_actual_max_across_positions() -> None:
    """AC (ALP-624): position_max_size_pct is the actual maximum position size
    (as % of portfolio value) across open_positions + pending_positions — NOT
    the rule's limit value.

    Scenario from the post-mortem (inv-20260520T235728Z-047a54ce): a JPM
    position sized at 18.08% of portfolio with a 5.0% rule limit must produce
    state.position_max_size_pct == 18.08 (the actual max), not 5.0.
    """
    # JPM at 18.08% of an $100,000 portfolio → market value 18_080
    # plus cash of 81_920 → portfolio_value 100_000.
    jpm = _make_equity_position_view(
        "POS-JPM",
        "JPM",
        Direction.LONG,
        share_count=100.0,
        market_value_usd=18_080.0,
        notional_usd=18_080.0,
        delta_adjusted_usd=18_080.0,
        position_weight_pct=18.08,
    )
    cash_ledger = CashLedger(
        current_cash_usd=81_920.0,
        settled_cash_usd=81_920.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=81_920.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=81.92,
        true_deployable_capital_usd=81_920.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    # Rule limit 5.0%; the actual-max field must be 18.08 (NOT 5.0).
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
        open_positions=[jpm],
        cash_ledger=cash_ledger,
        active_risk_parameters=params,
    )
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    assert lib.position_max_size_pct == pytest.approx(18.08)


def test_position_max_size_pct_empty_book_is_zero() -> None:
    """AC (ALP-624): an empty book (no open + no pending positions) produces
    position_max_size_pct == 0.0, not None."""
    snapshot = _make_pydantic_snapshot()
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    assert lib.position_max_size_pct == 0.0


def test_position_max_size_pct_includes_pending_positions() -> None:
    """AC (ALP-624): pending positions count toward the actual-max
    computation, not just open positions."""
    pending = _make_equity_position_view(
        "POS-PENDING-NVDA",
        "NVDA",
        Direction.LONG,
        share_count=10.0,
        market_value_usd=15_000.0,
        notional_usd=15_000.0,
        delta_adjusted_usd=15_000.0,
        position_weight_pct=15.0,
        status=PositionStatus.PENDING,
        execution_history=(),
    )
    snapshot = _make_pydantic_snapshot(pending_positions=[pending])
    # portfolio_value = 15_000 + 80_000 (default cash) = 95_000
    # 15_000 / 95_000 * 100 = 15.789...
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    assert lib.position_max_size_pct == pytest.approx(15_000.0 / 95_000.0 * 100.0)


def test_position_max_size_pct_takes_max_of_short_and_long_by_abs() -> None:
    """AC (ALP-624 / ALP-621): the max is over |notional_exposure| so a SHORT
    with larger absolute notional outranks a smaller LONG."""
    aapl_long = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=50.0,
        market_value_usd=5_000.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=5_000.0,
        position_weight_pct=5.0,
    )
    # SHORT fixture treats market_value_usd as a magnitude (the existing
    # _make_equity_position_view helper does the same — see other SHORT
    # tests). The translator's abs() in the actual-max derivation handles
    # both the production signed convention and this magnitude convention.
    xom_short = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=120.0,
        market_value_usd=12_000.0,
        notional_usd=12_000.0,
        delta_adjusted_usd=-12_000.0,
        position_weight_pct=12.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[aapl_long, xom_short])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    # portfolio_value (per fixture convention) = 5_000 + 12_000 + 80_000 = 97_000
    # max |notional| / portfolio_value * 100 = 12_000 / 97_000 * 100
    expected = 12_000.0 / 97_000.0 * 100.0
    assert lib.position_max_size_pct == pytest.approx(expected)


def test_position_max_size_pct_zero_portfolio_value_guarded() -> None:
    """AC (ALP-624): when portfolio_value_usd is 0 the field is 0.0
    (no division-by-zero)."""
    # No positions, zero cash → portfolio_value_usd = 0
    empty_cash = CashLedger(
        current_cash_usd=0.0,
        settled_cash_usd=0.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=0.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    snapshot = _make_pydantic_snapshot(cash_ledger=empty_cash)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    assert lib.position_max_size_pct == 0.0


def test_position_max_size_pct_missing_rule_still_raises() -> None:
    """AC (ALP-624): the field is now derived from positions, but the
    translator still requires the position_max_size_pct rule to be present
    in active_risk_parameters (the rule's effective_limit_key lookup still
    flows through this registry — see the story scope note)."""
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


def test_position_max_size_pct_uses_notional_not_market_value_for_options() -> None:
    """AC (ALP-621 Finding 2): an OPTIONS position's
    ``current_market_value_usd`` is premium (e.g., $1k), while
    ``notional_exposure_usd`` is underlying exposure (e.g., $50k). The
    rule's projection math operates on proposal/position notional, so the
    snapshot field must use the same basis or the rule's actual and projected
    values disagree by the option leverage ratio.

    Scenario: a single options position with $1k market value (premium) but
    $50k notional exposure on a $100k book → ``position_max_size_pct`` must
    be 50.0 (not 1.0).
    """
    opt_pos = _make_options_position_view(
        "POS-OPT-LEVERED",
        "NVDA",
        Direction.LONG,
        contract_count=5.0,
        market_value_usd=1_000.0,  # premium paid
        notional_usd=50_000.0,  # underlying exposure
        delta_adjusted_usd=40_000.0,
        position_weight_pct=1.0,
    )
    cash_ledger = CashLedger(
        current_cash_usd=99_000.0,
        settled_cash_usd=99_000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=99_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=99.0,
        true_deployable_capital_usd=99_000.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[opt_pos], cash_ledger=cash_ledger)
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)
    # portfolio_value = 1_000 (mv) + 99_000 (cash) = 100_000
    # notional 50_000 / pv 100_000 * 100 = 50.0 — NOT 1_000 / 100_000 * 100 = 1.0
    assert lib.position_max_size_pct == pytest.approx(50.0)


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
    """AC: ExistingPosition.daily_borrow_cost_usd is the rate→USD conversion for
    a SHORT equity position whose ticker the borrow-cost resolver covers."""
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

    # The resolver yields XOM's annualized fee rate (5.0%); the translator
    # converts it against the position notional (5_500.0).
    expected_cost_usd = daily_borrow_cost_usd(notional_usd=5_500.0, annual_fee_pct=5.0)
    ep = lib.existing_positions["POS-XOM"]
    assert ep.daily_borrow_cost_usd is not None
    assert ep.daily_borrow_cost_usd == pytest.approx(expected_cost_usd)

    # daily_borrow_cost_pct must match the library's _borrow_cost_contribute
    # formula (cost_usd / portfolio_value * 100.0) so the read scale matches
    # the write scale exactly.
    portfolio_value = 5_500.0 + 80_000.0
    assert lib.daily_borrow_cost_pct == pytest.approx(expected_cost_usd / portfolio_value * 100)


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


def test_short_equity_borrow_resolver_uncovered_ticker() -> None:
    """AC (ALP-586): a SHORT equity whose ticker the resolver has no row for
    yields daily_borrow_cost_usd=None — a clean signal, not a crash or a
    silent 0.0 — and contributes nothing to daily_borrow_cost_pct."""
    pos = _make_equity_position_view(
        "POS-CSCO",
        "CSCO",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    # _borrow_cost_resolver covers XOM/JPM only; CSCO resolves to None.
    lib = to_library_snapshot(
        snapshot,
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=_borrow_cost_resolver,
    )

    ep = lib.existing_positions["POS-CSCO"]
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
    # single_short_max_position_id pins the largest short for the cascade dispatcher.
    assert lib.single_short_max_position_id == "POS-XOM"


def test_single_short_max_position_id_none_when_no_shorts() -> None:
    """AC: ``single_short_max_position_id`` is ``None`` when the book has no shorts."""
    snapshot = _make_pydantic_snapshot(open_positions=[], sector_exposure=[])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.single_short_max_pct == pytest.approx(0.0)
    assert lib.single_short_max_position_id is None


def test_single_short_max_position_id_breaks_ties_lexicographically() -> None:
    """AC: identical ``position_weight_pct`` resolves to the smallest ``position_id``.

    Determinism guard — equal books must produce equal projections so the
    cascade dispatcher routes the same breach to the same position on every
    tick.
    """
    pos_a = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_000.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=-5_000.0,
        position_weight_pct=4.0,
    )
    pos_x = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_000.0,
        notional_usd=5_000.0,
        delta_adjusted_usd=-5_000.0,
        position_weight_pct=4.0,
    )
    sector_entries = [
        SectorExposureEntry(
            sector="tech",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(5_000.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=4.0,
            long_short_ratio=None,
        ),
        SectorExposureEntry(
            sector="energy",
            long_delta_adjusted_usd=money(0.0),
            short_delta_adjusted_usd=money(5_000.0),
            long_pct_of_portfolio=0.0,
            short_pct_of_portfolio=4.0,
            long_short_ratio=None,
        ),
    ]
    snapshot = _make_pydantic_snapshot(
        open_positions=[pos_x, pos_a],  # input order reversed
        sector_exposure=sector_entries,
    )
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert lib.single_short_max_pct == pytest.approx(4.0)
    assert lib.single_short_max_position_id == "POS-AAPL"


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
        direction=None,
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
    assert xom.daily_borrow_cost_usd == pytest.approx(
        daily_borrow_cost_usd(notional_usd=5_500.0, annual_fee_pct=5.0)
    )

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
    # ALP-624: position_max_size_pct is the actual maximum position size, not
    # the rule's limit value. AAPL at 17_500 is the largest position; with
    # portfolio_value 93_900 that is 17_500/93_900*100 ≈ 18.638%.
    assert lib.position_max_size_pct == pytest.approx(17_500.0 / expected_pv * 100.0)

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
    expected_borrow_cost_usd = daily_borrow_cost_usd(notional_usd=5_500.0, annual_fee_pct=5.0)
    assert lib.daily_borrow_cost_pct == pytest.approx(expected_borrow_cost_usd / expected_pv * 100)


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
        direction=None,
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
    # ALP-603: a strategy has no position-level direction — the translator
    # emits ``None`` for it regardless of the persisted record's placeholder.
    assert ep.direction is None
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
    status: OrderStatus = OrderStatus.PENDING,
) -> OrderRecord:
    pp = PriceParameters(
        limit_price=price(str(limit_price)) if limit_price is not None else None,
        stop_trigger_price=(
            price(str(stop_trigger_price)) if stop_trigger_price is not None else None
        ),
    )
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
        status=status,
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


def test_existing_positions_reserves_capital_uses_remaining_quantity() -> None:
    """PARTIALLY_FILLED orders contribute ``remaining_quantity * px`` — not
    total ``quantity``. The OMS-side ``_order_notional_estimate`` uses the
    same field; a regression to ``quantity`` would over-state the release.
    """
    pos = _make_equity_position_view(
        "POS-AAPL",
        "AAPL",
        Direction.LONG,
        share_count=30.0,
        market_value_usd=3_000.0,
        notional_usd=3_000.0,
        delta_adjusted_usd=3_000.0,
        position_weight_pct=3.0,
    )
    order = _make_pending_entry_order(
        order_id="ORD-PARTIAL",
        position_id="POS-AAPL",
        ticker="AAPL",
        role=OrderRole.ADD_ENTRY,
        limit_price=100.0,
        filled_quantity=30.0,
        remaining_quantity=20.0,
        status=OrderStatus.PARTIALLY_FILLED,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos], pending_orders=[order])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # 100 * remaining_quantity (20) = 2000; would be 5000 if quantity were used
    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(2_000.0)


def test_existing_positions_reserves_capital_orphan_order_ignored() -> None:
    """A pending order whose position_id matches no open/pending position is
    silently dropped — the per-position map is keyed on positions present in
    the snapshot. The snapshot's own invariants (``snapshot.py``) make this
    state structurally improbable in production, but pinning the behavior
    documents the contract.
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
    orphan = _make_pending_entry_order(
        order_id="ORD-ORPHAN",
        position_id="POS-NONEXISTENT",
        ticker="AAPL",
        role=OrderRole.ENTRY,
        limit_price=99.0,
        remaining_quantity=10.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos], pending_orders=[orphan])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert set(lib.existing_positions.keys()) == {"POS-AAPL"}
    assert lib.existing_positions["POS-AAPL"].reserves_capital_usd == pytest.approx(0.0)


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
        notional_usd=money(0),
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


# ---------------------------------------------------------------------------
# ALP-595 — strategy portfolio-greek accumulation: net-signed, leg-summed
# ---------------------------------------------------------------------------


def _make_strategy_position_view(
    position_id: str,
    underlying: str,
    *,
    leg_specs: Sequence[tuple[float, float]],
    strategy_greeks: OptionGreeks,
    market_value_usd: float,
    notional_usd: float,
    delta_adjusted_usd: float,
    position_weight_pct: float,
) -> PositionView:
    """Build an OPEN multi-leg STRATEGY ``PositionView``.

    ``leg_specs`` is a sequence of ``(contract_count, contract_multiplier)``
    pairs — one per leg. A strategy record carries ``direction = None``
    (ALP-610); the strategy's directional contribution rides on
    ``strategy_greeks``, and direction reads route through
    ``position_direction()``, which yields ``None`` for a strategy.
    """
    legs = tuple(
        StrategyLeg(
            leg_id=f"leg-{i}",
            direction=Direction.LONG,
            options=OptionsPositionDetails(
                underlying_ticker=Symbol(underlying),
                strike_price=100.0 + i,
                expiration_date=_OPTION_EXPIRY,
                contract_type=OptionContractType.CALL,
                contract_count=count,
                contract_multiplier=multiplier,
                premium_paid_per_contract=5.0,
                greeks=OptionGreeks(
                    delta=0.5, gamma=0.02, theta=-0.10, vega=0.30, as_of_timestamp=_NOW
                ),
            ),
        )
        for i, (count, multiplier) in enumerate(leg_specs)
    )
    details = StrategyPositionDetails(
        strategy_type_label="bear_call_spread",
        legs=legs,
        net_premium_usd=-200.0,
        max_profit_usd=200.0,
        max_loss_usd=-800.0,
        breakeven_levels=(102.0,),
        strategy_greeks=strategy_greeks,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_PHASE1,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=_PHASE1,
                fill_price=price(2.0),
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


def test_net_short_delta_strategy_contributes_negative_portfolio_delta() -> None:
    """AC: a net-short-delta strategy contributes a negative delta to the
    portfolio greek view. The sign comes from ``strategy_greeks`` (net-signed
    per ALP-588 decision C), not from the position-level ``LONG`` placeholder."""
    short_delta_greeks = OptionGreeks(
        delta=-0.40,
        gamma=0.01,
        theta=0.05,
        vega=-0.20,
        as_of_timestamp=_NOW,
    )
    pos = _make_strategy_position_view(
        "POS-BEARCALL",
        "NVDA",
        leg_specs=[(1.0, 100.0), (1.0, 100.0)],
        strategy_greeks=short_delta_greeks,
        market_value_usd=400.0,
        notional_usd=400.0,
        delta_adjusted_usd=-300.0,
        position_weight_pct=0.4,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # strategy_greeks.delta is negative → portfolio delta contribution negative.
    assert lib.options_delta_pct < 0.0
    # theta sign is also taken from strategy_greeks (positive for a credit
    # strategy collecting decay), not flipped by a position-level direction.
    assert lib.portfolio_theta_pct_per_day > 0.0
    assert lib.portfolio_vega_pct_per_iv_point < 0.0


def test_strategy_greek_contribution_uses_leg_summed_multiplier_units() -> None:
    """AC: ``_accumulate_portfolio_greeks`` scales a strategy's contribution by
    the leg-summed multiplier units (Σ contract_count * contract_multiplier),
    not a single leg's ``contract_count``."""
    strategy_greeks = OptionGreeks(
        delta=0.30,
        gamma=0.01,
        theta=-0.05,
        vega=0.20,
        as_of_timestamp=_NOW,
    )
    # Two legs: 2 contracts and 3 contracts, both at multiplier 100.
    # Leg-summed units = (2 * 100) + (3 * 100) = 500.
    # A first-leg-only scaling would use 2 * 100 = 200.
    pos = _make_strategy_position_view(
        "POS-SPREAD",
        "NVDA",
        leg_specs=[(2.0, 100.0), (3.0, 100.0)],
        strategy_greeks=strategy_greeks,
        market_value_usd=400.0,
        notional_usd=400.0,
        delta_adjusted_usd=300.0,
        position_weight_pct=0.4,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    leg_summed_units = (2.0 * 100.0) + (3.0 * 100.0)
    expected_delta_pct = strategy_greeks.delta * leg_summed_units / lib.portfolio_value_usd * 100.0
    assert lib.options_delta_pct == pytest.approx(expected_delta_pct)

    first_leg_units = 2.0 * 100.0
    first_leg_delta_pct = strategy_greeks.delta * first_leg_units / lib.portfolio_value_usd * 100.0
    # The leg-summed scaling must differ from the discarded first-leg scaling.
    assert lib.options_delta_pct != pytest.approx(first_leg_delta_pct)


# ---------------------------------------------------------------------------
# ALP-606 — strategy-aware direction reads route through position_direction()
# ---------------------------------------------------------------------------


def test_strategy_never_selected_by_single_short_max_filter() -> None:
    """AC: a strategy position is never selected by the shorts filter.

    ``_select_single_short_max`` reads direction via ``position_direction()``,
    which returns ``None`` for a strategy — unequal to ``Direction.SHORT`` — so
    the strategy is excluded regardless of its per-leg directions. Reading
    the raw ``record.direction`` would mis-select it as the largest short.
    """
    strategy = _make_strategy_position_view(
        "POS-STRAT",
        "NVDA",
        leg_specs=[(1.0, 100.0), (1.0, 100.0)],
        strategy_greeks=OptionGreeks(
            delta=-0.40, gamma=0.01, theta=0.05, vega=-0.20, as_of_timestamp=_NOW
        ),
        market_value_usd=9_000.0,
        notional_usd=9_000.0,
        delta_adjusted_usd=-9_000.0,
        position_weight_pct=9.0,  # larger than the genuine short below
    )
    genuine_short = _make_equity_position_view(
        "POS-XOM",
        "XOM",
        Direction.SHORT,
        share_count=50.0,
        market_value_usd=5_500.0,
        notional_usd=5_500.0,
        delta_adjusted_usd=-5_500.0,
        position_weight_pct=5.5,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[strategy, genuine_short])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # The strategy (weight 9.0) must not be picked despite outweighing the
    # genuine equity short — the equity short (5.5) is the only short.
    assert lib.single_short_max_pct == pytest.approx(5.5)
    assert lib.single_short_max_position_id == "POS-XOM"


def test_strategy_excluded_from_borrow_cost() -> None:
    """AC: the short-equity borrow-cost loop reads direction via
    ``position_direction()``; a strategy (``None``) is never treated as a
    short equity, so no borrow cost accrues for it even when a borrow-cost
    resolver is supplied.
    """
    strategy = _make_strategy_position_view(
        "POS-STRAT",
        "NVDA",
        leg_specs=[(1.0, 100.0), (1.0, 100.0)],
        strategy_greeks=OptionGreeks(
            delta=-0.40, gamma=0.01, theta=0.05, vega=-0.20, as_of_timestamp=_NOW
        ),
        market_value_usd=9_000.0,
        notional_usd=9_000.0,
        delta_adjusted_usd=-9_000.0,
        position_weight_pct=9.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[strategy])
    lib = to_library_snapshot(
        snapshot,
        sector_resolver=_sector_resolver,
        borrow_cost_resolver=lambda _ticker: 10.0,
    )

    # A strategy is not a short equity → no borrow cost, no recorded accrual.
    assert lib.daily_borrow_cost_pct == pytest.approx(0.0)
    assert lib.existing_positions["POS-STRAT"].daily_borrow_cost_usd is None


def test_strategy_existing_position_built_with_none_direction() -> None:
    """AC: a strategy's ``ExistingPosition`` projection carries ``direction``
    ``None``.

    ``position_direction()`` returns ``None`` for a strategy and
    ``ExistingPosition.direction`` is optional (ALP-603) — a strategy has no
    position-level direction, its directional sign lives per-leg. The
    guardrail consumers are leg-derived (ALP-588 story 01d) and do not branch
    on this field for a strategy.
    """
    strategy = _make_strategy_position_view(
        "POS-STRAT",
        "NVDA",
        leg_specs=[(1.0, 100.0), (1.0, 100.0)],
        strategy_greeks=OptionGreeks(
            delta=-0.40, gamma=0.01, theta=0.05, vega=-0.20, as_of_timestamp=_NOW
        ),
        market_value_usd=9_000.0,
        notional_usd=9_000.0,
        delta_adjusted_usd=-9_000.0,
        position_weight_pct=9.0,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[strategy])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    assert "POS-STRAT" in lib.existing_positions
    ep = lib.existing_positions["POS-STRAT"]
    assert ep.asset_type == AssetType.STRATEGY
    # A strategy has no position-level direction (ALP-603).
    assert ep.direction is None


def test_recompute_strategy_greeks_feeds_true_signed_delta_to_portfolio_view() -> None:
    """ALP-612 end-to-end: the real ``recompute_strategy_greeks`` output,
    consumed by ``_accumulate_portfolio_greeks`` via ``to_library_snapshot``,
    contributes the strategy's *true* signed delta — not 2x — to the portfolio
    greek view.

    This exercises the production writer (the continuous-monitor greeks
    refresh) and the production reader against the same convention. A synthetic
    ``strategy_greeks`` fixture cannot catch a writer/reader convention drift;
    only the real recompute output can.
    """
    # A net-short-delta bear call spread: short the lower-strike (higher-delta)
    # call, long the higher-strike (lower-delta) call. 1 contract * 100 each.
    expiry = date(2026, 3, 20)

    def _leg_options(strike: float) -> OptionsPositionDetails:
        return OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=strike,
            expiration_date=expiry,
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=5.0,
            greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
        )

    base_strategy = StrategyPositionDetails(
        strategy_type_label="bear_call_spread",
        legs=(
            StrategyLeg(leg_id="short-leg", direction=Direction.SHORT, options=_leg_options(195.0)),
            StrategyLeg(leg_id="long-leg", direction=Direction.LONG, options=_leg_options(205.0)),
        ),
        net_premium_usd=-300.0,
        max_profit_usd=300.0,
        max_loss_usd=-700.0,
        breakeven_levels=(198.0,),
        strategy_greeks=OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0),
    )

    # Production writer: the continuous-monitor greeks refresh.
    per_leg, aggregated = recompute_strategy_greeks(
        strategy=base_strategy,
        leg_ivs={"short-leg": 0.32, "long-leg": 0.30},
        spot=200.0,
        as_of=_NOW,
        risk_free_rate=0.045,
    )
    # The spread is net-short delta: the short lower-strike call dominates.
    assert aggregated.delta < 0.0

    # Production reader: to_library_snapshot -> _accumulate_portfolio_greeks.
    # The view's legs carry the same 1-contract * 100-multiplier units the
    # recompute saw, so the leg-summed unit scale matches.
    pos = _make_strategy_position_view(
        "POS-BEARCALL",
        "NVDA",
        leg_specs=[(1.0, 100.0), (1.0, 100.0)],
        strategy_greeks=aggregated,
        market_value_usd=400.0,
        notional_usd=400.0,
        delta_adjusted_usd=-300.0,
        position_weight_pct=0.4,
    )
    snapshot = _make_pydantic_snapshot(open_positions=[pos])
    lib = to_library_snapshot(snapshot, sector_resolver=_sector_resolver)

    # The portfolio delta contribution must equal the strategy's true signed
    # delta-units — Σ(leg_sign * contract_count * contract_multiplier * delta) —
    # scaled to a percentage of portfolio value.
    leg_units = 1.0 * 100.0
    true_signed_delta_units = (
        -1.0 * leg_units * per_leg["short-leg"].delta + 1.0 * leg_units * per_leg["long-leg"].delta
    )
    expected_delta_pct = true_signed_delta_units / lib.portfolio_value_usd * 100.0
    assert lib.options_delta_pct == pytest.approx(expected_delta_pct)
    assert lib.options_delta_pct < 0.0

    # Regression guard: the pre-ALP-612 gross-sum writer folded contract_count
    # into strategy_greeks, and _accumulate_portfolio_greeks then multiplied by
    # Σ(contract_count * contract_multiplier) again — exactly 2x too large.
    assert lib.options_delta_pct != pytest.approx(2.0 * expected_delta_pct)
