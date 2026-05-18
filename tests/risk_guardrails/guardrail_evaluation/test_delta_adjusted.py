"""Tests for delta-adjusted exposure (story 03).

The function under test combines Black-Scholes greeks (story 02a), IV sourcing
(story 02b), and the conservative buffer into a single ``DeltaAdjustedExposure``
per proposal. Tests exercise the equity, single-leg option, and multi-leg
strategy branches plus the action-conditional behaviour for OPEN/ADD/CLOSE/
ADJUST/CANCEL.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money
from alphamind.risk_guardrails.guardrail_evaluation import (
    Action,
    AssetType,
    ContractType,
    DeltaAdjustedExposure,
    Direction,
    EscalationZones,
    FeatureFlagsView,
    FixtureIvProvider,
    Greeks,
    IvQuote,
    IvSource,
    IvSurfaceEntry,
    LibraryConfig,
    MarketInputs,
    OptionLeg,
    ProposedDelta,
    RealizedVolEntry,
    bs_greeks,
    compute_delta_adjusted_exposure,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_AS_OF = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)
_EXPIRATION = date(2026, 5, 28)  # 30 days from _AS_OF
_TIME_TO_EXPIRATION_YEARS = 30 / 365
_RISK_FREE_RATE = 0.045
_SPOT = 100.0
_STRIKE = 100.0
_IV = 0.30
_CONTRACT_MULTIPLIER = 100


def _market(
    *,
    underlying_prices: Mapping[str, float] | None = None,
    iv_provider: FixtureIvProvider | None = None,
) -> MarketInputs:
    if underlying_prices is None:
        underlying_prices = {"AAPL": _SPOT}
    if iv_provider is None:
        iv_provider = _atm_provider("AAPL")
    return MarketInputs(
        underlying_prices=underlying_prices,
        risk_free_rate=_RISK_FREE_RATE,
        iv_provider=iv_provider,
        as_of=_AS_OF,
    )


def _atm_provider(underlying: str) -> FixtureIvProvider:
    """Surface with both call and put IVs at strike 100, expiry 30 days out."""
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=_STRIKE,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=_STRIKE,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.PUT,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _config(
    *,
    active_regime: str = "normal",
    conservative_buffer_pct: float = 10.0,
) -> LibraryConfig:
    return LibraryConfig(
        effective_limits={},
        escalation_zones={
            "noop": EscalationZones(warning=0.5, critical=0.8, hard_block=1.0),
        },
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("Technology",),
        active_regime=active_regime,
        active_profile="medium",
        conservative_buffer_pct=conservative_buffer_pct,
    )


def _equity(
    *,
    direction: Direction = Direction.LONG,
    notional_usd: float = 5_000.0,
    action: Action = Action.OPEN,
) -> ProposedDelta:
    return ProposedDelta(
        id="proposal-eq",
        underlying=Symbol("AAPL"),
        sector="Technology",
        direction=direction,
        asset_type=AssetType.EQUITY,
        notional_usd=money(notional_usd),
        quantity=notional_usd / _SPOT,
        option_legs=None,
        action=action,
        existing_position_id=None,
    )


def _single_call(
    *,
    quantity: float = 10.0,
    direction: Direction = Direction.LONG,
    leg_quantity: int = 1,
    action: Action = Action.OPEN,
) -> ProposedDelta:
    return ProposedDelta(
        id="proposal-opt",
        underlying=Symbol("AAPL"),
        sector="Technology",
        direction=direction,
        asset_type=AssetType.OPTION,
        notional_usd=money(1_000.0),
        quantity=quantity,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=_STRIKE,
                expiration=_EXPIRATION,
                quantity=leg_quantity,
            ),
        ),
        action=action,
        existing_position_id=None,
    )


# ---------------------------------------------------------------------------
# Equity branch
# ---------------------------------------------------------------------------


def test_equity_long_open_returns_positive_signed_notional() -> None:
    """Equity long OPEN: ``signed_notional_usd = +notional_usd``;
    ``net_greeks = None``."""
    proposal = _equity(direction=Direction.LONG, notional_usd=5_000.0)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result == DeltaAdjustedExposure(
        proposal_id="proposal-eq",
        signed_notional_usd=5_000.0,
        net_greeks=None,
        iv_used=None,
        iv_source=None,
        unbuffered_delta=None,
    )


def test_equity_short_open_returns_negative_signed_notional() -> None:
    """Equity short OPEN: ``signed_notional_usd = -notional_usd``."""
    proposal = _equity(direction=Direction.SHORT, notional_usd=3_000.0)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result.signed_notional_usd == -3_000.0
    assert result.net_greeks is None


# ---------------------------------------------------------------------------
# Single-leg option branch
# ---------------------------------------------------------------------------


def test_single_leg_atm_call_open_applies_buffer_and_signed_notional() -> None:
    """ATM call long OPEN under medium * normal regime: ``signed_notional_usd``
    is ``bs_delta * (1 + 0.10) * spot * 100 * quantity``."""
    proposal = _single_call(quantity=10.0, direction=Direction.LONG)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    bs = bs_greeks(
        spot=_SPOT,
        strike=_STRIKE,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    expected_buffered = abs(bs.delta) * (1 + 0.10)
    expected_notional = expected_buffered * _SPOT * _CONTRACT_MULTIPLIER * 10.0

    assert result.signed_notional_usd == pytest.approx(expected_notional, rel=1e-9)
    assert result.net_greeks == bs
    assert result.iv_used == pytest.approx(_IV, rel=1e-12)
    assert result.iv_source is IvSource.SURFACE
    assert result.unbuffered_delta == pytest.approx(abs(bs.delta), rel=1e-12)


def test_buffer_inflates_absolute_delta_by_pct() -> None:
    """``conservative_buffer_pct=10`` * normal regime: the buffered delta is
    ``unbuffered * 1.10``. Implementing the spec's ``0.50 → 0.55`` example
    structurally — the BS layer's exact delta varies with greeks but the buffer
    relationship is the contract."""
    proposal = _single_call(quantity=1.0, direction=Direction.LONG)

    result = compute_delta_adjusted_exposure(
        proposal=proposal,
        market=_market(),
        config=_config(conservative_buffer_pct=10.0, active_regime="normal"),
    )

    assert result.unbuffered_delta is not None
    buffered_from_signed = result.signed_notional_usd / (_SPOT * _CONTRACT_MULTIPLIER * 1.0)
    assert buffered_from_signed == pytest.approx(result.unbuffered_delta * 1.10, abs=1e-6)


def test_elevated_regime_applies_one_point_five_buffer_multiplier() -> None:
    """``regime=elevated`` applies a 1.5* buffer multiplier per the literal
    mapping: with ``conservative_buffer_pct=10``, effective buffer is 15%, so
    buffered |delta| = unbuffered * 1.15."""
    proposal = _single_call(quantity=1.0, direction=Direction.LONG)

    result = compute_delta_adjusted_exposure(
        proposal=proposal,
        market=_market(),
        config=_config(conservative_buffer_pct=10.0, active_regime="elevated"),
    )

    assert result.unbuffered_delta is not None
    buffered_from_signed = result.signed_notional_usd / (_SPOT * _CONTRACT_MULTIPLIER * 1.0)
    assert buffered_from_signed == pytest.approx(
        result.unbuffered_delta * (1 + 0.10 * 1.5), abs=1e-9
    )


# ---------------------------------------------------------------------------
# Strategy aggregation
# ---------------------------------------------------------------------------


def _spread_provider(underlying: str) -> FixtureIvProvider:
    """Surface for a long call spread: 100/110 strikes, 30 days, IV=0.30."""
    return FixtureIvProvider(
        surface={
            underlying: IvSurfaceEntry(
                underlying=underlying,
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                    IvQuote(
                        strike=110.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={},
    )


def _long_call_spread() -> ProposedDelta:
    return ProposedDelta(
        id="proposal-spread",
        underlying=Symbol("AAPL"),
        sector="Technology",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(500.0),
        quantity=1.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=110.0,
                expiration=_EXPIRATION,
                quantity=-1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )


def test_long_call_spread_aggregates_to_net_delta_smaller_than_either_leg() -> None:
    """A long 100-call + short 110-call has a positive net delta strictly
    between zero and either leg's |delta|. The buffer applies to the *net*,
    not to each leg."""
    proposal = _long_call_spread()
    market = _market(iv_provider=_spread_provider("AAPL"))
    config = _config(active_regime="normal", conservative_buffer_pct=10.0)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=market, config=config)

    leg_low_delta = bs_greeks(
        spot=_SPOT,
        strike=100.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    ).delta
    leg_high_delta = bs_greeks(
        spot=_SPOT,
        strike=110.0,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    ).delta
    expected_net_delta = leg_low_delta - leg_high_delta

    assert result.net_greeks is not None
    assert result.net_greeks.delta == pytest.approx(expected_net_delta, rel=1e-9)
    # Net delta is positive and strictly between zero and the long leg's
    # delta (the larger leg contribution): canonical long-call-spread property.
    assert 0 < result.net_greeks.delta < leg_low_delta
    # Buffer applied once to the net (not per-leg).
    expected_signed = abs(expected_net_delta) * 1.10 * _SPOT * _CONTRACT_MULTIPLIER * 1.0
    assert result.signed_notional_usd == pytest.approx(expected_signed, rel=1e-9)
    # Unbuffered audit field reports the net |delta|, not the sum of leg |delta|s.
    assert result.unbuffered_delta is not None
    assert result.unbuffered_delta == pytest.approx(abs(expected_net_delta), rel=1e-12)
    assert result.unbuffered_delta < abs(leg_low_delta) + abs(leg_high_delta)


def test_atm_straddle_has_near_zero_delta_with_long_vol_greek_signs() -> None:
    """Long ATM call + long ATM put: net delta ≈ 0, gamma > 0, theta < 0,
    vega > 0 (canonical long-volatility exposure)."""
    proposal = ProposedDelta(
        id="proposal-straddle",
        underlying=Symbol("AAPL"),
        sector="Technology",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(600.0),
        quantity=1.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=_STRIKE,
                expiration=_EXPIRATION,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.PUT,
                strike=_STRIKE,
                expiration=_EXPIRATION,
                quantity=1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result.net_greeks is not None
    # Near-zero net delta — call delta ~+0.55, put delta ~-0.45 (slight rate
    # asymmetry); test asserts |net delta| < 0.15 to leave headroom for
    # interest-rate effects without pinning the precise BS value.
    assert abs(result.net_greeks.delta) < 0.15
    # Long-vol signs.
    assert result.net_greeks.gamma > 0
    assert result.net_greeks.theta < 0
    assert result.net_greeks.vega > 0


# ---------------------------------------------------------------------------
# Action-conditional behaviour
# ---------------------------------------------------------------------------


def test_close_long_equity_negates_signed_notional() -> None:
    """``Action=CLOSE`` on a long equity returns ``signed_notional_usd`` with
    sign opposite to ``Action=OPEN`` on the same instrument/direction."""
    open_proposal = _equity(direction=Direction.LONG, notional_usd=5_000.0, action=Action.OPEN)
    close_proposal = _equity(direction=Direction.LONG, notional_usd=5_000.0, action=Action.CLOSE)

    open_result = compute_delta_adjusted_exposure(
        proposal=open_proposal, market=_market(), config=_config()
    )
    close_result = compute_delta_adjusted_exposure(
        proposal=close_proposal, market=_market(), config=_config()
    )

    assert close_result.signed_notional_usd == -open_result.signed_notional_usd
    assert open_result.signed_notional_usd > 0


def test_close_long_call_negates_signed_notional() -> None:
    """Same convention applies to options: closing a long call decreases
    exposure, so ``signed_notional_usd`` is the negative of OPEN's."""
    open_proposal = _single_call(direction=Direction.LONG, action=Action.OPEN)
    close_proposal = _single_call(direction=Direction.LONG, action=Action.CLOSE)

    open_result = compute_delta_adjusted_exposure(
        proposal=open_proposal, market=_market(), config=_config()
    )
    close_result = compute_delta_adjusted_exposure(
        proposal=close_proposal, market=_market(), config=_config()
    )

    assert close_result.signed_notional_usd == pytest.approx(
        -open_result.signed_notional_usd, rel=1e-12
    )


