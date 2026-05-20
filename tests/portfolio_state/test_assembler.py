"""Tests for the snapshot assembler (story 06)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
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
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.regt_margin_attribution import RegTExcessAggregates
from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.assembler import assemble_snapshot
from alphamind.portfolio_state.consumers.synthesizer import (
    SnapshotBackedSynthesizerReader,
    adapt_ticker_sector_resolver,
)
from alphamind.portfolio_state.freshness import AssembledSnapshot, SnapshotFreshness
from alphamind.portfolio_state.pricing import (
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
    StubOptionPriceProvider,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
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
    StrategyLeg,
    StrategyPositionDetails,
    occ_symbol_for_options,
)
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.repository import (
    CurrentInvocationMetadata,
    PortfolioPnLInputs,
    PriorInvocationContext,
    RepositoryConsistencyError,
    RepositoryFixture,
    RepositoryReadError,
    StubPortfolioStateRepository,
)
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    FeatureFlagsView,
    LibraryConfig,
    build_risk_budget_consumption,
)
from alphamind.risk_guardrails.library_snapshot import to_library_snapshot

# ---------------------------------------------------------------------------
# Shared timestamps
# ---------------------------------------------------------------------------

_PHASE1_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
_NOW = datetime(2025, 6, 1, 9, 30, 0, tzinfo=UTC)  # 30 min after phase1
_ENTRY_AT = datetime(2025, 6, 1, 8, 0, 0, tzinfo=UTC)  # 1.5 h before _NOW
_ORDER_SUBMITTED_AT = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)  # 30 min before _NOW
_INV_ID = "inv-test-001"

# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------


def _make_config() -> PortfolioStateConfig:
    return PortfolioStateConfig(
        pm_decision_log_sliding_window_invocations=5,
        thesis_resolutions_lookback_trading_days=10,
        thesis_quality_aggregates_trailing_windows_days=(5, 20),
        snapshot_freshness_max_phase1_to_snapshot_seconds=300.0,
        snapshot_freshness_max_price_age_seconds=60.0,
        snapshot_freshness_max_option_price_age_seconds=300.0,
    )


def _make_cash_ledger(current_cash: float = 10_000.0) -> CashLedger:
    return CashLedger(
        current_cash_usd=current_cash,
        settled_cash_usd=current_cash,
        reserved_capital_usd=0.0,
        available_buying_power_usd=current_cash,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_drawdown_state(
    current_drawdown_pct: float = 0.0,
    drawdown_by_source_pct: dict[str, float] | None = None,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=current_drawdown_pct,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=current_drawdown_pct,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct=drawdown_by_source_pct or {},
    )


def _make_pnl_inputs() -> PortfolioPnLInputs:
    return PortfolioPnLInputs(
        daily_realized_pnl_usd=0.0,
        cumulative_realized_pnl_usd=0.0,
        rolling_realized_pnl={"1d": 0.0, "3d": 0.0, "5d": 0.0, "20d": 0.0},
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_active_risk_parameters(
    regime: RegimeLabel = RegimeLabel.NORMAL,
    parameter_change_flag: bool = False,
) -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=regime,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=parameter_change_flag,
        entries=(),
        active_overlays=(),
    )


def _make_active_risk_parameters_with_entry(rule_id: str, value: float) -> ActiveRiskParameterSet:
    entry = ActiveRiskParameterEntry(
        rule_id=rule_id,
        rule_label=rule_id,
        value=value,
        unit="USD",
        regime_multiplier_applied=1.0,
        base_value=value,
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(entry,),
        active_overlays=(),
    )


def _make_thesis_quality_aggregates() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
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


def _make_invocation_metadata(invocation_id: str = _INV_ID) -> CurrentInvocationMetadata:
    return CurrentInvocationMetadata(
        invocation_id=invocation_id,
        phase1_committed_at=_PHASE1_AT,
        pipeline_invocation_started_at=None,
    )


def _make_prior_context(
    prior_params: ActiveRiskParameterSet | None = None,
) -> PriorInvocationContext:
    if prior_params is None:
        return PriorInvocationContext(
            prior_invocation_id=None,
            prior_active_risk_parameters=None,
            prior_phase1_committed_at=None,
        )
    return PriorInvocationContext(
        prior_invocation_id="inv-000",
        prior_active_risk_parameters=prior_params,
        prior_phase1_committed_at=_PHASE1_AT,
    )


def _make_equity_position(
    position_id: str = "POS-001",
    ticker: str = "NVDA",
    share_count: float = 100.0,
    cost_per_share: float = 500.0,
    direction: Direction = Direction.LONG,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=cost_per_share,
        borrow_rate_pct=0.5 if direction == Direction.SHORT else None,
        locate_status=LocateStatus.LOCATED if direction == Direction.SHORT else None,
        margin_held_usd=1000.0 if direction == Direction.SHORT else None,
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_AT,
        fill_price=price(cost_per_share),
        fill_quantity=share_count,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )
    execution_history = () if status == PositionStatus.PENDING else (fill,)
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=direction,
        entry_timestamp=_ENTRY_AT,
        details=equity,
        execution_history=execution_history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
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


def _make_pending_order(
    order_id: str = "ORD-001",
    position_id: str = "POS-001",
    submission_timestamp: datetime = _ORDER_SUBMITTED_AT,
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol("NVDA"))
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
        submission_timestamp=submission_timestamp,
        last_update_timestamp=submission_timestamp,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.0,
    )


def _make_bracket(
    bracket_id: str = "BRK-001",
    position_id: str = "POS-001",
    stop_price: float = 480.0,
    target_price: float = 550.0,
) -> BracketRecord:
    stop_leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-stop-1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=stop_price, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    target_leg = BracketLeg(
        leg_id="leg-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId("ord-target-1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=target_price, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(stop_leg, target_leg),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_strategy_position(
    position_id: str = "STR-001",
    leg1_underlying: str = "NVDA",
    leg2_underlying: str = "NVDA",
    premium1: float = 10.0,
    premium2: float = 5.0,
    net_premium_usd: float | None = None,
    max_loss_usd: float | None = None,
    leg1_direction: Direction | None = None,
    leg2_direction: Direction | None = None,
) -> PositionRecord:
    leg1_options = OptionsPositionDetails(
        underlying_ticker=Symbol(leg1_underlying),
        strike_price=500.0,
        expiration_date=date(2025, 12, 31),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=premium1,
        greeks=OptionGreeks(delta=0.5, gamma=0.01, theta=-0.5, vega=0.2),
    )
    leg2_options = OptionsPositionDetails(
        underlying_ticker=Symbol(leg2_underlying),
        strike_price=510.0,
        expiration_date=date(2025, 12, 31),
        contract_type=OptionContractType.PUT,
        contract_count=1.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=premium2,
        greeks=OptionGreeks(delta=-0.5, gamma=0.01, theta=-0.5, vega=0.2),
    )
    gross_premium = (premium1 + premium2) * LISTED_OPTION_CONTRACT_MULTIPLIER
    strategy_details = StrategyPositionDetails(
        strategy_type_label="LONG_STRADDLE",
        legs=(
            StrategyLeg(leg_id="leg-1", options=leg1_options, direction=leg1_direction),
            StrategyLeg(leg_id="leg-2", options=leg2_options, direction=leg2_direction),
        ),
        net_premium_usd=net_premium_usd if net_premium_usd is not None else gross_premium,
        max_profit_usd=float("inf"),
        max_loss_usd=max_loss_usd if max_loss_usd is not None else gross_premium,
        breakeven_levels=(490.0, 520.0),
        strategy_greeks=OptionGreeks(delta=0.0, gamma=0.02, theta=-1.0, vega=0.4),
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_AT,
        fill_price=price((premium1 + premium2) * LISTED_OPTION_CONTRACT_MULTIPLIER),
        fill_quantity=1.0,
        slippage=signed_money(0.0),
        fees=money(2.0),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_ENTRY_AT,
        details=strategy_details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_fixture(
    open_positions: tuple[PositionRecord, ...] = (),
    pending_positions: tuple[PositionRecord, ...] = (),
    brackets: tuple[BracketRecord, ...] = (),
    pending_orders: tuple[OrderRecord, ...] = (),
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]] | None = None,
    drawdown_state: DrawdownState | None = None,
    current_invocation_metadata: CurrentInvocationMetadata | None = None,
    prior_invocation_context: PriorInvocationContext | None = None,
    active_risk_parameters: ActiveRiskParameterSet | None = None,
    cash_ledger: CashLedger | None = None,
    regt_excess_aggregates: RegTExcessAggregates | None = None,
) -> RepositoryFixture:
    return RepositoryFixture(
        open_positions=open_positions,
        pending_positions=pending_positions,
        drawdown_state=drawdown_state or _make_drawdown_state(),
        portfolio_pnl_inputs=_make_pnl_inputs(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=cash_ledger or _make_cash_ledger(),
        pending_orders=pending_orders,
        active_risk_parameters=active_risk_parameters or _make_active_risk_parameters(),
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=recent_pm_decision_log,
        position_modification_trail=position_modification_trail or {},
        thesis_quality_aggregates=_make_thesis_quality_aggregates(),
        brackets=brackets,
        current_invocation_metadata=current_invocation_metadata or _make_invocation_metadata(),
        prior_invocation_context=prior_invocation_context or _make_prior_context(),
        regt_excess_aggregates=regt_excess_aggregates
        or RegTExcessAggregates(
            trailing_30d_usd=0.0,
            trailing_90d_usd=0.0,
            lifetime_usd=0.0,
        ),
    )


def _null_sector_resolver(pos: PositionRecord) -> str | None:
    return None


def _ticker_sector_resolver(
    mapping: dict[str, str],
) -> Callable[[PositionRecord], str | None]:
    def _resolve(pos: PositionRecord) -> str | None:
        if isinstance(pos.details, EquityPositionDetails):
            return mapping.get(pos.details.ticker)
        if isinstance(pos.details, OptionsPositionDetails):
            return mapping.get(pos.details.underlying_ticker)
        return None

    return _resolve


def _run[T](value: T) -> T:
    """Identity passthrough kept for call-site stability across the ALP-468 sync strip.

    Before the strip, callers wrapped ``_run(assemble_snapshot(...))`` to drive
    the assembler's coroutine via ``asyncio.run``. The assembler is sync now;
    the helper now just returns the value unchanged so the call sites remain
    grep-stable. Inline at next refactor pass.
    """
    return value


# ---------------------------------------------------------------------------
# Failing stub repository — raises on any call
# ---------------------------------------------------------------------------


class _FailingRepository:
    """Raises RepositoryReadError on every method call."""

    def get_open_positions(self) -> tuple[PositionRecord, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_pending_positions(self) -> tuple[PositionRecord, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_drawdown_state(self) -> DrawdownState:
        raise RepositoryReadError("simulated read failure")

    def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs:
        raise RepositoryReadError("simulated read failure")

    def get_active_theses(self) -> tuple[ThesisRecord, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_cash_ledger(self) -> CashLedger:
        raise RepositoryReadError("simulated read failure")

    def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates:
        del now
        raise RepositoryReadError("simulated read failure")

    def get_pending_orders(self) -> tuple[OrderRecord, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        raise RepositoryReadError("simulated read failure")

    def get_intra_invocation_changelog(self, *, invocation_id: str) -> tuple[ActivityLogEntry, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]:
        raise RepositoryReadError("simulated read failure")

    def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate:
        raise RepositoryReadError("simulated read failure")

    def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        raise RepositoryReadError("simulated read failure")

    def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        raise RepositoryReadError("simulated read failure")

    def get_prior_invocation_context(self) -> PriorInvocationContext:
        raise RepositoryReadError("simulated read failure")


class _ConsistencyErrorRepository(_FailingRepository):
    def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        raise RepositoryConsistencyError("isolation violation")

    def get_prior_invocation_context(self) -> PriorInvocationContext:
        raise RepositoryConsistencyError("isolation violation")


# ---------------------------------------------------------------------------
# Test 1: Happy path — empty portfolio
# ---------------------------------------------------------------------------


def test_empty_portfolio_returns_valid_snapshot() -> None:
    """Empty portfolio produces a valid snapshot with all-empty tuples and zero rollups."""
    fixture = _make_fixture()
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)
    config = _make_config()

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=config,
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert snapshot.open_positions == ()
    assert snapshot.pending_positions == ()
    assert snapshot.brackets == ()
    assert snapshot.pending_orders == ()
    assert snapshot.portfolio_pnl.total_unrealized_pnl_usd == 0.0
    assert snapshot.portfolio_pnl.total_unrealized_pnl_pct_of_portfolio == 0.0
    assert snapshot.invocation_id == _INV_ID
    assert snapshot.snapshot_assembled_at == _NOW


# ---------------------------------------------------------------------------
# Test 2: assemble_snapshot signature — sync, keyword-only
# ---------------------------------------------------------------------------


def test_assemble_snapshot_is_sync() -> None:
    """Per ALP-468: ``assemble_snapshot`` is synchronous after the strip.

    Calling it without ``await`` returns an ``AssembledSnapshot`` directly,
    not a coroutine. Guards against accidental re-introduction of ``async``.
    """
    import inspect

    assert not inspect.iscoroutinefunction(assemble_snapshot)


def test_assemble_snapshot_call_returns_assembled_snapshot_not_coroutine() -> None:
    """Smoke check: a stub-fixture invocation yields a value, not a coroutine."""
    from alphamind.portfolio_state.freshness import AssembledSnapshot
    from alphamind.portfolio_state.repository import StubPortfolioStateRepository

    repo = StubPortfolioStateRepository(_make_fixture())
    provider = StubCurrentPriceProvider({}, _NOW)
    result = assemble_snapshot(
        repository=repo,
        price_provider=provider,
        option_price_provider=StubOptionPriceProvider({}, _NOW),
        sector_resolver=_null_sector_resolver,
        config=_make_config(),
        now=_NOW,
    )
    # Direct equality against AssembledSnapshot (sync return) — would fail if
    # ``assemble_snapshot`` were async and returned a coroutine.
    assert isinstance(result, AssembledSnapshot)


# ---------------------------------------------------------------------------
# Test 3: Single open equity position — enrichment correctness
# ---------------------------------------------------------------------------


def test_single_equity_position_enrichment() -> None:
    """Single LONG NVDA position; verify market value, weight, unrealized P/L."""
    # 100 shares, cost $500, price $520 → MV = $52,000, unrealized = $2,000
    pos = _make_equity_position(share_count=100.0, cost_per_share=500.0)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),  # no cash, only position
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert len(snapshot.open_positions) == 1
    pos_out = snapshot.open_positions[0]
    assert pos_out.current_market_value_usd == pytest.approx(52_000.0)
    assert pos_out.unrealized_pnl_usd == pytest.approx(2_000.0)  # 52000 - 50000
    # weight = abs(52000) / 52000 * 100 = 100%
    assert pos_out.position_weight_pct == pytest.approx(100.0)

    # Sector exposure: one entry (UNCLASSIFIED since null resolver)
    assert len(snapshot.sector_exposure) == 1
    sector = snapshot.sector_exposure[0]
    assert sector.sector == "UNCLASSIFIED"
    assert sector.long_delta_adjusted_usd == pytest.approx(52_000.0)

    # Directional exposure: net long
    assert snapshot.directional_exposure.total_long_delta_adjusted_usd == pytest.approx(52_000.0)
    assert snapshot.directional_exposure.total_short_delta_adjusted_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ALP-579: Pending positions contribute to sector / directional exposure
# ---------------------------------------------------------------------------


_DEFAULT_ZONES = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)


def test_pending_positions_flow_through_all_three_exposure_consumers() -> None:
    """Snapshot reader, library snapshot, and risk-budget projection all
    reflect PENDING positions consistently — guards the three tools that
    diverged in invocation ``inv-20260519T030654Z-60f10023``:

    * ``SnapshotBackedSynthesizerReader.get_exposure_snapshot`` (returned
      "No exposure" pre-fix while ``get_positions_summary`` saw 8 positions).
    * ``to_library_snapshot`` → ``LibrarySnapshot.sector_exposure_pct`` /
      ``net_long_pct`` / ``gross_pct`` (the shape ``validate_guardrail``
      reads and the headroom calculator projects rules against).
    * ``build_risk_budget_consumption`` against that library snapshot (the
      source of the headroom blocks in the analyst / strategist / PM bundles).
    """
    sector_by_ticker = {"NVDA": "tech", "AMD": "tech", "JPM": "financials"}

    def ticker_sector(ticker: str) -> str:
        return sector_by_ticker.get(ticker, "unclassified")

    pending_positions = (
        _make_equity_position(
            "PND-NVDA", "NVDA", 10.0, 500.0, Direction.LONG, PositionStatus.PENDING
        ),
        _make_equity_position(
            "PND-JPM", "JPM", 20.0, 150.0, Direction.LONG, PositionStatus.PENDING
        ),
    )
    fixture = _make_fixture(
        pending_positions=pending_positions,
        cash_ledger=_make_cash_ledger(current_cash=20_000.0),
        active_risk_parameters=_make_active_risk_parameters_with_entry(
            "position_max_size_pct", 10.0
        ),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {
            "NVDA": _make_fresh_quote("NVDA", 520.0),
            "JPM": _make_fresh_quote("JPM", 160.0),
        },
        _NOW,
    )

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=adapt_ticker_sector_resolver(ticker_sector),
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    # Tool 1: SnapshotBackedSynthesizerReader.
    reader = SnapshotBackedSynthesizerReader(snapshot, adapt_ticker_sector_resolver(ticker_sector))
    positions = reader.get_positions_summary()
    exposure = reader.get_exposure_snapshot()
    assert {p.ticker for p in positions} == {"NVDA", "JPM"}
    assert exposure.sector_exposure_pct != {}
    assert exposure.gross_exposure_pct > 0.0
    assert exposure.net_directional_pct > 0.0

    # Tool 2: library snapshot.
    library_snapshot = to_library_snapshot(snapshot, sector_resolver=ticker_sector)
    assert library_snapshot.sector_exposure_pct["tech"] > 0.0
    assert library_snapshot.sector_exposure_pct["financials"] > 0.0
    assert library_snapshot.net_long_pct > 0.0
    assert library_snapshot.gross_pct > 0.0

    # Tool 3: build_risk_budget_consumption.
    limits = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "gross_exposure_pct": 120.0,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    library_config = LibraryConfig(
        effective_limits=MappingProxyType(limits),
        escalation_zones=MappingProxyType({k: _DEFAULT_ZONES for k in limits}),
        feature_flags=FeatureFlagsView(options_enabled=False, short_selling_enabled=False),
        active_sectors=("tech", "financials"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )
    risk_budget = build_risk_budget_consumption(library_snapshot, library_config)
    entries_by_id = {e.rule_id: e for e in risk_budget.entries}
    assert entries_by_id["sector_concentration_tech"].current_value > 0.0
    assert entries_by_id["sector_concentration_financials"].current_value > 0.0
    assert entries_by_id["net_long_pct"].current_value > 0.0
    assert entries_by_id["gross_exposure_pct"].current_value > 0.0


# ---------------------------------------------------------------------------
# Test 4: Multi-position — sector rollup, directional exposure, trail keying
# ---------------------------------------------------------------------------


def test_multi_position_rollup() -> None:
    """Three positions: two sectors, one short, one long-with-bracket, one plain long."""
    pos_tech_long = _make_equity_position("POS-TECH", "NVDA", 10.0, 500.0, Direction.LONG)
    pos_tech_short = _make_equity_position("POS-TECH-S", "AMD", 5.0, 100.0, Direction.SHORT)
    pos_health = _make_equity_position("POS-HLTH", "JNJ", 20.0, 150.0, Direction.LONG)

    modification_trail_entry = ActivityLogEntry(
        entry_id="trail-1",
        invocation_id=_INV_ID,
        timestamp=_NOW,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=PositionId("POS-TECH"),
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=PMDecisionDetail(
            envelope_id="e1",
            source_provenance_json={},
            evaluation_json={},
            modifications_json=[],
            resulting_command_ids=(),
            verdict=PMVerdict.APPROVE,
        ),
    )

    fixture = _make_fixture(
        open_positions=(pos_tech_long, pos_tech_short, pos_health),
        position_modification_trail={"POS-TECH": (modification_trail_entry,)},
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {
            "NVDA": _make_fresh_quote("NVDA", 520.0),
            "AMD": _make_fresh_quote("AMD", 110.0),
            "JNJ": _make_fresh_quote("JNJ", 160.0),
        },
        _NOW,
    )

    def sector_resolver(pos: PositionRecord) -> str | None:
        mapping = {"NVDA": "TECH", "AMD": "TECH", "JNJ": "HEALTHCARE"}
        if isinstance(pos.details, EquityPositionDetails):
            return mapping.get(pos.details.ticker)
        return None

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert len(snapshot.open_positions) == 3

    # Verify sectors
    sectors_by_name = {s.sector: s for s in snapshot.sector_exposure}
    assert "TECH" in sectors_by_name
    assert "HEALTHCARE" in sectors_by_name

    # TECH: long 5200 (NVDA 10 * 520), short 550 (AMD 5 * 110)
    tech = sectors_by_name["TECH"]
    assert tech.long_delta_adjusted_usd == pytest.approx(5_200.0)
    assert tech.short_delta_adjusted_usd == pytest.approx(550.0)

    # position_modification_trail keyed by position_id
    assert "POS-TECH" in snapshot.position_modification_trail
    assert len(snapshot.position_modification_trail["POS-TECH"]) == 1

    # positions sorted by position_id
    ids = [p.position_id for p in snapshot.open_positions]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Test 5: Two-pass weight enrichment correctness
# ---------------------------------------------------------------------------


def test_two_pass_weight_enrichment() -> None:
    """Verify position_weight_pct == abs(mv) / total_portfolio_value * 100."""
    pos_a = _make_equity_position("POS-A", "NVDA", 10.0, 500.0)
    pos_b = _make_equity_position("POS-B", "AAPL", 20.0, 150.0)
    cash = 0.0
    # After price:
    # NVDA: 10 * 520 = 5200 MV
    # AAPL: 20 * 160 = 3200 MV
    # total = 5200 + 3200 = 8400
    # weight_a = 5200/8400*100 = 61.9047...
    # weight_b = 3200/8400*100 = 38.0952...

    fixture = _make_fixture(
        open_positions=(pos_a, pos_b),
        cash_ledger=_make_cash_ledger(current_cash=cash),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {
            "NVDA": _make_fresh_quote("NVDA", 520.0),
            "AAPL": _make_fresh_quote("AAPL", 160.0),
        },
        _NOW,
    )

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    by_id = {p.position_id: p for p in snapshot.open_positions}
    total = 5_200.0 + 3_200.0
    assert by_id["POS-A"].position_weight_pct == pytest.approx(5_200.0 / total * 100)
    assert by_id["POS-B"].position_weight_pct == pytest.approx(3_200.0 / total * 100)
    assert pytest.approx(
        by_id["POS-A"].position_weight_pct + by_id["POS-B"].position_weight_pct
    ) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Test 6: Stale price from provider — assembly continues, MV = 0.0
# ---------------------------------------------------------------------------


def test_stale_price_does_not_abort_assembly(caplog: pytest.LogCaptureFixture) -> None:
    """Provider returns is_stale=True; position has MV=0.0 but snapshot constructs."""
    pos = _make_equity_position()
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=1000.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    stale_quote = _make_stale_quote("NVDA")
    provider = StubCurrentPriceProvider({"NVDA": stale_quote}, _NOW)

    with caplog.at_level(logging.WARNING, logger="alphamind.portfolio_state.assembler"):
        assembled = _run(
            assemble_snapshot(
                repository=repo,
                price_provider=provider,
                option_price_provider=StubOptionPriceProvider({}, _NOW),
                sector_resolver=_null_sector_resolver,
                config=_make_config(),
                now=_NOW,
            )
        )
    snapshot = assembled.snapshot

    assert len(snapshot.open_positions) == 1
    assert snapshot.open_positions[0].current_market_value_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 7: Unknown ticker missing from get_quotes result — treated as stale
# ---------------------------------------------------------------------------


def test_missing_ticker_from_get_quotes_treated_as_stale(caplog: pytest.LogCaptureFixture) -> None:
    """Ticker absent from get_quotes result → warning logged, MV=0.0, snapshot constructs."""
    pos = _make_equity_position(ticker=Symbol("UNKNOWN"))
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=1000.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)  # no quotes at all

    with caplog.at_level(logging.WARNING, logger="alphamind.portfolio_state.assembler"):
        assembled = _run(
            assemble_snapshot(
                repository=repo,
                price_provider=provider,
                option_price_provider=StubOptionPriceProvider({}, _NOW),
                sector_resolver=_null_sector_resolver,
                config=_make_config(),
                now=_NOW,
            )
        )
    snapshot = assembled.snapshot

    assert snapshot.open_positions[0].current_market_value_usd == pytest.approx(0.0)
    # Warning should have been emitted
    assert any(
        "stale" in r.message.lower() or "missing" in r.message.lower() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 8: RepositoryReadError propagates
# ---------------------------------------------------------------------------


def test_repository_read_error_propagates() -> None:
    """RepositoryReadError from any repository call propagates from assemble_snapshot."""
    repo = _FailingRepository()
    provider = StubCurrentPriceProvider({}, _NOW)

    with pytest.raises(RepositoryReadError):
        _run(
            assemble_snapshot(
                repository=repo,
                price_provider=provider,
                option_price_provider=StubOptionPriceProvider({}, _NOW),
                sector_resolver=_null_sector_resolver,
                config=_make_config(),
                now=_NOW,
            )
        )


# ---------------------------------------------------------------------------
# Test 9: RepositoryConsistencyError propagates
# ---------------------------------------------------------------------------


def test_repository_consistency_error_propagates() -> None:
    """RepositoryConsistencyError propagates from assemble_snapshot."""
    repo = _ConsistencyErrorRepository()
    provider = StubCurrentPriceProvider({}, _NOW)

    with pytest.raises(RepositoryConsistencyError):
        _run(
            assemble_snapshot(
                repository=repo,
                price_provider=provider,
                option_price_provider=StubOptionPriceProvider({}, _NOW),
                sector_resolver=_null_sector_resolver,
                config=_make_config(),
                now=_NOW,
            )
        )


# ---------------------------------------------------------------------------
# Test 10: parameter_change_flag — no prior context → False
# ---------------------------------------------------------------------------


def test_parameter_change_flag_false_when_no_prior_context() -> None:
    """When prior context is None, parameter_change_flag must be False."""
    fixture = _make_fixture(prior_invocation_context=_make_prior_context(prior_params=None))
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert snapshot.active_risk_parameters.parameter_change_flag is False


# ---------------------------------------------------------------------------
# Test 11: parameter_change_flag — prior params differ → True
# ---------------------------------------------------------------------------


def test_parameter_change_flag_true_when_prior_params_differ() -> None:
    """When prior risk parameters differ, parameter_change_flag must be True."""
    current = _make_active_risk_parameters_with_entry("rule-1", 1000.0)
    prior = _make_active_risk_parameters_with_entry("rule-1", 2000.0)  # different value

    fixture = _make_fixture(
        active_risk_parameters=current,
        prior_invocation_context=_make_prior_context(prior_params=prior),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert snapshot.active_risk_parameters.parameter_change_flag is True


# ---------------------------------------------------------------------------
# Test 12: drawdown_by_source_pct enriched when empty and drawdown > 0
# ---------------------------------------------------------------------------


def test_drawdown_by_source_enriched_when_empty() -> None:
    """Repository returns empty drawdown_by_source_pct with drawdown > 0; assembler enriches."""
    # Two positions: one with unrealized loss (will contribute to drawdown source)
    # bought at 600, now 520 → unrealized loss drives drawdown source
    pos_loss = _make_equity_position("POS-LOSS", "NVDA", 100.0, 600.0)
    pos_gain = _make_equity_position("POS-GAIN", "AAPL", 10.0, 100.0)

    fixture = _make_fixture(
        open_positions=(pos_loss, pos_gain),
        drawdown_state=_make_drawdown_state(
            current_drawdown_pct=5.0,
            drawdown_by_source_pct={},  # empty — assembler should fill
        ),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {
            "NVDA": _make_fresh_quote("NVDA", 520.0),
            "AAPL": _make_fresh_quote("AAPL", 150.0),
        },
        _NOW,
    )

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    # NVDA: 100 shares * (520 - 600) = -8000 (loss) → should be in drawdown source
    # AAPL: 10 shares * (150 - 100) = +500 (gain) → not in drawdown source
    assert "POS-LOSS" in snapshot.drawdown.drawdown_by_source_pct
    assert "POS-GAIN" not in snapshot.drawdown.drawdown_by_source_pct
    assert snapshot.drawdown.drawdown_by_source_pct["POS-LOSS"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Test 13: drawdown_by_source_pct NOT overwritten when pre-populated
# ---------------------------------------------------------------------------


def test_drawdown_by_source_not_overwritten_when_prepopulated() -> None:
    """Repository provides pre-populated drawdown_by_source_pct; assembler leaves it intact."""
    pre_populated = {"POS-1": 3.0, "POS-2": 2.0}
    fixture = _make_fixture(
        drawdown_state=_make_drawdown_state(
            current_drawdown_pct=5.0,
            drawdown_by_source_pct=pre_populated,
        )
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert snapshot.drawdown.drawdown_by_source_pct == pre_populated


# ---------------------------------------------------------------------------
# Test 14: Pending order age enrichment
# ---------------------------------------------------------------------------


def test_pending_order_age_enriched() -> None:
    """Assembler enriches pending orders' age_hours from now - submission_timestamp."""
    # Order submitted 30 min before _NOW → expected age = 0.5 hours
    order = _make_pending_order(submission_timestamp=_ORDER_SUBMITTED_AT)

    # Need an open position for the bracket/order to reference
    pos = _make_equity_position()
    fixture = _make_fixture(
        open_positions=(pos,),
        pending_orders=(order,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    assert len(snapshot.pending_orders) == 1
    # _ORDER_SUBMITTED_AT = _NOW - 30 min = 0.5 hours
    assert snapshot.pending_orders[0].age_hours == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test 15: Strategy position with missing leg price raises
# ---------------------------------------------------------------------------


def test_strategy_position_missing_leg_price_handled_in_band() -> None:
    """Strategy with a missing underlying price is handled in-band: MV=0.0, assembly continues.

    Story 06 § Error handling permits "(or in-band-handles)" for strategy staleness.
    """
    pos = _make_strategy_position(leg1_underlying="NVDA", leg2_underlying="AMD")
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    # Provide NVDA but omit AMD → leg-2 underlying is missing from the price map
    provider = StubCurrentPriceProvider(
        {"NVDA": _make_fresh_quote("NVDA", 520.0)},
        _NOW,
    )

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    assert assembled.snapshot.open_positions[0].current_market_value_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 16: Determinism
# ---------------------------------------------------------------------------


def test_determinism() -> None:
    """Identical inputs produce identical snapshots across repeated calls."""
    pos = _make_equity_position()
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=1000.0),
    )

    def make_assembled() -> object:
        repo = StubPortfolioStateRepository(fixture)
        provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)
        return _run(
            assemble_snapshot(
                repository=repo,
                price_provider=provider,
                option_price_provider=StubOptionPriceProvider({}, _NOW),
                sector_resolver=_null_sector_resolver,
                config=_make_config(),
                now=_NOW,
            )
        )

    snap1 = make_assembled()
    snap2 = make_assembled()
    assert snap1 == snap2


