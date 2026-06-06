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

import alphamind.state.invocation_context  # noqa: F401 — break state.repository circular import
from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.money import Money, Price, money, price
from alphamind.config.models.execution import FeeSchedule, OrderType, PaperHarness
from alphamind.decision.strategist.models import PositionAssessment
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.iv_lookup import IVSnapshotLookupResult
from alphamind.execution.counterfactual_replay_engine.option_replay import (
    price_option_at_underlying_bar,
)
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.execution.counterfactual_replay_engine.strategist_replay import (
    StrategistActionResult,
    replay_strategist_proposal,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
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


def _bs_premium(*, underlying: float, ts: datetime, strike: float = 100.0) -> Price:
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

        assert result.realized_pl is None
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