def test_adjust_returns_zero_signed_notional_and_zero_greeks_for_options() -> None:
    """``Action=ADJUST`` is exposure-neutral: ``signed_notional_usd=0`` and
    ``net_greeks=Greeks(0,0,0,0)`` for options."""
    proposal = _single_call(action=Action.ADJUST)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result.signed_notional_usd == 0.0
    assert result.net_greeks == Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)


def test_adjust_returns_zero_signed_notional_for_equity() -> None:
    """Equity ADJUST is also exposure-neutral; greeks stay None for equity."""
    proposal = _equity(action=Action.ADJUST)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result.signed_notional_usd == 0.0
    assert result.net_greeks is None


def test_cancel_returns_zero_signed_notional() -> None:
    """``Action=CANCEL`` is exposure-neutral; the cancelled order's reserved
    capital is released by the capital rule's contribution function (story 04)
    reading ``proposal.notional_usd`` directly, not the math layer's output."""
    proposal = _equity(action=Action.CANCEL)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    assert result.signed_notional_usd == 0.0


def test_unbuffered_delta_equals_bs_delta_for_options_and_none_for_equity() -> None:
    """Audit-trail field: options report the *unbuffered* |bs_delta| exactly;
    equity reports ``None``."""
    option = compute_delta_adjusted_exposure(
        proposal=_single_call(quantity=1.0, direction=Direction.LONG),
        market=_market(),
        config=_config(),
    )
    bs = bs_greeks(
        spot=_SPOT,
        strike=_STRIKE,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )
    assert option.unbuffered_delta == pytest.approx(abs(bs.delta), rel=1e-12)

    equity = compute_delta_adjusted_exposure(
        proposal=_equity(),
        market=_market(),
        config=_config(),
    )
    assert equity.unbuffered_delta is None