# ---------------------------------------------------------------------------
# Test 17: Position age correctness
# ---------------------------------------------------------------------------


def test_position_age_computed_from_entry_timestamp() -> None:
    """position_age_hours = (now - entry_timestamp) / 3600."""
    pos = _make_equity_position()  # entry_timestamp = _ENTRY_AT = 1.5h before _NOW
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    # _NOW - _ENTRY_AT = 1.5 hours
    expected_age = (_NOW - _ENTRY_AT).total_seconds() / 3600.0
    assert snapshot.open_positions[0].position_age_hours == pytest.approx(expected_age)


# ---------------------------------------------------------------------------
# Test 18: Sector resolver with named sectors
# ---------------------------------------------------------------------------


def test_sector_resolver_used_for_sector_exposure() -> None:
    """Sector resolver is invoked; positions classified into named sectors."""
    pos_nvda = _make_equity_position("POS-NVDA", "NVDA", 10.0, 500.0)
    pos_jnj = _make_equity_position("POS-JNJ", "JNJ", 10.0, 150.0)
    fixture = _make_fixture(
        open_positions=(pos_nvda, pos_jnj),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {
            "NVDA": _make_fresh_quote("NVDA", 520.0),
            "JNJ": _make_fresh_quote("JNJ", 160.0),
        },
        _NOW,
    )
    resolver = _ticker_sector_resolver({"NVDA": "TECH", "JNJ": "HEALTHCARE"})

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    snapshot = assembled.snapshot

    sector_names = {s.sector for s in snapshot.sector_exposure}
    assert "TECH" in sector_names
    assert "HEALTHCARE" in sector_names
    assert "UNCLASSIFIED" not in sector_names


