"""Tests for the per-class-group PM stress revaluation (ALP-425, story 03b).

Per ``regt-margin-attribution.md`` § Portfolio-margin reference model:
``stress_class_group`` returns the worst-case P/L magnitude across a 10-point
equidistant shock grid applied to one class group's instruments.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from alphamind.execution.regt_margin_attribution import (
    ClassGroup,
    IvShockMultipliers,
    RegTMarginAttributionConfig,
    ShockParameters,
    stress_class_group,
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
from alphamind.risk_guardrails.guardrail_evaluation import (
    ContractType,
    FixtureIvProvider,
    IvLookupError,
    IvLookupResult,
    IvProvider,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    MarketInputs,
    bs_price,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION = date(2026, 5, 28)  # 30 days from _AS_OF
_TIME_TO_EXPIRATION_YEARS = 30 / 365
_RISK_FREE_RATE = 0.0425
_SPOT = 100.0
_STRIKE = 100.0
_IV = 0.30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    per_symbol_overrides: Mapping[str, float] | None = None,
    unmapped_default: float = 0.20,
    worst_down_multiplier: float = 1.20,
    worst_up_multiplier: float = 0.95,
) -> RegTMarginAttributionConfig:
    return RegTMarginAttributionConfig(
        pm_model_version="test_v1",
        shock_parameters=ShockParameters(
            per_symbol_overrides=dict(per_symbol_overrides or {}),
            high_cap_equity=0.15,
            small_cap_equity=0.20,
            unmapped_default=unmapped_default,
        ),
        iv_shock=IvShockMultipliers(
            worst_down_multiplier=worst_down_multiplier,
            worst_up_multiplier=worst_up_multiplier,
        ),
        risk_free_rate_annual=_RISK_FREE_RATE,
    )


def _fill() -> PositionFill:
    return PositionFill(
        fill_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        fill_price=100.0,
        fill_quantity=10.0,
        slippage=0.0,
        fees=1.0,
    )


def _greeks() -> OptionGreeks:
    return OptionGreeks(delta=0.5, gamma=0.05, theta=-0.02, vega=0.10)


def _equity_position(
    *,
    position_id: str,
    ticker: str,
    share_count: float,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=ticker,
        share_count=share_count,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=0.05 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=500.0 if is_short else None,
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _option_position(
    *,
    position_id: str,
    underlying_ticker: str,
    strike: float = _STRIKE,
    contract_count: float = 1.0,
    contract_type: OptionContractType = OptionContractType.CALL,
    direction: Direction = Direction.LONG,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=underlying_ticker,
        strike_price=strike,
        expiration_date=_EXPIRATION,
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.5,
        greeks=_greeks(),
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _bull_call_spread_position(
    *,
    position_id: str,
    underlying_ticker: str,
    long_strike: float,
    short_strike: float,
) -> PositionRecord:
    long_leg_details = OptionsPositionDetails(
        underlying_ticker=underlying_ticker,
        strike_price=long_strike,
        expiration_date=_EXPIRATION,
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=_greeks(),
    )
    short_leg_details = OptionsPositionDetails(
        underlying_ticker=underlying_ticker,
        strike_price=short_strike,
        expiration_date=_EXPIRATION,
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=_greeks(),
    )
    long_leg = StrategyLeg(
        leg_id="long_call",
        direction=Direction.LONG,
        options=long_leg_details,
    )
    short_leg = StrategyLeg(
        leg_id="short_call",
        direction=Direction.SHORT,
        options=short_leg_details,
    )
    details = StrategyPositionDetails(
        strategy_type_label="bull_call_spread",
        legs=(long_leg, short_leg),
        net_premium_usd=-300.0,
        max_profit_usd=200.0,
        max_loss_usd=-300.0,
        breakeven_levels=(103.0,),
        strategy_greeks=_greeks(),
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 1, 1, 14, 30, tzinfo=UTC),
        details=details,
        execution_history=(_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _iv_provider(
    underlying: str,
    *,
    strikes: tuple[float, ...] = (_STRIKE,),
) -> FixtureIvProvider:
    quotes = tuple(
        IvQuote(
            strike=strike,
            expiration=_EXPIRATION,
            contract_type=ct,
            implied_volatility=_IV,
        )
        for strike in strikes
        for ct in (ContractType.CALL, ContractType.PUT)
    )
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(underlying=underlying, quotes=quotes),
        },
        realized_vol={},
    )


def _market(
    *,
    underlying: str = "AAPL",
    spot: float = _SPOT,
    iv_provider: IvProvider | None = None,
    strikes: tuple[float, ...] = (_STRIKE,),
) -> MarketInputs:
    return MarketInputs(
        underlying_prices={underlying: spot},
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=iv_provider
        if iv_provider is not None
        else _iv_provider(underlying, strikes=strikes),
        as_of=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_equity_only_class_group_worst_loss_is_shock_pct_times_market_value() -> None:
    """Long equity, shock 15% → worst loss = 0.15 * |quantity| * current_price."""
    aapl = _equity_position(position_id="p1", ticker="AAPL", share_count=10.0)
    group = ClassGroup(underlying_symbol="AAPL", positions=(aapl,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.15})

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Worst loss is on the down-shock: 0.15 * 10 * 100 = 150.0
    assert margin == pytest.approx(0.15 * 10.0 * 100.0)


def test_short_equity_only_class_group_worst_loss_on_up_shock() -> None:
    """Short equity, shock 15% → worst loss = 0.15 * |quantity| * current_price (up-shock)."""
    aapl_short = _equity_position(
        position_id="p1",
        ticker="AAPL",
        share_count=10.0,
        direction=Direction.SHORT,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(aapl_short,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.15})

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Short loses on up-shock; magnitude == 0.15 * 10 * 100 = 150.0
    assert margin == pytest.approx(0.15 * 10.0 * 100.0)


def test_long_call_only_class_group_worst_loss_on_down_shock() -> None:
    """Long call (positive delta) -> worst loss on -shock direction; magnitude < premium."""
    call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(call,))
    shock_pct = 0.20
    cfg = _config(per_symbol_overrides={"AAPL": shock_pct})

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Theoretical max loss for a long call is the premium times multiplier.
    # Compute the baseline premium using bs_price at the unstressed inputs and
    # IV at the midpoint multiplier.
    baseline_premium = bs_price(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    max_theoretical_loss = baseline_premium * 1.0 * 100.0  # one contract * 100 multiplier
    assert margin > 0
    assert margin < max_theoretical_loss

    # Verify the worst-case direction matches -shock: compute shocked premium at
    # the worst-down endpoint and confirm it produces a loss.
    worst_down_iv = _IV * cfg.iv_shock.worst_down_multiplier
    shocked_down_premium = bs_price(
        spot=100.0 * (1.0 - shock_pct),
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=worst_down_iv,
        contract_type=ContractType.CALL,
    )
    # Loss per leg = (shocked_price - baseline_price) * signed_contracts * 100
    expected_down_pl = (shocked_down_premium - baseline_premium) * 1.0 * 100.0
    assert expected_down_pl < 0
    assert margin == pytest.approx(abs(expected_down_pl))


def test_long_put_only_class_group_worst_loss_on_up_shock() -> None:
    """Long put (negative delta) → worst loss on +shock direction."""
    put = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.PUT,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(put,))
    shock_pct = 0.20
    cfg = _config(per_symbol_overrides={"AAPL": shock_pct})

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    baseline_premium = bs_price(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.PUT,
    )
    # IV multiplier at the +1.0 endpoint is worst_up_multiplier.
    worst_up_iv = _IV * cfg.iv_shock.worst_up_multiplier
    shocked_up_premium = bs_price(
        spot=100.0 * (1.0 + shock_pct),
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=worst_up_iv,
        contract_type=ContractType.PUT,
    )
    expected_up_pl = (shocked_up_premium - baseline_premium) * 1.0 * 100.0
    assert expected_up_pl < 0
    assert margin == pytest.approx(abs(expected_up_pl))


def test_short_call_class_group_worst_loss_on_up_shock() -> None:
    """Short call → worst loss on +shock (call value rises, short loses)."""
    short_call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.SHORT,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(short_call,))
    shock_pct = 0.20
    cfg = _config(per_symbol_overrides={"AAPL": shock_pct})

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    baseline_premium = bs_price(
        spot=100.0,
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    worst_up_iv = _IV * cfg.iv_shock.worst_up_multiplier
    shocked_up_premium = bs_price(
        spot=100.0 * (1.0 + shock_pct),
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=worst_up_iv,
        contract_type=ContractType.CALL,
    )
    # signed_contracts = -1 for short
    expected_up_pl = (shocked_up_premium - baseline_premium) * -1.0 * 100.0
    assert expected_up_pl < 0
    assert margin == pytest.approx(abs(expected_up_pl))


def test_hedged_equity_plus_long_call_class_group_smaller_than_unhedged_call() -> None:
    """Short equity + long call hedge → combined worst loss < standalone short equity.

    A long call insures a short equity position against upside-shock loss:
    on the +shock direction the short stock loses (large) while the long
    call gains (partially offsetting), so the combined class-group worst
    loss across the grid is strictly less than the standalone short-equity
    worst loss. This also verifies that within a class group the
    per-grid-point net P/L is the sum of per-instrument P/Ls (no
    max-of-instruments offset).
    """
    shock_pct = 0.20
    cfg = _config(per_symbol_overrides={"AAPL": shock_pct})

    # Standalone short equity.
    short_equity = _equity_position(
        position_id="p1",
        ticker="AAPL",
        share_count=10.0,
        direction=Direction.SHORT,
    )
    standalone_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="AAPL", positions=(short_equity,)),
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Short equity + long call (the hedge). 10 shares short paired with one
    # long ATM call (10x100-multiplier = 100 deltas on the call side covers
    # the short stock's 10 shares).
    long_call = _option_position(
        position_id="p2",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    combined_margin = stress_class_group(
        class_group=ClassGroup(underlying_symbol="AAPL", positions=(short_equity, long_call)),
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # The hedge offsets some of the short equity's up-shock loss, so the
    # combined worst-loss magnitude is strictly less than the standalone.
    assert combined_margin < standalone_margin


def test_iv_shock_paired_to_price_shock_direction() -> None:
    """At grid endpoints, the IV used == baseline_iv * configured endpoint multiplier.

    Records every ``bs_price`` IV input via a tracking provider that returns
    a fixed baseline ``_IV``, then patches ``bs_price`` to capture the
    ``implied_volatility`` value passed at each call. We verify that the
    minimum IV across all calls equals ``baseline_iv * worst_up_multiplier``
    (used at the +1.0 grid endpoint) and the maximum equals
    ``baseline_iv * worst_down_multiplier`` (used at -1.0).
    """
    call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(call,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.20})

    captured_ivs: list[float] = []

    def _capture_bs_price(**kwargs: object) -> float:
        iv = kwargs["implied_volatility"]
        assert isinstance(iv, float)
        captured_ivs.append(iv)
        return bs_price(**kwargs)  # type: ignore[arg-type]

    with patch(
        "alphamind.execution.regt_margin_attribution.pm_stress.bs_price",
        side_effect=_capture_bs_price,
    ):
        stress_class_group(
            class_group=group,
            market_inputs=_market(underlying="AAPL", spot=100.0),
            config=cfg,
        )

    # Exclude the baseline lookup (called once at unstressed inputs); the
    # remaining 10 calls correspond to the 10 grid points.
    grid_ivs = captured_ivs[1:]
    assert len(grid_ivs) == 10
    assert min(grid_ivs) == pytest.approx(_IV * cfg.iv_shock.worst_up_multiplier)
    assert max(grid_ivs) == pytest.approx(_IV * cfg.iv_shock.worst_down_multiplier)


def test_grid_has_ten_points() -> None:
    """The function evaluates exactly 10 shock points per option leg.

    Counts ``bs_price`` invocations through a patched seam: one call for the
    baseline revaluation (per leg), then one per grid point per leg. With
    one leg, the total is ``1 + 10 == 11``.
    """
    call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(call,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.20})

    call_count = 0

    def _count_bs_price(**kwargs: object) -> float:
        nonlocal call_count
        call_count += 1
        return bs_price(**kwargs)  # type: ignore[arg-type]

    with patch(
        "alphamind.execution.regt_margin_attribution.pm_stress.bs_price",
        side_effect=_count_bs_price,
    ):
        stress_class_group(
            class_group=group,
            market_inputs=_market(underlying="AAPL", spot=100.0),
            config=cfg,
        )

    # 1 baseline + 10 grid points = 11 total bs_price invocations for one leg.
    assert call_count == 11


def test_unknown_underlying_raises_key_error() -> None:
    """Class group with underlying absent from market_inputs.underlying_prices raises KeyError."""
    aapl = _equity_position(position_id="p1", ticker="AAPL", share_count=10.0)
    group = ClassGroup(underlying_symbol="AAPL", positions=(aapl,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.15})

    market = MarketInputs(
        underlying_prices={"NVDA": 500.0},  # AAPL absent
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=_iv_provider("AAPL"),
        as_of=_AS_OF,
    )

    with pytest.raises(KeyError):
        stress_class_group(class_group=group, market_inputs=market, config=cfg)


def test_strategy_position_sums_per_leg_pl_at_each_grid_point() -> None:
    """NVDA bull call spread: per-grid-point net P/L equals long-leg P/L + short-leg P/L.

    Bull call spread = long ATM call (100) + short OTM call (105). Defined-risk
    bullish: max gain at +shock, max loss at -shock. Verify the
    ``stress_class_group`` result equals a hand-rolled per-grid-point sum
    (compute baseline and shocked premiums for each leg, sum signed leg P/Ls,
    take ``abs(min(...))`` over the grid).
    """
    spread = _bull_call_spread_position(
        position_id="p1",
        underlying_ticker="NVDA",
        long_strike=100.0,
        short_strike=105.0,
    )
    group = ClassGroup(underlying_symbol="NVDA", positions=(spread,))
    shock_pct = 0.20
    cfg = _config(per_symbol_overrides={"NVDA": shock_pct})
    spot = 100.0

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(
            underlying="NVDA",
            spot=spot,
            strikes=(100.0, 105.0),
        ),
        config=cfg,
    )

    # Hand-roll the per-grid-point P/L using the same convention as the module.
    grid_positions = [-1.0 + i * (2.0 / 9.0) for i in range(10)]
    long_baseline = bs_price(
        spot=spot,
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    short_baseline = bs_price(
        spot=spot,
        strike=105.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )

    iv_low = cfg.iv_shock.worst_down_multiplier
    iv_high = cfg.iv_shock.worst_up_multiplier

    expected_pls: list[float] = []
    for grid_pos in grid_positions:
        shocked_spot = spot * (1.0 + grid_pos * shock_pct)
        weight = (grid_pos + 1.0) / 2.0
        iv_mult = iv_low + weight * (iv_high - iv_low)
        long_shocked = bs_price(
            spot=shocked_spot,
            strike=100.0,
            time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
            risk_free_rate=_RISK_FREE_RATE,
            implied_volatility=_IV * iv_mult,
            contract_type=ContractType.CALL,
        )
        short_shocked = bs_price(
            spot=shocked_spot,
            strike=105.0,
            time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
            risk_free_rate=_RISK_FREE_RATE,
            implied_volatility=_IV * iv_mult,
            contract_type=ContractType.CALL,
        )
        long_pl = (long_shocked - long_baseline) * 1.0 * 100.0  # +1 contract long
        short_pl = (short_shocked - short_baseline) * -1.0 * 100.0  # -1 contract short
        expected_pls.append(long_pl + short_pl)

    expected_margin = abs(min(expected_pls))
    assert margin == pytest.approx(expected_margin)

    # Sanity check the strategy direction: defined-risk bullish, worst at -1.0.
    worst_grid_index = min(range(10), key=lambda i: expected_pls[i])
    assert worst_grid_index == 0  # the -1.0 grid endpoint


def test_lookup_iv_called_once_per_option_leg() -> None:
    """``IvProvider.lookup_iv`` is called exactly once per option leg, not 10x per leg.

    The IV multiplier is applied to the baseline returned by the provider; the
    provider itself is not re-consulted per grid point.
    """

    class _CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def lookup_iv(
            self,
            *,
            underlying: str,
            strike: float,
            expiration: date,
            contract_type: ContractType,
            as_of: datetime,
        ) -> IvLookupResult:
            del underlying, strike, expiration, contract_type, as_of
            self.calls += 1
            return IvLookupResult(
                implied_volatility=_IV,
                source=IvSource.SURFACE,
                notes=None,
            )

    long_call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    short_put = _option_position(
        position_id="p2",
        underlying_ticker="AAPL",
        strike=95.0,
        contract_count=1.0,
        contract_type=OptionContractType.PUT,
        direction=Direction.SHORT,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(long_call, short_put))
    cfg = _config(per_symbol_overrides={"AAPL": 0.20})
    provider = _CountingProvider()

    stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0, iv_provider=provider),
        config=cfg,
    )

    # Two option legs: lookup_iv called exactly twice.
    assert provider.calls == 2


def test_missing_iv_propagates_iv_lookup_error() -> None:
    """Missing IV (no surface entry, no realized-vol fallback) propagates IvLookupError."""
    call = _option_position(
        position_id="p1",
        underlying_ticker="AAPL",
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(call,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.20})

    # Provider with no surface and no realized-vol entries.
    empty_provider = FixtureIvProvider(surface={}, realized_vol={})

    market = MarketInputs(
        underlying_prices={"AAPL": 100.0},
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=empty_provider,
        as_of=_AS_OF,
    )

    with pytest.raises(IvLookupError):
        stress_class_group(class_group=group, market_inputs=market, config=cfg)


def test_unmapped_symbol_falls_through_to_unmapped_default() -> None:
    """Underlying with no per_symbol_overrides entry uses unmapped_default for shock_pct."""
    aapl = _equity_position(position_id="p1", ticker="AAPL", share_count=10.0)
    group = ClassGroup(underlying_symbol="AAPL", positions=(aapl,))
    # No per-symbol override for AAPL; unmapped_default = 0.25.
    cfg = _config(per_symbol_overrides={}, unmapped_default=0.25)

    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Worst loss = 0.25 * 10 * 100 = 250.0
    assert margin == pytest.approx(0.25 * 10.0 * 100.0)


def test_lower_case_underlying_ticker_resolves_both_price_and_iv() -> None:
    """A lower-case ``underlying_ticker`` on an option leg resolves through both
    ``underlying_prices`` and the IV provider.

    ``OptionsPositionDetails.underlying_ticker`` (and ``StrategyLeg.options.``)
    carries no normalisation, so the baseline-lookup helper must normalise once
    and reuse for both lookups; otherwise the price read upper-cases the key
    while the IV lookup passes the raw (lower-case) ticker and silently misses
    the surface entry. This regression test confirms the lookups are unified.
    """
    call = _option_position(
        position_id="p1",
        underlying_ticker="aapl",  # lower-case — class-group symbol is upper-case
        strike=100.0,
        contract_count=1.0,
        contract_type=OptionContractType.CALL,
        direction=Direction.LONG,
    )
    group = ClassGroup(underlying_symbol="AAPL", positions=(call,))
    cfg = _config(per_symbol_overrides={"AAPL": 0.20})

    # ``underlying_prices`` keyed upper-case; ``FixtureIvProvider.surface``
    # also keyed upper-case (the production case). Without the unified
    # normalisation, the IV lookup would miss the AAPL surface entry on the
    # raw lower-case ticker and raise IvLookupError.
    margin = stress_class_group(
        class_group=group,
        market_inputs=_market(underlying="AAPL", spot=100.0),
        config=cfg,
    )

    # Sanity: a positive worst-case loss (matches the long-call class-group case).
    assert margin > 0