def test_iv_source_falls_back_when_any_leg_falls_back() -> None:
    """Two-leg strategy where one leg's strike is in the surface and the other
    is outside the chain (forcing realized-vol fallback): aggregate
    ``iv_source`` is ``REALIZED_VOL_FALLBACK``."""
    # Surface only covers strike 100 calls; the 200 call falls back to RV.
    provider = FixtureIvProvider(
        surface={
            "AAPL": IvSurfaceEntry(
                underlying=Symbol("AAPL"),
                quotes=(
                    IvQuote(
                        strike=100.0,
                        expiration=_EXPIRATION,
                        contract_type=ContractType.CALL,
                        implied_volatility=_IV,
                    ),
                ),
            ),
        },
        realized_vol={
            "AAPL": RealizedVolEntry(underlying=Symbol("AAPL"), trailing_30d_realized_vol=0.25)
        },
    )
    proposal = ProposedDelta(
        id="proposal-mixed",
        underlying=Symbol("AAPL"),
        sector="Technology",
        direction=Direction.LONG,
        asset_type=AssetType.STRATEGY,
        notional_usd=money(200.0),
        quantity=1.0,
        option_legs=(
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=100.0,
                expiration=_EXPIRATION,
                quantity=1,
            ),
            OptionLeg(
                contract_type=ContractType.CALL,
                strike=200.0,
                expiration=_EXPIRATION,
                quantity=-1,
            ),
        ),
        action=Action.OPEN,
        existing_position_id=None,
    )

    result = compute_delta_adjusted_exposure(
        proposal=proposal, market=_market(iv_provider=provider), config=_config()
    )

    assert result.iv_source is IvSource.REALIZED_VOL_FALLBACK