# ---------------------------------------------------------------------------
# Test 19: assemble_snapshot returns AssembledSnapshot bundle (story 08)
# ---------------------------------------------------------------------------


def test_assemble_snapshot_returns_assembled_snapshot_bundle() -> None:
    """assemble_snapshot returns an AssembledSnapshot bundling snapshot and freshness."""
    pos = _make_equity_position("POS-NVDA", "NVDA", 10.0, 500.0)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )

    # Verify bundle shape
    assert isinstance(assembled, AssembledSnapshot)
    assert isinstance(assembled.snapshot, PortfolioStateSnapshot)
    assert isinstance(assembled.freshness, SnapshotFreshness)

    # Freshness sidecar corresponds to the snapshot
    assert assembled.freshness.snapshot_assembled_at == assembled.snapshot.snapshot_assembled_at
    assert assembled.freshness.phase1_committed_at == assembled.snapshot.phase1_committed_at
    assert assembled.freshness.total_open_positions == len(assembled.snapshot.open_positions)
    assert assembled.freshness.total_positions == (
        len(assembled.snapshot.open_positions) + len(assembled.snapshot.pending_positions)
    )

    # Position is priced fresh (NVDA quote provided)
    assert assembled.freshness.all_position_prices_fresh is True
    assert "POS-NVDA" in assembled.freshness.position_ids_priced_fresh


