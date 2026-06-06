"""Tests for strategist position-action replay (ALP-563, story 07 §2-4).

Covers the four position-action simulators (CLOSE / REDUCE / ADJUST-BRACKET /
ADD) across equity and single-leg option positions, plus the pending-order
CANCEL / MODIFY surface and the ``replay_strategist_proposal`` dispatcher.

The paper harness's :func:`compute_live_execution_estimate` is pure compute, so
it is exercised for real (a real :class:`PaperHarness` config plus ADV / vol),
not mocked. P/L assertions are stated against the harness's own drag output
rather than hard-coded numbers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import pytest

import alphamind.state.invocation_context  # noqa: F401 — break state.repository circular import
from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol
from alphamind._kernel.money import Money, money, price
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.decision.analyst.models import Recommendation
from alphamind.decision.strategist.models import PendingOrderAssessment, PositionAssessment
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg, UnevaluableReason
from alphamind.execution.counterfactual_replay_engine.iv_lookup import IVSnapshotLookupResult
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.execution.counterfactual_replay_engine.strategist_replay import (
    PendingOrderReplayResult,
    StrategistActionResult,
    reconstruct_pending_order_entry,
    replay_pending_order_proposal,
    replay_strategist_proposal,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.orders import (
    OrderType as OrderTypeRecord,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
)
from alphamind.state.repository.position_state import PositionStateSnapshot

_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
_NEXT_BAR_TS = datetime(2026, 6, 1, 14, 15, tzinfo=UTC)
_OPENED_AT = datetime(2026, 5, 28, 14, 0, tzinfo=UTC)
_ADV = 5_000_000.0
_RVOL = 0.30
_RFR = 0.04

_CONFIG = PaperHarness(
    spread_buffer_pct=0.1,
    impact_coefficients={OrderType.market: 0.5, OrderType.limit: 0.3, OrderType.stop: 0.4},
    fee_schedule=FeeSchedule(
        cat_per_executed_share=0.0001,
        taf_per_share_sells=0.000166,
        sec_pct_of_notional_sells=0.0000278,
        orf_per_options_contract=0.02685,
        occ_per_options_contract=0.02,
    ),
)


# ---------------------------------------------------------------------------
# Fakes / builders
# ---------------------------------------------------------------------------


class _FakeIVRepo:
    """In-memory ``OptionsSnapshotRepository`` returning a fixed IV at any ts."""

    def __init__(self, iv: float | None = 0.35) -> None:
        self._iv = iv

    def resolve_contract_ticker(
        self,
        *,
        underlying: str,
        strike: Decimal,
        expiration: date,
        contract_type: Literal["call", "put"],
    ) -> str:
        return f"O:{underlying}-{contract_type}"

    def lookup_iv(
        self,
        *,
        contract_ticker: str,
        target_ts: datetime,
    ) -> IVSnapshotLookupResult | None:
        if self._iv is None:
            return None
        return IVSnapshotLookupResult(
            snapshot_ts=target_ts,
            implied_volatility=self._iv,
            underlying_price_at_snapshot=None,
            lag_minutes=0.0,
        )

    def has_snapshot_at_or_before(self, contract_ticker: str, when: datetime) -> bool:
        return self._iv is not None


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float) -> OhlcvBar:
    return OhlcvBar(
        period_start=ts,
        period_end=ts + timedelta(minutes=15),
        open=o,
        high=h,
        low=low,
        close=c,
        adj_volume=1_000_000,
    )


def _flat_bars(open_price: float, n: int = 6) -> tuple[OhlcvBar, ...]:
    """``n`` bars from the proposal bar forward, all flat at *open_price*."""
    return tuple(
        _bar(
            _PROPOSAL_TS + timedelta(minutes=15 * i),
            o=open_price,
            h=open_price,
            low=open_price,
            c=open_price,
        )
        for i in range(n)
    )


def _bars_hitting_high(*, base: float, spike_high: float) -> tuple[OhlcvBar, ...]:
    """Flat bars at *base*, with the third bar spiking up to *spike_high*."""
    bars = list(_flat_bars(base, n=4))
    spike = bars[2]
    bars[2] = _bar(spike.period_start, o=base, h=spike_high, low=base, c=base)
    return tuple(bars)


def _bars_hitting_low(*, base: float, spike_low: float) -> tuple[OhlcvBar, ...]:
    """Flat bars at *base*, with the third bar dipping down to *spike_low*."""
    bars = list(_flat_bars(base, n=4))
    spike = bars[2]
    bars[2] = _bar(spike.period_start, o=base, h=base, low=spike_low, c=base)
    return tuple(bars)


def _equity_snapshot(
    *,
    direction: Direction = Direction.LONG,
    net_qty: float = 50.0,
    basis: float = 100.0,
) -> PositionStateSnapshot:
    return PositionStateSnapshot(
        position_id=PositionId("POS-1"),
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=abs(net_qty),
            average_cost_basis_per_share=basis,
        ),
        direction=direction,
        net_quantity_as_of=net_qty if direction is Direction.LONG else -abs(net_qty),
        average_cost_basis=price(str(basis)),
        open_brackets=(),
        opened_at=_OPENED_AT,
    )


def _option_snapshot(
    *,
    direction: Direction = Direction.LONG,
    net_qty: float = 4.0,
    premium: float = 5.0,
) -> PositionStateSnapshot:
    return PositionStateSnapshot(
        position_id=PositionId("POS-OPT"),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("AAPL"),
            strike_price=100.0,
            expiration_date=date(2026, 7, 17),
            contract_type=OptionContractType.CALL,
            contract_count=abs(net_qty),
            contract_multiplier=100.0,
            premium_paid_per_contract=premium,
            greeks=OptionGreeks(delta=0.5, gamma=0.1, theta=-0.1, vega=0.2),
        ),
        direction=direction,
        net_quantity_as_of=net_qty if direction is Direction.LONG else -abs(net_qty),
        average_cost_basis=price(str(premium)),
        open_brackets=(),
        opened_at=_OPENED_AT,
    )


def _close_assessment(*, quantity: float | str = "all") -> PositionAssessment:
    return _position_assessment(
        action="close",
        params={
            "action": "close",
            "quantity": quantity,
            "order_type": "market",
            "close_rationale_type": "risk_management",
        },
        extra={"exposure_impact": _EXPOSURE},
    )


_EXPOSURE = {"sector_delta_adjusted_change": "-5000.00", "net_directional_impact": "-5000.00"}


def _position_assessment(
    *,
    action: str,
    params: dict[str, Any],
    extra: dict[str, Any],
) -> PositionAssessment:
    body: dict[str, Any] = {
        "assessment_id": "SA-1",
        "position_id": "POS-1",
        "thesis_id": "THS-1",
        "underlying": "AAPL",
        "sector": "tech",
        "thesis_status": "at-risk",
        "recommended_action": action,
        "action_parameters": params,
        "status_rationale": "Status.",
        "action_rationale": "Action.",
        **extra,
    }
    return PositionAssessment.model_validate(body)


def _bs_premium(*, underlying: float, ts: datetime, strike: float = 100.0) -> Money:
    """The exit premium the close path should derive for the test option."""
    return price_option_at_underlying_bar(
        underlying_open=underlying,
        strike=Decimal(str(strike)),
        expiration=date(2026, 7, 17),
        contract_type="call",
        bar_timestamp=ts,
        implied_volatility=0.35,
        risk_free_rate=_RFR,
    )


def _drag(
    *,
    fill_price: float,
    side: Literal["buy", "sell"],
    order_type: OrderType,
    quantity: float,
    instrument_type: InstrumentType,
    adv: float | None = _ADV,
) -> tuple[Money, Money]:
    est = compute_live_execution_estimate(
        fill_price=price(str(fill_price)),
        fill_quantity=quantity,
        instrument_type=instrument_type,
        side=side,
        order_type=order_type,
        adv_shares=adv,
        realized_volatility=_RVOL,
        config=_CONFIG,
    )
    assert est is not None
    slippage = money(est.estimated_spread_usd + est.estimated_impact_usd)
    return slippage, est.estimated_regulatory_fees_usd


# ---------------------------------------------------------------------------
# CLOSE — equity
# ---------------------------------------------------------------------------


class TestCloseEquity:
    def test_long_close_all_profit(self) -> None:
        # avg_cost 100, qty 50 long; next bar opens 110 → gross +500 minus close drag.
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _flat_bars(110.0)
        proposal = _close_assessment(quantity="all")

        result = replay_strategist_proposal(
            proposal,
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert isinstance(result, StrategistActionResult)
        assert result.entered is True
        assert result.exit_leg is ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL
        assert result.exit_price == price("110")
        assert result.exit_timestamp == _NEXT_BAR_TS

        exit_slip, exit_fee = _drag(
            fill_price=110.0,
            side="sell",
            order_type=OrderType.market,
            quantity=50.0,
            instrument_type=InstrumentType.EQUITY,
        )
        gross = Decimal(110) - Decimal(100)
        expected = gross * Decimal(50) - Decimal(exit_slip) - Decimal(exit_fee)
        assert result.realized_pl == Money(expected)
        # Only the close side carries drag — the position's entry already happened.
        assert result.entry_slippage == money("0")
        assert result.entry_fees == money("0")
        assert result.entry_price == price("100")
        assert result.entry_timestamp == _OPENED_AT

    def test_long_close_all_loss_is_signed_negative(self) -> None:
        # avg_cost 100, qty 50 long; next bar opens 90 → gross -500 (minus drag).
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _flat_bars(90.0)

        result = replay_strategist_proposal(
            _close_assessment(quantity="all"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.realized_pl is not None
        assert Decimal(result.realized_pl) < 0
        exit_slip, exit_fee = _drag(
            fill_price=90.0,
            side="sell",
            order_type=OrderType.market,
            quantity=50.0,
            instrument_type=InstrumentType.EQUITY,
        )
        expected = (
            (Decimal(90) - Decimal(100)) * Decimal(50) - Decimal(exit_slip) - Decimal(exit_fee)
        )
        assert result.realized_pl == Money(expected)

    def test_short_close_profit_uses_negative_direction_sign(self) -> None:
        # Short avg_cost 100; next bar opens 90 → short gains +1000 minus drag.
        snap = _equity_snapshot(direction=Direction.SHORT, net_qty=100.0, basis=100.0)
        bars = _flat_bars(90.0)

        result = replay_strategist_proposal(
            _close_assessment(quantity="all"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL
        exit_slip, exit_fee = _drag(
            fill_price=90.0,
            side="buy",
            order_type=OrderType.market,
            quantity=100.0,
            instrument_type=InstrumentType.EQUITY,
        )
        gross = (Decimal(90) - Decimal(100)) * Decimal(100) * Decimal(-1)
        assert result.realized_pl == Money(gross - Decimal(exit_slip) - Decimal(exit_fee))

    def test_partial_close_uses_explicit_quantity(self) -> None:
        # quantity=20 (absolute) of a 50-share long position.
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _flat_bars(110.0)

        result = replay_strategist_proposal(
            _close_assessment(quantity=20.0),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        exit_slip, exit_fee = _drag(
            fill_price=110.0,
            side="sell",
            order_type=OrderType.market,
            quantity=20.0,
            instrument_type=InstrumentType.EQUITY,
        )
        gross = (Decimal(110) - Decimal(100)) * Decimal(20)
        assert result.realized_pl == Money(gross - Decimal(exit_slip) - Decimal(exit_fee))


class TestCloseOption:
    def test_long_option_close_bs_priced_with_multiplier(self) -> None:
        # ITM call: underlying 120, strike 100, premium basis 5 → BS premium well
        # above 5 → profit, scaled by the 100x contract multiplier.
        snap = _option_snapshot(direction=Direction.LONG, net_qty=4.0, premium=5.0)
        bars = _flat_bars(120.0)
        iv_repo = _FakeIVRepo(iv=0.35)

        result = replay_strategist_proposal(
            _close_assessment(quantity="all"),
            snap,
            bars,
            iv_repo=iv_repo,
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL
        assert result.exit_price is not None
        bs_premium = _bs_premium(underlying=120.0, ts=_NEXT_BAR_TS)
        assert result.exit_price == bs_premium
        exit_slip, exit_fee = _drag(
            fill_price=float(bs_premium),
            side="sell",
            order_type=OrderType.market,
            quantity=4.0,
            instrument_type=InstrumentType.OPTIONS,
        )
        gross = (Decimal(bs_premium) - Decimal(5)) * Decimal(4) * Decimal(100)
        assert result.realized_pl == Money(gross - Decimal(exit_slip) - Decimal(exit_fee))

    def test_option_close_missing_iv_is_data_missing_sentinel(self) -> None:
        snap = _option_snapshot()
        bars = _flat_bars(120.0)
        iv_repo = _FakeIVRepo(iv=None)

        result = replay_strategist_proposal(
            _close_assessment(quantity="all"),
            snap,
            bars,
            iv_repo=iv_repo,
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        # The IV-miss sentinel is explicitly flagged and carries no fabricated
        # outcome: no entered=True, no exit leg, no exit premium.
        assert result.data_missing is True
        assert result.realized_pl is None
        assert result.entered is False
        assert result.exit_leg is None
        assert result.entry_price is None
        assert result.exit_price is None


class TestReduceEquity:
    def test_reduce_uses_absolute_quantity_not_percentage(self) -> None:
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _flat_bars(110.0)
        proposal = _position_assessment(
            action="reduce",
            params={"action": "reduce", "quantity": 15.0, "order_type": "market"},
            extra={"exposure_impact": _EXPOSURE, "reduce_rationale": "Trim."},
        )

        result = replay_strategist_proposal(
            proposal,
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STRATEGIST_CLOSE_AT_PROPOSAL
        exit_slip, exit_fee = _drag(
            fill_price=110.0,
            side="sell",
            order_type=OrderType.market,
            quantity=15.0,
            instrument_type=InstrumentType.EQUITY,
        )
        gross = (Decimal(110) - Decimal(100)) * Decimal(15)
        assert result.realized_pl == Money(gross - Decimal(exit_slip) - Decimal(exit_fee))


def _adjust_bracket_assessment(
    *,
    new_target: str | None = None,
    new_stop: str | None = None,
    thesis_only: bool = False,
) -> PositionAssessment:
    params: dict[str, Any] = {"action": "adjust-bracket"}
    if new_target is not None:
        params["new_target_level"] = {"price": new_target, "order_type": "limit"}
    if new_stop is not None:
        params["new_stop_level"] = {"trigger_price": new_stop, "order_type": "stop"}
    if thesis_only:
        # A thesis/event-only adjust: no price/stop/time level — the schema's
        # "at least one" requirement is met by a thesis-component update.
        params["thesis_component_updates"] = [
            {"component_type": "invalidation_rationale", "narrative": "Thesis softened."}
        ]
    return _position_assessment(
        action="adjust-bracket",
        params=params,
        extra={"adjustment_rationale": "Tighten."},
    )


class TestAdjustBracketEquity:
    def test_target_hit_long(self) -> None:
        # Long position; proposed target 115 is hit on the spike bar.
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _bars_hitting_high(base=105.0, spike_high=116.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(new_target="115.00", new_stop="95.00"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.entered is True
        assert result.exit_leg is ExitLeg.TARGET_HIT
        assert result.exit_price == price("115")
        # Only the exit side carries drag (the position was already open).
        assert result.entry_slippage == money("0")
        exit_slip, exit_fee = _drag(
            fill_price=115.0,
            side="sell",
            order_type=OrderType.limit,
            quantity=50.0,
            instrument_type=InstrumentType.EQUITY,
        )
        gross = (Decimal(115) - Decimal(100)) * Decimal(50)
        assert result.realized_pl == Money(gross - Decimal(exit_slip) - Decimal(exit_fee))

    def test_stop_hit_long(self) -> None:
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _bars_hitting_low(base=98.0, spike_low=90.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(new_target="130.00", new_stop="95.00"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STOP_HIT
        assert result.exit_price == price("95")
        assert result.realized_pl is not None
        assert Decimal(result.realized_pl) < 0


class TestAdjustBracketOption:
    def test_stop_hit_long_option_bs_priced(self) -> None:
        snap = _option_snapshot(direction=Direction.LONG, net_qty=4.0, premium=5.0)
        bars = _bars_hitting_low(base=98.0, spike_low=88.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(new_target="130.00", new_stop="90.00"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(iv=0.35),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STOP_HIT
        # Exit premium BS-priced at the stop's underlying level (90).
        assert result.exit_price is not None
        assert result.exit_price == _bs_premium(underlying=90.0, ts=bars[2].period_start)


def _stop_only_bracket(
    *, threshold_usd: float, direction: Literal["GTE", "LTE"] = "LTE"
) -> BracketRecord:
    """A bracket carrying only a mechanical PRICE_STOP leg — no TAKE_PROFIT."""
    return BracketRecord(
        bracket_id=BracketId("BR-1"),
        position_id=PositionId("POS-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ORD-1"),
        protective_legs=(
            BracketLeg(
                leg_id="L1",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"),
                    threshold_usd=threshold_usd,
                    direction=direction,
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
        ),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


class TestAdjustBracketStopOnly:
    def test_targetless_adjust_on_stop_only_position_is_evaluable(self) -> None:
        # Thesis/event-only ADJUST-BRACKET: no new_target_level, and the position
        # carries only a PRICE_STOP leg (no TAKE_PROFIT). Per the spec it must
        # still be evaluable — the walk runs on the stop / window only — not crash
        # on a missing target. The stop at 95 fires when the bar dips to 90.
        snap = PositionStateSnapshot(
            position_id=PositionId("POS-1"),
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"), share_count=50.0, average_cost_basis_per_share=100.0
            ),
            direction=Direction.LONG,
            net_quantity_as_of=50.0,
            average_cost_basis=price("100"),
            open_brackets=(_stop_only_bracket(threshold_usd=95.0),),
            opened_at=_OPENED_AT,
        )
        bars = _bars_hitting_low(base=98.0, spike_low=90.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(thesis_only=True),  # no new target, no new stop
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.STOP_HIT
        assert result.exit_price == money("95")

    def test_targetless_stopless_adjust_resolves_at_window_end(self) -> None:
        # Neither a target nor any stop leg: the only resolution is the window
        # end (the synthetic far-out time leg). The walk must reach a TIME_STOP
        # result rather than raising on the absent target.
        snap = PositionStateSnapshot(
            position_id=PositionId("POS-1"),
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"), share_count=50.0, average_cost_basis_per_share=100.0
            ),
            direction=Direction.LONG,
            net_quantity_as_of=50.0,
            average_cost_basis=price("100"),
            open_brackets=(),
            opened_at=_OPENED_AT,
        )
        bars = _flat_bars(105.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(thesis_only=True),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.TIME_STOP_FIRED
        assert result.entered is True


class TestNaiveOpenedAt:
    def test_adjust_bracket_with_naive_opened_at_does_not_crash(self) -> None:
        # PositionStateSnapshot.opened_at carries no tz guarantee. A naive
        # opened_at flows into GuardrailValidationResult.checked_at (and the
        # synthetic far-out time leg), which require tz-aware UTC — so the
        # synthesis must coerce it rather than raise a ValidationError.
        naive_opened_at = datetime(2026, 5, 28, 14, 0)  # noqa: DTZ001 — tz-naive on purpose
        snap = PositionStateSnapshot(
            position_id=PositionId("POS-1"),
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"), share_count=50.0, average_cost_basis_per_share=100.0
            ),
            direction=Direction.LONG,
            net_quantity_as_of=50.0,
            average_cost_basis=price("100"),
            open_brackets=(),
            opened_at=naive_opened_at,
        )
        bars = _bars_hitting_high(base=105.0, spike_high=116.0)

        result = replay_strategist_proposal(
            _adjust_bracket_assessment(new_target="115.00", new_stop="95.00"),
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.TARGET_HIT
        assert result.exit_price == money("115")

    def test_add_with_naive_opened_at_does_not_crash(self) -> None:
        naive_opened_at = datetime(2026, 5, 28, 14, 0)  # noqa: DTZ001 — tz-naive on purpose
        snap = PositionStateSnapshot(
            position_id=PositionId("POS-1"),
            details=EquityPositionDetails(
                ticker=Symbol("AAPL"), share_count=50.0, average_cost_basis_per_share=100.0
            ),
            direction=Direction.LONG,
            net_quantity_as_of=50.0,
            average_cost_basis=price("100"),
            open_brackets=(_stop_only_bracket(threshold_usd=95.0),),
            opened_at=naive_opened_at,
        )
        bars = _bars_hitting_high(base=105.0, spike_high=130.0)
        add = _add_assessment(
            additional_quantity=10.0,
            entry_order={"type": "market"},
            bracket_adjustment={
                "action": "adjust-bracket",
                "new_target_level": {"price": "120.00", "order_type": "limit"},
            },
        )

        result = replay_strategist_proposal(
            add,
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.exit_leg is ExitLeg.TARGET_HIT
        assert result.entered is True


def _add_assessment(
    *,
    additional_quantity: float,
    entry_order: dict[str, Any],
    bracket_adjustment: dict[str, Any] | None = None,
) -> PositionAssessment:
    params: dict[str, Any] = {
        "action": "add",
        "additional_quantity": additional_quantity,
        "additional_dollar_value": "1000.00",
        "entry_order": entry_order,
    }
    if bracket_adjustment is not None:
        params["bracket_adjustment"] = bracket_adjustment
    return _position_assessment(
        action="add",
        params=params,
        extra={
            "exposure_impact": _EXPOSURE,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [],
                "checked_at": "2026-06-01T14:00:00+00:00",
            },
            "add_conviction_justification": "Conviction up.",
        },
    )


class TestAddEquity:
    def test_add_fills_then_target_hit(self) -> None:
        # Market add fills at the next bar open (105); target 115 hit on the spike.
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _bars_hitting_high(base=105.0, spike_high=116.0)
        proposal = _add_assessment(
            additional_quantity=10.0,
            entry_order={"type": "market"},
            bracket_adjustment={
                "action": "adjust-bracket",
                "new_target_level": {"price": "115.00", "order_type": "limit"},
                "new_stop_level": {"trigger_price": "95.00", "order_type": "stop"},
            },
        )

        result = replay_strategist_proposal(
            proposal,
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.entered is True
        assert result.exit_leg is ExitLeg.TARGET_HIT
        assert result.exit_price == price("115")
        # ADD is a fresh fill — both entry and exit drag apply, on 10 shares only.
        entry_slip, entry_fee = _drag(
            fill_price=105.0,
            side="buy",
            order_type=OrderType.market,
            quantity=10.0,
            instrument_type=InstrumentType.EQUITY,
        )
        exit_slip, exit_fee = _drag(
            fill_price=115.0,
            side="sell",
            order_type=OrderType.limit,
            quantity=10.0,
            instrument_type=InstrumentType.EQUITY,
        )
        assert result.entry_slippage == entry_slip
        assert result.entry_fees == entry_fee
        gross = (Decimal(115) - Decimal(105)) * Decimal(10)
        expected = (
            gross
            - Decimal(entry_slip)
            - Decimal(entry_fee)
            - Decimal(exit_slip)
            - Decimal(exit_fee)
        )
        assert result.realized_pl == Money(expected)

    def test_add_entry_window_expired_unfilled(self) -> None:
        # Limit add at 90; bars never trade down to 90 → never fills.
        snap = _equity_snapshot(direction=Direction.LONG, net_qty=50.0, basis=100.0)
        bars = _flat_bars(110.0)
        proposal = _add_assessment(
            additional_quantity=10.0,
            entry_order={"type": "limit", "limit_price": "90.00"},
            bracket_adjustment={
                "action": "adjust-bracket",
                "new_target_level": {"price": "120.00", "order_type": "limit"},
            },
        )

        result = replay_strategist_proposal(
            proposal,
            snap,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.entered is False
        assert result.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED
        assert result.realized_pl is None
        assert result.entry_price is None
        assert result.exit_price is None


class TestAddOption:
    def test_option_add_fills_then_target_hit(self) -> None:
        snap = _option_snapshot(direction=Direction.LONG, net_qty=4.0, premium=5.0)
        bars = _bars_hitting_high(base=105.0, spike_high=130.0)
        proposal = _add_assessment(
            additional_quantity=2.0,
            entry_order={"type": "market"},
            bracket_adjustment={
                "action": "adjust-bracket",
                "new_target_level": {"price": "125.00", "order_type": "limit"},
                "new_stop_level": {"trigger_price": "95.00", "order_type": "stop"},
            },
        )

        result = replay_strategist_proposal(
            proposal,
            snap,
            bars,
            iv_repo=_FakeIVRepo(iv=0.35),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.entered is True
        assert result.exit_leg is ExitLeg.TARGET_HIT
        assert result.exit_price is not None
        # Both entry and exit premium are BS-derived on the added 2 contracts.
        assert result.entry_price == _bs_premium(underlying=105.0, ts=_NEXT_BAR_TS)
        assert result.realized_pl is not None


class TestDispatcher:
    def test_hold_action_raises(self) -> None:
        # A hold proposal carries no action_parameters and is eligibility-filtered
        # upstream; reaching the dispatcher is an internal contract violation.
        snap = _equity_snapshot()
        bars = _flat_bars(100.0)
        proposal = PositionAssessment.model_validate(
            {
                "assessment_id": "SA-9",
                "position_id": "POS-1",
                "thesis_id": "THS-1",
                "underlying": "AAPL",
                "sector": "tech",
                "thesis_status": "on-track",
                "recommended_action": "hold",
                "status_rationale": "Hold.",
                "action_rationale": "Hold.",
            }
        )

        with pytest.raises(ValueError, match=r"unsupported recommended_action"):
            replay_strategist_proposal(
                proposal,
                snap,
                bars,
                iv_repo=_FakeIVRepo(),
                paper_harness_config=_CONFIG,
                risk_free_rate=_RFR,
                adv=_ADV,
                realized_volatility=_RVOL,
            )


def _pending_assessment(*, action: str) -> PendingOrderAssessment:
    body: dict[str, Any] = {
        "pending_order_assessment_id": "SA-ORD-1",
        "order_id": "ORD-1",
        "position_id": "POS-1",
        "order_type": "entry_limit",
        "order_age_hours": 3.0,
        "fill_probability_assessment": "plausible",
        "recommended_action": action,
        "drift_rationale": "Drift.",
        "action_rationale": "Action.",
    }
    if action == "modify":
        body["modification_parameters"] = {"new_limit_price": "99.00"}
    return PendingOrderAssessment.model_validate(body)


def _entry_recommendation(*, limit_price: str, quantity: float = 10.0) -> Recommendation:
    return Recommendation.model_validate(
        {
            "recommendation_id": "REC-7",
            "instrument": {"asset_type": "equity", "ticker": "AAPL", "direction": "long"},
            "underlying": "AAPL",
            "sector": "tech",
            "conviction_level": 3,
            "entry_order": {"type": "limit", "limit_price": limit_price},
            "position_size": {
                "quantity": quantity,
                "dollar_value": "1000.00",
                "pct_of_portfolio": 0.05,
            },
            "target": {
                "target_type": "absolute_price",
                "price": "120.00",
                "dollar_pl_target": "200.00",
            },
            "invalidation_legs": [
                {
                    "leg_id": "INV-1",
                    "type": "price",
                    "is_hard": True,
                    "condition": {
                        "underlying_trigger": "AAPL",
                        "comparator": "<=",
                        "trigger_price": "80.00",
                    },
                    "order_parameters": {"order_type": "stop"},
                }
            ],
            "time_expectation_hours": 8.0,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [],
                "checked_at": "2026-06-01T14:00:00+00:00",
            },
            "thesis_narrative": "Test.",
            "target_rationale": "Test.",
            "invalidation_rationale": [{"leg_id": "INV-1", "rationale": "Support."}],
            "position_size_rationale": "Standard.",
            "counterarguments_acknowledged": "Acknowledged.",
        }
    )


class TestPendingOrderCancel:
    def test_cancel_replays_entry_and_brackets_when_it_would_fill(self) -> None:
        # Limit entry at 100; bars trade down to fill, then target 120 is hit.
        proposal = _pending_assessment(action="cancel")
        entry = _entry_recommendation(limit_price="100.00", quantity=10.0)
        bars = _bars_hitting_high(base=100.0, spike_high=121.0)

        result = replay_pending_order_proposal(
            proposal,
            entry,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert isinstance(result, PendingOrderReplayResult)
        assert result.unevaluable_reason is None
        assert result.action_result is not None
        assert result.action_result.entered is True
        assert result.action_result.exit_leg is ExitLeg.TARGET_HIT

    def test_cancel_entry_never_fills(self) -> None:
        proposal = _pending_assessment(action="cancel")
        entry = _entry_recommendation(limit_price="80.00", quantity=10.0)
        bars = _flat_bars(110.0)

        result = replay_pending_order_proposal(
            proposal,
            entry,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.unevaluable_reason is None
        assert result.action_result is not None
        assert result.action_result.entered is False
        assert result.action_result.exit_leg is ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED


class TestPendingOrderModify:
    def test_modify_is_unevaluable(self) -> None:
        proposal = _pending_assessment(action="modify")
        entry = _entry_recommendation(limit_price="100.00")
        bars = _flat_bars(110.0)

        result = replay_pending_order_proposal(
            proposal,
            entry,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )

        assert result.action_result is None
        assert (
            result.unevaluable_reason is UnevaluableReason.STRATEGIST_POSITION_ACTION_NOT_SUPPORTED
        )


# ---------------------------------------------------------------------------
# reconstruct_pending_order_entry — order record + as-of state → entry Recommendation
# ---------------------------------------------------------------------------


def _entry_order_record(
    *,
    order_type: OrderTypeRecord,
    limit_price: str | None = None,
    stop_trigger_price: str | None = None,
    quantity: float = 10.0,
) -> OrderRecord:
    from alphamind.portfolio_state.records.orders import EquityInstrumentSpec

    return OrderRecord(
        order_id=OrderId("ORD-1"),
        position_id=PositionId("POS-1"),
        bracket_id=BracketId("BRK-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=order_type,
        price_parameters=PriceParameters(
            limit_price=price(limit_price) if limit_price is not None else None,
            stop_trigger_price=price(stop_trigger_price)
            if stop_trigger_price is not None
            else None,
        ),
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=None,
        alpaca_order_id_chain=(),
        submission_timestamp=_OPENED_AT,
        last_update_timestamp=_OPENED_AT,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=4.0,
    )


def _snapshot_with_brackets(target: float, stop: float) -> PositionStateSnapshot:
    bracket = BracketRecord(
        bracket_id=BracketId("BRK-1"),
        position_id=PositionId("POS-1"),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ORD-1"),
        protective_legs=(
            BracketLeg(
                leg_id="L-TP",
                leg_type=BracketLegType.TAKE_PROFIT,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"), threshold_usd=target, direction="GTE"
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
            BracketLeg(
                leg_id="L-SL",
                leg_type=BracketLegType.PRICE_STOP,
                order_id=None,
                trigger=PriceTrigger(
                    underlying_ticker=Symbol("AAPL"), threshold_usd=stop, direction="LTE"
                ),
                enforcement=BracketLegEnforcement.MECHANICAL,
                status=BracketLegStatus.ACTIVE,
            ),
        ),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )
    return PositionStateSnapshot(
        position_id=PositionId("POS-1"),
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=10.0,
            average_cost_basis_per_share=100.0,
        ),
        direction=Direction.LONG,
        net_quantity_as_of=10.0,
        average_cost_basis=price("100.00"),
        open_brackets=(bracket,),
        opened_at=_OPENED_AT,
    )


class TestReconstructPendingOrderEntry:
    def test_limit_order_maps_entry_order_and_bracket_levels(self) -> None:
        order = _entry_order_record(
            order_type=OrderTypeRecord.LIMIT, limit_price="100.00", quantity=10.0
        )
        state = _snapshot_with_brackets(target=120.0, stop=90.0)

        entry = reconstruct_pending_order_entry(order, state)

        assert isinstance(entry, Recommendation)
        assert entry.entry_order.type == "limit"
        assert entry.entry_order.limit_price == price("100.00")
        assert entry.position_size.quantity == 10.0
        # The bracket's target/stop become the entry's target + price-stop leg.
        assert entry.target.price == price("120.00")
        assert any(leg.type == "price" for leg in entry.invalidation_legs)

    def test_reconstructed_entry_replays_through_cancel_path(self) -> None:
        # A reconstructed entry is exactly what replay_pending_order_proposal expects.
        order = _entry_order_record(
            order_type=OrderTypeRecord.LIMIT, limit_price="100.00", quantity=10.0
        )
        state = _snapshot_with_brackets(target=120.0, stop=90.0)
        entry = reconstruct_pending_order_entry(order, state)

        proposal = _pending_assessment(action="cancel")
        bars = _bars_hitting_high(base=100.0, spike_high=121.0)
        result = replay_pending_order_proposal(
            proposal,
            entry,
            bars,
            iv_repo=_FakeIVRepo(),
            paper_harness_config=_CONFIG,
            risk_free_rate=_RFR,
            adv=_ADV,
            realized_volatility=_RVOL,
        )
        assert result.action_result is not None
        assert result.action_result.entered is True
        assert result.action_result.exit_leg is ExitLeg.TARGET_HIT

    def test_stop_limit_order_maps_stop_and_limit(self) -> None:
        order = _entry_order_record(
            order_type=OrderTypeRecord.STOP_LIMIT,
            limit_price="101.00",
            stop_trigger_price="100.50",
        )
        state = _snapshot_with_brackets(target=120.0, stop=90.0)
        entry = reconstruct_pending_order_entry(order, state)
        assert entry.entry_order.type == "stop_limit"
        assert entry.entry_order.limit_price == price("101.00")
        assert entry.entry_order.stop_price == price("100.50")