def test_quantity_scales_signed_notional_linearly() -> None:
    """``quantity=N`` strategies multiplies the per-strategy notional by N."""
    base = compute_delta_adjusted_exposure(
        proposal=_single_call(quantity=1.0, direction=Direction.LONG),
        market=_market(),
        config=_config(),
    )
    five = compute_delta_adjusted_exposure(
        proposal=_single_call(quantity=5.0, direction=Direction.LONG),
        market=_market(),
        config=_config(),
    )

    assert five.signed_notional_usd == pytest.approx(5.0 * base.signed_notional_usd, rel=1e-12)


def test_compute_delta_adjusted_exposure_is_pure() -> None:
    """Two calls with equal inputs produce equal outputs (no hidden state)."""
    proposal = _single_call(quantity=3.0, direction=Direction.LONG)
    market = _market()
    config = _config()

    first = compute_delta_adjusted_exposure(proposal=proposal, market=market, config=config)
    second = compute_delta_adjusted_exposure(proposal=proposal, market=market, config=config)

    assert first == second


def test_unknown_regime_label_defaults_to_1x_multiplier_without_raising() -> None:
    """An unknown regime label (e.g., ``"made_up"``) defaults to a ``1.0``
    multiplier — out-of-band labels are upstream contract violations the
    math layer treats defensively."""
    proposal = _single_call(quantity=1.0, direction=Direction.LONG)

    # Same buffer outcome as ``regime=normal`` (which has multiplier 1.0).
    made_up = compute_delta_adjusted_exposure(
        proposal=proposal,
        market=_market(),
        config=_config(active_regime="made_up", conservative_buffer_pct=10.0),
    )
    normal = compute_delta_adjusted_exposure(
        proposal=proposal,
        market=_market(),
        config=_config(active_regime="normal", conservative_buffer_pct=10.0),
    )

    assert made_up.signed_notional_usd == pytest.approx(normal.signed_notional_usd, rel=1e-12)


def test_short_call_leg_with_long_direction_uses_proposal_direction_for_sign() -> None:
    """A LONG-direction strategy whose only leg is a short call has
    net_delta = -bs_delta(call). The buffer applies to |net_delta|; the sign
    of ``signed_notional_usd`` is set by ``proposal.direction``, not the sign
    of ``net_delta``."""
    proposal = _single_call(quantity=1.0, direction=Direction.LONG, leg_quantity=-1)

    result = compute_delta_adjusted_exposure(proposal=proposal, market=_market(), config=_config())

    bs = bs_greeks(
        spot=_SPOT,
        strike=_STRIKE,
        time_to_expiration_years=_TIME_TO_EXPIRATION_YEARS,
        risk_free_rate=_RISK_FREE_RATE,
        implied_volatility=_IV,
        contract_type=ContractType.CALL,
    )

    assert result.net_greeks is not None
    # ``-1 * bs_delta`` (the leg quantity is -1).
    assert result.net_greeks.delta == pytest.approx(-bs.delta, rel=1e-12)
    # Buffered absolute, signed by the proposal's LONG direction.
    expected_signed = abs(bs.delta) * 1.10 * _SPOT * _CONTRACT_MULTIPLIER * 1.0
    assert result.signed_notional_usd == pytest.approx(expected_signed, rel=1e-9)
    assert result.signed_notional_usd > 0