def test_assemble_snapshot_exposes_materialized_price_map() -> None:
    """``assemble_snapshot`` surfaces the assembler-internal price_map on
    the AssembledSnapshot bundle so downstream callers can reuse the
    quotes it already fetched (ALP-407).

    Verifies the returned ``price_map`` mirrors the ticker→PriceQuote
    mapping the provider returned, keyed by the same tickers the
    assembler enumerated when fetching prices.
    """
    pos_a = _make_equity_position("POS-A", "NVDA", 10.0, 500.0)
    pos_b = _make_equity_position("POS-B", "AAPL", 20.0, 150.0)
    fixture = _make_fixture(
        open_positions=(pos_a, pos_b),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    nvda_quote = _make_fresh_quote("NVDA", 520.0)
    aapl_quote = _make_fresh_quote("AAPL", 155.0)
    provider = StubCurrentPriceProvider({"NVDA": nvda_quote, "AAPL": aapl_quote}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )

    assert set(assembled.price_map.keys()) == {"NVDA", "AAPL"}
    assert assembled.price_map["NVDA"].price_usd == pytest.approx(520.0)
    assert assembled.price_map["AAPL"].price_usd == pytest.approx(155.0)
    # The values are the materialized PriceQuote objects from the provider.
    assert isinstance(assembled.price_map["NVDA"], PriceQuote)


def test_assemble_snapshot_price_map_omits_unknown_ticker() -> None:
    """When a position references a ticker the provider does not know, the
    snapshot still assembles successfully (the position is classified as
    ``unknown_ticker`` by the assembler's pricing path) and ``price_map``
    simply omits the unknown ticker.

    Locks down the ``StubCurrentPriceProvider.get_quotes`` filter contract:
    unknown tickers are silently dropped from the result rather than
    raising.
    """
    pos_known = _make_equity_position("POS-NVDA", "NVDA", 10.0, 500.0)
    pos_unknown = _make_equity_position("POS-WTF", "WTF", 5.0, 100.0)
    fixture = _make_fixture(
        open_positions=(pos_known, pos_unknown),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    # Provider only knows NVDA — WTF is intentionally absent.
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )

    # The snapshot assembled successfully and the unknown-ticker position
    # was classified by the assembler's pricing path.
    assert isinstance(assembled, AssembledSnapshot)
    assert "POS-WTF" in assembled.freshness.position_ids_unknown_ticker
    # The materialized price_map only carries the ticker the provider knew.
    assert set(assembled.price_map.keys()) == {"NVDA"}
    assert "WTF" not in assembled.price_map


def test_assemble_snapshot_price_map_empty_for_empty_portfolio() -> None:
    """With zero positions, the assembler enumerates no pricing tickers and
    ``price_map`` is an empty dict. Locks down the empty-map contract."""
    fixture = _make_fixture(
        open_positions=(),
        pending_positions=(),
        cash_ledger=_make_cash_ledger(current_cash=10_000.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )

    assert assembled.price_map == {}


def test_assembler_recomputes_available_buying_power_from_canonical_formula() -> None:
    """``available_buying_power_usd`` is a derived field — the assembler
    overwrites whatever the cash_ledger row carries with the canonical
    formula ``settled_cash - reserved_capital - margin_held``.

    Phase 1/2 stop maintaining this field; the persisted value is whatever
    the seed left there. The assembler is the single source of truth at
    read time.
    """
    # Seed cash with a stale/wrong available_buying_power so the test fails
    # if the assembler simply passes the field through unchanged.
    cash = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=90_000.0,
        reserved_capital_usd=5_000.0,
        available_buying_power_usd=999_999.0,
        margin_held_usd=3_000.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    fixture = _make_fixture(cash_ledger=cash)
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    enriched = assembled.snapshot.cash_ledger
    # 90_000 settled - 5_000 reserved - 3_000 margin = 82_000.
    assert enriched.available_buying_power_usd == pytest.approx(82_000.0)
    # The canonical formula matches true_deployable_capital_usd by construction.
    assert enriched.available_buying_power_usd == enriched.true_deployable_capital_usd


def test_step_11_populates_regt_excess_trailing_fields_from_aggregates() -> None:
    """Step 11 must overwrite the ``regt_excess_*`` placeholders on the
    persisted ``CashLedger`` row with the values returned by the repository's
    ``get_regt_excess_aggregates(now)`` call."""
    aggregates = RegTExcessAggregates(
        trailing_30d_usd=12.5,
        trailing_90d_usd=33.75,
        lifetime_usd=125.0,
    )
    fixture = _make_fixture(regt_excess_aggregates=aggregates)
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    enriched = assembled.snapshot.cash_ledger
    assert enriched.regt_excess_trailing_30d_usd == pytest.approx(12.5)
    assert enriched.regt_excess_trailing_90d_usd == pytest.approx(33.75)
    assert enriched.regt_excess_lifetime_usd == pytest.approx(125.0)


def test_step_11_zero_aggregates_overwrite_persisted_placeholders() -> None:
    """Step 11 must surface aggregator zeros — not leak persisted placeholders.

    Seeds the ``cash_ledger`` row with deliberately non-zero placeholders so
    that a no-op refactor (Step 11 forgetting to populate the three regt
    fields from the aggregator) would surface those placeholders unchanged
    and the test would fail.
    """
    cash = CashLedger(
        current_cash_usd=10_000.0,
        settled_cash_usd=10_000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=10_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=999.0,
        regt_excess_trailing_90d_usd=999.0,
        regt_excess_lifetime_usd=999.0,
    )
    fixture = _make_fixture(cash_ledger=cash)  # default fixture supplies zero aggregates
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({}, _NOW)

    assembled = _run(
        assemble_snapshot(
            repository=repo,
            price_provider=provider,
            option_price_provider=StubOptionPriceProvider({}, _NOW),
            sector_resolver=_null_sector_resolver,
            config=_make_config(),
            now=_NOW,
        )
    )
    enriched = assembled.snapshot.cash_ledger
    assert enriched.regt_excess_trailing_30d_usd == 0.0
    assert enriched.regt_excess_trailing_90d_usd == 0.0
    assert enriched.regt_excess_lifetime_usd == 0.0


# ---------------------------------------------------------------------------
# Option pricing via OptionPriceProvider (ALP-516)
#
# The three required fixtures from the issue's acceptance criteria:
#   1. Live snapshot present   → MV uses the live option quote
#   2. Stale snapshot          → MV falls back to entry premium
#   3. No snapshot             → MV falls back to entry premium
# ---------------------------------------------------------------------------


_OPT_UNDERLYING = "NVDA"
_OPT_STRIKE = 500.0
_OPT_EXPIRY = date(2026, 1, 16)
_OPT_CONTRACT_TYPE = OptionContractType.CALL


def _make_open_options_position(
    *,
    premium: float = 5.0,
    contract_count: float = 2.0,
) -> PositionRecord:
    """Build a single open long-call options position for MV-pricing tests."""
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(_OPT_UNDERLYING),
        strike_price=_OPT_STRIKE,
        expiration_date=_OPT_EXPIRY,
        contract_type=_OPT_CONTRACT_TYPE,
        contract_count=contract_count,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=premium,
        greeks=OptionGreeks(delta=0.6, gamma=0.01, theta=-0.5, vega=0.2),
    )
    fill = PositionFill(
        fill_timestamp=_ENTRY_AT,
        fill_price=price(premium),
        fill_quantity=contract_count,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )
    return PositionRecord(
        position_id=PositionId("POS-OPT-001"),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_ENTRY_AT,
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _occ_symbol_for_test() -> str:
    """Resolve the OCC symbol for the single test position via the shared helper."""
    return occ_symbol_for_options(_make_open_options_position().details)  # type: ignore[arg-type]


def _assemble_with_option_provider(
    *,
    option_quotes: dict[str, PriceQuote],
    premium: float = 5.0,
) -> AssembledSnapshot:
    """Drive a single-option-position snapshot through ``assemble_snapshot``."""
    pos = _make_open_options_position(premium=premium)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {_OPT_UNDERLYING: _make_fresh_quote(_OPT_UNDERLYING, 520.0)}, _NOW
    )
    option_provider = StubOptionPriceProvider(option_quotes, _NOW)
    return assemble_snapshot(
        repository=repo,
        price_provider=provider,
        option_price_provider=option_provider,
        sector_resolver=_null_sector_resolver,
        config=_make_config(),
        now=_NOW,
    )


def test_option_pricing_uses_live_quote_when_fresh() -> None:
    """A fresh option snapshot drives MV; entry premium is NOT used."""
    occ = _occ_symbol_for_test()
    live_premium = 7.50
    quote = PriceQuote(
        ticker=occ,
        price_usd=live_premium,
        as_of_timestamp=_NOW - timedelta(seconds=30),
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )
    assembled = _assemble_with_option_provider(option_quotes={occ: quote}, premium=5.0)
    view = assembled.snapshot.open_positions[0]
    # MV = 2 contracts * 100 multiplier * $7.50 = $1500
    expected_mv = 2.0 * LISTED_OPTION_CONTRACT_MULTIPLIER * live_premium
    assert float(view.current_market_value_usd) == pytest.approx(expected_mv)


def test_option_pricing_falls_back_to_entry_premium_when_stale() -> None:
    """A stale option snapshot is ignored; MV uses entry premium."""
    occ = _occ_symbol_for_test()
    quote = PriceQuote(
        ticker=occ,
        price_usd=7.50,
        as_of_timestamp=_NOW - timedelta(seconds=600),
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,  # The stub recomputes staleness against ``_NOW``.
    )
    assembled = _assemble_with_option_provider(option_quotes={occ: quote}, premium=5.0)
    view = assembled.snapshot.open_positions[0]
    # MV = 2 * 100 * $5.00 = $1000 (entry premium)
    expected_mv = 2.0 * LISTED_OPTION_CONTRACT_MULTIPLIER * 5.0
    assert float(view.current_market_value_usd) == pytest.approx(expected_mv)


def test_option_pricing_falls_back_to_entry_premium_when_no_snapshot() -> None:
    """An empty option provider keeps the entry-premium MV that pre-ALP-516 used."""
    assembled = _assemble_with_option_provider(option_quotes={}, premium=5.0)
    view = assembled.snapshot.open_positions[0]
    expected_mv = 2.0 * LISTED_OPTION_CONTRACT_MULTIPLIER * 5.0
    assert float(view.current_market_value_usd) == pytest.approx(expected_mv)


def test_strategy_pricing_mixes_live_leg_with_entry_premium_leg() -> None:
    """When one strategy leg has a live quote and the other does not, MV combines both."""
    leg1_premium = 10.0
    leg2_premium = 5.0
    leg1_live = 12.5
    pos = _make_strategy_position(premium1=leg1_premium, premium2=leg2_premium)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider(
        {"NVDA": _make_fresh_quote("NVDA", 520.0)},
        _NOW,
    )

    assert isinstance(pos.details, StrategyPositionDetails)
    leg1_occ = occ_symbol_for_options(pos.details.legs[0].options)
    option_provider = StubOptionPriceProvider(
        {
            leg1_occ: PriceQuote(
                ticker=leg1_occ,
                price_usd=leg1_live,
                as_of_timestamp=_NOW - timedelta(seconds=30),
                source=PriceSource.INTRADAY_QUOTE,
                is_stale=False,
            ),
        },
        _NOW,
    )

    assembled = assemble_snapshot(
        repository=repo,
        price_provider=provider,
        option_price_provider=option_provider,
        sector_resolver=_null_sector_resolver,
        config=_make_config(),
        now=_NOW,
    )
    view = assembled.snapshot.open_positions[0]

    # Leg 1 marks at the live quote, leg 2 falls back to entry premium.
    # Each leg has contract_count=1, multiplier=100.
    expected_mv = (
        1.0 * LISTED_OPTION_CONTRACT_MULTIPLIER * leg1_live
        + 1.0 * LISTED_OPTION_CONTRACT_MULTIPLIER * leg2_premium
    )
    assert float(view.current_market_value_usd) == pytest.approx(expected_mv)


# ---------------------------------------------------------------------------
# Strategy unrealized P/L percentage — non-inverting denominator (ALP-599)
# ---------------------------------------------------------------------------


def _assemble_credit_strategy(
    *,
    leg1_mark: float,
    leg2_mark: float,
    net_premium_usd: float,
    max_loss_usd: float,
) -> PositionView:
    """Assemble a SHORT-leg + LONG-leg credit strategy and return its enriched view.

    Leg 1 is SHORT (a written contract — a liability) and leg 2 is LONG. Both
    legs have ``contract_count=1`` and ``multiplier=100``; both option-contract
    OCC symbols are given fresh live marks so market value is driven entirely
    by ``leg1_mark`` / ``leg2_mark``.
    """
    pos = _make_strategy_position(
        leg1_direction=Direction.SHORT,
        leg2_direction=Direction.LONG,
        net_premium_usd=net_premium_usd,
        max_loss_usd=max_loss_usd,
    )
    assert isinstance(pos.details, StrategyPositionDetails)
    leg1_occ = occ_symbol_for_options(pos.details.legs[0].options)
    leg2_occ = occ_symbol_for_options(pos.details.legs[1].options)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 520.0)}, _NOW)
    option_provider = StubOptionPriceProvider(
        {
            leg1_occ: PriceQuote(
                ticker=leg1_occ,
                price_usd=leg1_mark,
                as_of_timestamp=_NOW - timedelta(seconds=30),
                source=PriceSource.INTRADAY_QUOTE,
                is_stale=False,
            ),
            leg2_occ: PriceQuote(
                ticker=leg2_occ,
                price_usd=leg2_mark,
                as_of_timestamp=_NOW - timedelta(seconds=30),
                source=PriceSource.INTRADAY_QUOTE,
                is_stale=False,
            ),
        },
        _NOW,
    )
    assembled = assemble_snapshot(
        repository=repo,
        price_provider=provider,
        option_price_provider=option_provider,
        sector_resolver=_null_sector_resolver,
        config=_make_config(),
        now=_NOW,
    )
    return assembled.snapshot.open_positions[0]


def test_strategy_credit_winner_has_positive_unrealized_pnl_pct() -> None:
    """ALP-599: a net-credit strategy with positive P/L USD shows a positive pct.

    Sold for a $300 credit (net_premium_usd = -300); legs now cost $150 to buy
    back (MV = -300 + 150 = -150). P/L USD = -150 - (-300) = +150 (profit).
    With abs(max_loss_usd)=500 the pct is +30% — not the -50% the old
    net_premium_usd denominator would have inverted it to.
    """
    view = _assemble_credit_strategy(
        leg1_mark=3.0,
        leg2_mark=1.5,
        net_premium_usd=-300.0,
        max_loss_usd=-500.0,
    )
    assert float(view.unrealized_pnl_usd) == pytest.approx(150.0)
    assert view.unrealized_pnl_pct == pytest.approx(30.0)
    assert view.unrealized_pnl_pct > 0.0


def test_strategy_credit_loser_has_negative_unrealized_pnl_pct() -> None:
    """ALP-599: a net-credit strategy with negative P/L USD shows a negative pct.

    Sold for a $300 credit; legs now cost $450 to buy back
    (MV = -700 + 250 = -450). P/L USD = -450 - (-300) = -150 (loss).
    With abs(max_loss_usd)=500 the pct is -30%.
    """
    view = _assemble_credit_strategy(
        leg1_mark=7.0,
        leg2_mark=2.5,
        net_premium_usd=-300.0,
        max_loss_usd=-500.0,
    )
    assert float(view.unrealized_pnl_usd) == pytest.approx(-150.0)
    assert view.unrealized_pnl_pct == pytest.approx(-30.0)
    assert view.unrealized_pnl_pct < 0.0


def test_strategy_pnl_pct_denominator_is_abs_max_loss_not_net_premium() -> None:
    """ALP-599: the strategy P/L pct denominator is abs(max_loss_usd).

    Holding P/L USD (+150) and net_premium_usd (-300) fixed, doubling the
    max_loss_usd magnitude halves the percentage — proving the denominator is
    abs(max_loss_usd). If the denominator were still net_premium_usd the pct
    would be a fixed -50% regardless of max_loss_usd.
    """
    view_max_loss_500 = _assemble_credit_strategy(
        leg1_mark=3.0,
        leg2_mark=1.5,
        net_premium_usd=-300.0,
        max_loss_usd=-500.0,
    )
    view_max_loss_1000 = _assemble_credit_strategy(
        leg1_mark=3.0,
        leg2_mark=1.5,
        net_premium_usd=-300.0,
        max_loss_usd=-1000.0,
    )
    # Same P/L USD in both: +150.
    assert float(view_max_loss_500.unrealized_pnl_usd) == pytest.approx(150.0)
    assert float(view_max_loss_1000.unrealized_pnl_usd) == pytest.approx(150.0)
    # 150 / 500 = 30%; 150 / 1000 = 15% — denominator tracks abs(max_loss_usd).
    assert view_max_loss_500.unrealized_pnl_pct == pytest.approx(30.0)
    assert view_max_loss_1000.unrealized_pnl_pct == pytest.approx(15.0)


def test_strategy_zero_max_loss_yields_zero_pnl_pct() -> None:
    """ALP-599: a strategy with max_loss_usd == 0.0 (skeleton) yields pnl_pct == 0.0."""
    view = _assemble_credit_strategy(
        leg1_mark=3.0,
        leg2_mark=1.5,
        net_premium_usd=-300.0,
        max_loss_usd=0.0,
    )
    assert float(view.unrealized_pnl_usd) == pytest.approx(150.0)
    assert view.unrealized_pnl_pct == 0.0


def test_strategy_unbounded_max_loss_yields_zero_pnl_pct() -> None:
    """ALP-599: a strategy with max_loss_usd == -inf yields pnl_pct == 0.0, no blow-up."""
    view = _assemble_credit_strategy(
        leg1_mark=3.0,
        leg2_mark=1.5,
        net_premium_usd=-300.0,
        max_loss_usd=float("-inf"),
    )
    assert float(view.unrealized_pnl_usd) == pytest.approx(150.0)
    assert view.unrealized_pnl_pct == 0.0


def test_equity_unrealized_pnl_pct_divides_by_cost_basis_unchanged() -> None:
    """ALP-599 regression: an EQUITY position's P/L pct still divides by cost basis.

    100 shares at $500 cost (cost basis $50,000); current price $550
    (MV $55,000). P/L USD = $5,000; P/L pct = 5000 / 50000 = 10% — the
    instrument_type branch leaves the equity path on compute_unrealized_pnl_pct.
    """
    pos = _make_equity_position(share_count=100.0, cost_per_share=500.0)
    fixture = _make_fixture(
        open_positions=(pos,),
        cash_ledger=_make_cash_ledger(current_cash=0.0),
    )
    repo = StubPortfolioStateRepository(fixture)
    provider = StubCurrentPriceProvider({"NVDA": _make_fresh_quote("NVDA", 550.0)}, _NOW)
    assembled = assemble_snapshot(
        repository=repo,
        price_provider=provider,
        option_price_provider=StubOptionPriceProvider({}, _NOW),
        sector_resolver=_null_sector_resolver,
        config=_make_config(),
        now=_NOW,
    )
    view = assembled.snapshot.open_positions[0]
    assert float(view.unrealized_pnl_usd) == pytest.approx(5_000.0)
    assert view.unrealized_pnl_pct == pytest.approx(10.0)


def test_single_leg_option_unrealized_pnl_pct_divides_by_cost_basis_unchanged() -> None:
    """ALP-599 regression: a single-leg OPTIONS position's P/L pct still divides by cost basis.

    2 contracts at $5 premium (cost basis = 2 * 100 * 5 = $1,000); live mark
    $7.50 (MV = 2 * 100 * 7.50 = $1,500). P/L USD = $500; P/L pct =
    500 / 1000 = 50% — the instrument_type branch leaves OPTIONS unchanged.
    """
    occ = _occ_symbol_for_test()
    assembled = _assemble_with_option_provider(
        option_quotes={
            occ: PriceQuote(
                ticker=occ,
                price_usd=7.50,
                as_of_timestamp=_NOW - timedelta(seconds=30),
                source=PriceSource.INTRADAY_QUOTE,
                is_stale=False,
            )
        },
        premium=5.0,
    )
    view = assembled.snapshot.open_positions[0]
    assert float(view.unrealized_pnl_usd) == pytest.approx(500.0)
    assert view.unrealized_pnl_pct == pytest.approx(50.0)
