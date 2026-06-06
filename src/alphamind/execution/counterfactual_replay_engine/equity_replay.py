"""Per-proposal equity replay primitives (ALP-559, design Steps 2-4).

Pure simulation over a hydrated analyst :class:`Recommendation` with an equity
instrument: entry against the underlying bar stream, bracket-trigger walking,
and P/L composition with paper-harness slippage / fees. The engine driver
(story 08) supplies the bar sequence, the paper-harness config, and the
ADV / realized-volatility lookups, then folds :class:`EquityReplayResult` into
a ``CounterfactualReplayRecord``.

Slippage and fees reuse the paper harness's
:func:`~alphamind.execution.paper_evaluation_harness.harness.compute_live_execution_estimate`
directly so counterfactual P/L applies the same drag basis as actual paper P/L
(design § Inputs point 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alphamind._kernel.money import DECIMAL_ZERO, Money, Price, money, price, signed_money
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.decision.analyst.models import (
    InstrumentEquity,
    InstrumentOption,
    PriceCondition,
    Recommendation,
    TimeCondition,
)
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import InstrumentType

__all__ = [
    "EquityBracketResult",
    "EquityEntryResult",
    "EquityPLResult",
    "EquityReplayResult",
    "compute_equity_pl",
    "replay_equity_proposal",
    "simulate_equity_brackets",
    "simulate_equity_entry",
]

# A market entry needs the proposal bar plus the following bar it fills at.
_MIN_BARS_FOR_MARKET_FILL = 2


@dataclass(frozen=True, slots=True)
class EquityEntryResult:
    """Outcome of entry simulation (design Step 2).

    ``entered`` False means no fill across the entry window; ``entry_price`` and
    ``entry_timestamp`` are then ``None`` and the bracket / P/L steps short-
    circuit to the entry-window-expired-unfilled leg.
    """

    entered: bool
    entry_price: Price | None
    entry_timestamp: datetime | None


def simulate_equity_entry(
    proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
) -> EquityEntryResult:
    """Simulate the entry fill for an equity proposal (design Step 2).

    Walks *bars* (ascending by ``period_start``) over the entry window and
    returns the first fill per the proposal's ``entry_order.type``.
    """
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentEquity)
    direction = instrument.direction
    entry_order = proposal.entry_order

    if entry_order.type == "limit":
        assert entry_order.limit_price is not None
        return _simulate_limit_entry(entry_order.limit_price, direction, bars)
    if entry_order.type == "market":
        return _simulate_market_entry(bars)
    # stop_limit
    assert entry_order.stop_price is not None
    assert entry_order.limit_price is not None
    return _simulate_stop_limit_entry(
        entry_order.stop_price, entry_order.limit_price, direction, bars
    )


def _simulate_stop_limit_entry(
    stop_price: Price,
    limit_price: Price,
    direction: str,
    bars: tuple[OhlcvBar, ...],
) -> EquityEntryResult:
    """Stop-limit: the stop arms the order; the limit then gates the fill.

    Long: the stop fires when ``bar.high >= stop_price``; thereafter the first
    bar with ``bar.low <= limit_price`` fills at the limit (the arming bar
    itself counts when its low already reaches the limit). Short mirrors:
    ``bar.low <= stop_price`` arms, then ``bar.high >= limit_price`` fills.
    """
    stop = float(stop_price)
    limit = float(limit_price)
    armed = False
    for bar in bars:
        if not armed:
            armed = bar.high >= stop if direction == "long" else bar.low <= stop
        if armed:
            filled = bar.low <= limit if direction == "long" else bar.high >= limit
            if filled:
                return EquityEntryResult(
                    entered=True,
                    entry_price=limit_price,
                    entry_timestamp=bar.period_start,
                )
    return EquityEntryResult(entered=False, entry_price=None, entry_timestamp=None)


def _simulate_market_entry(bars: tuple[OhlcvBar, ...]) -> EquityEntryResult:
    """Market order: fill at the open of the bar following the proposal bar.

    Contract with the engine driver (story 08): ``bars[0]`` is the bar that
    *contains* the proposal timestamp (``load_bars`` is called from
    ``window_start`` = the proposal bar's period). The market order therefore
    fills at ``bars[1].open`` — the open of the *next* bar — because filling at
    ``bars[0].open`` would use a price that predates the proposal. A sequence
    with only the proposal bar (or empty) yields no fill.
    """
    if len(bars) < _MIN_BARS_FOR_MARKET_FILL:
        return EquityEntryResult(entered=False, entry_price=None, entry_timestamp=None)
    next_bar = bars[1]
    return EquityEntryResult(
        entered=True,
        entry_price=price(next_bar.open),
        entry_timestamp=next_bar.period_start,
    )


def _simulate_limit_entry(
    limit_price: Price,
    direction: str,
    bars: tuple[OhlcvBar, ...],
) -> EquityEntryResult:
    """Limit order: long fills when bar low touches the limit; short on bar high."""
    threshold = float(limit_price)
    for bar in bars:
        touched = bar.low <= threshold if direction == "long" else bar.high >= threshold
        if touched:
            return EquityEntryResult(
                entered=True,
                entry_price=limit_price,
                entry_timestamp=bar.period_start,
            )
    return EquityEntryResult(entered=False, entry_price=None, entry_timestamp=None)


# ---------------------------------------------------------------------------
# Step 3 — Bracket simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquityBracketResult:
    """Outcome of the bracket walk (design Step 3).

    ``same_bar_ambiguity`` is ``True`` when the target and the price stop both
    trigger within the same bar; the engine assumes the stop fills first (a
    conservative bias) and the confidence classifier (story 05b) demotes such
    replays. When the entry never filled, ``exit_leg`` is
    ``ENTRY_WINDOW_EXPIRED_UNFILLED`` and the price / timestamp are ``None``.
    """

    exit_leg: ExitLeg
    exit_price: Price | None
    exit_timestamp: datetime | None
    same_bar_ambiguity: bool


def simulate_equity_brackets(
    proposal: Recommendation,
    entry: EquityEntryResult,
    bars: tuple[OhlcvBar, ...],
) -> EquityBracketResult:
    """Walk bars forward from entry, checking target / price-stop / time-stop.

    Per design Step 3 the three conditions are evaluated each bar in the
    documented order. A bar that triggers both the target and the price stop is
    recorded as a stop hit with ``same_bar_ambiguity=True``. Price exits record
    at the trigger level; time-stop exits at the bar open at the time-stop
    timestamp. If *entry* never filled, the bracket walk is skipped and the
    entry-window-expired-unfilled leg is returned.
    """
    if not entry.entered:
        return EquityBracketResult(
            exit_leg=ExitLeg.ENTRY_WINDOW_EXPIRED_UNFILLED,
            exit_price=None,
            exit_timestamp=None,
            same_bar_ambiguity=False,
        )

    # Both equity and single-leg option proposals carry underlying-anchored
    # price legs and time legs plus a position-level ``direction``, so the same
    # underlying-bar walk drives the trigger for both. The option replay path
    # (story 06) reuses this walker directly; only the exit-price recording
    # differs (underlying price for equity, BS-derived premium for options).
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentEquity | InstrumentOption)
    direction = instrument.direction
    target = float(proposal.target.price)
    price_stop = _price_stop_trigger(proposal)
    time_stop = _time_stop_deadline(proposal)

    assert entry.entry_timestamp is not None
    last_walked: OhlcvBar | None = None
    for bar in bars:
        if bar.period_start < entry.entry_timestamp:
            continue
        last_walked = bar
        target_hit = bar.high >= target if direction == "long" else bar.low <= target
        stop_hit = price_stop is not None and (
            bar.low <= price_stop if direction == "long" else bar.high >= price_stop
        )
        if stop_hit:
            assert price_stop is not None
            return EquityBracketResult(
                exit_leg=ExitLeg.STOP_HIT,
                exit_price=price(price_stop),
                exit_timestamp=bar.period_start,
                same_bar_ambiguity=target_hit,
            )
        if target_hit:
            return EquityBracketResult(
                exit_leg=ExitLeg.TARGET_HIT,
                exit_price=proposal.target.price,
                exit_timestamp=bar.period_start,
                same_bar_ambiguity=False,
            )
        if time_stop is not None and bar.period_start >= time_stop:
            return EquityBracketResult(
                exit_leg=ExitLeg.TIME_STOP_FIRED,
                exit_price=price(bar.open),
                exit_timestamp=bar.period_start,
                same_bar_ambiguity=False,
            )

    # No price target / stop / hard time-stop fired across the window. The
    # replay window is bounded by the thesis horizon
    # (``max(time_stop_horizon, target_estimated_horizon)`` — see
    # ``compute_replay_window``), so reaching its end is the thesis-duration
    # deadline elapsing: resolve as a time stop at the final bar's open, the
    # same recording rule a hard time leg uses. A proposal carrying only a hard
    # price leg (no time leg) is the case this covers — the schema requires only
    # *one* hard leg, price or time.
    assert last_walked is not None, "entered proposal has no bar at/after entry timestamp"
    return EquityBracketResult(
        exit_leg=ExitLeg.TIME_STOP_FIRED,
        exit_price=price(last_walked.open),
        exit_timestamp=last_walked.period_start,
        same_bar_ambiguity=False,
    )


def _price_stop_trigger(proposal: Recommendation) -> float | None:
    """Return the hard price-stop trigger level, or ``None`` if there is none."""
    for leg in proposal.invalidation_legs:
        if leg.type == "price" and leg.is_hard and isinstance(leg.condition, PriceCondition):
            return float(leg.condition.trigger_price)
    return None


def _time_stop_deadline(proposal: Recommendation) -> datetime | None:
    """Return the hard time-stop deadline, or ``None`` if there is none."""
    for leg in proposal.invalidation_legs:
        if leg.type == "time" and leg.is_hard and isinstance(leg.condition, TimeCondition):
            return leg.condition.deadline
    return None


# ---------------------------------------------------------------------------
# Step 4 — P/L composition
# ---------------------------------------------------------------------------

# Equity has no contract multiplier (design Step 4: multiplier = 1 for equity).
_EQUITY_MULTIPLIER = Decimal(1)

# EntryOrder.type → the paper harness's OrderType (which impact coefficient to
# use). stop_limit maps to the harness's stop coefficient — the only stop-like
# member it carries.
_ENTRY_ORDER_TYPE: dict[str, OrderType] = {
    "market": OrderType.market,
    "limit": OrderType.limit,
    "stop_limit": OrderType.stop,
}

# Exit leg → the order type the exit would have crossed as. A target fills as a
# resting limit; a price stop as a stop; a time stop as a market close.
_EXIT_ORDER_TYPE: dict[ExitLeg, OrderType] = {
    ExitLeg.TARGET_HIT: OrderType.limit,
    ExitLeg.STOP_HIT: OrderType.stop,
    ExitLeg.TIME_STOP_FIRED: OrderType.market,
}


@dataclass(frozen=True, slots=True)
class EquityPLResult:
    """Outcome of P/L composition (design Step 4).

    When the entry never filled, every monetary field is ``None`` — no entry,
    no exit, no P/L — matching the ``CounterfactualReplayRecord`` invariant
    (story 01: ``realized_pl`` must be ``None`` when ``entered`` is ``False``)
    and state-persistence.md's "null if not entered". When the paper harness
    cannot produce an estimate (missing ADV or realized volatility), the
    corresponding slippage and fees are recorded as zero — the confidence
    classifier (story 05b) demotes such replays.
    """

    realized_pl: Money | None
    entry_slippage: Money | None
    entry_fees: Money | None
    exit_slippage: Money | None
    exit_fees: Money | None


def compute_equity_pl(
    proposal: Recommendation,
    entry: EquityEntryResult,
    brackets: EquityBracketResult,
    *,
    paper_harness_config: PaperHarness,
    adv_shares: float | None,
    realized_volatility: float | None,
) -> EquityPLResult:
    """Compose realized P/L for an equity replay (design Step 4).

    ``realized_pl = (exit_price - entry_price) * quantity * direction_sign
    - entry_slippage - entry_fees - exit_slippage - exit_fees``, with
    ``direction_sign = +1`` for long and ``-1`` for short and ``multiplier = 1``
    for equity. Entry and exit slippage / fees come from
    :func:`compute_live_execution_estimate`, called once per side; a ``None``
    return records that side's slippage and fees as zero.
    """
    if not entry.entered:
        # No fill → no entry/exit costs and no P/L. All None per the record's
        # "null when entered is False" invariant (story 01) and
        # state-persistence.md "null if not entered".
        return EquityPLResult(
            realized_pl=None,
            entry_slippage=None,
            entry_fees=None,
            exit_slippage=None,
            exit_fees=None,
        )

    assert entry.entry_price is not None
    assert brackets.exit_price is not None

    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentEquity)
    direction = instrument.direction
    direction_sign = Decimal(1) if direction == "long" else Decimal(-1)
    quantity = Decimal(str(proposal.position_size.quantity))

    entry_side: Literal["buy", "sell"] = "buy" if direction == "long" else "sell"
    exit_side: Literal["buy", "sell"] = "sell" if direction == "long" else "buy"

    entry_slippage, entry_fees = _side_drag(
        fill_price=entry.entry_price,
        order_type=_ENTRY_ORDER_TYPE[proposal.entry_order.type],
        side=entry_side,
        quantity=proposal.position_size.quantity,
        config=paper_harness_config,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
    )
    exit_slippage, exit_fees = _side_drag(
        fill_price=brackets.exit_price,
        order_type=_EXIT_ORDER_TYPE[brackets.exit_leg],
        side=exit_side,
        quantity=proposal.position_size.quantity,
        config=paper_harness_config,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
    )

    gross = (
        (Decimal(brackets.exit_price) - Decimal(entry.entry_price))
        * quantity
        * _EQUITY_MULTIPLIER
        * direction_sign
    )
    realized = gross - entry_slippage - entry_fees - exit_slippage - exit_fees
    return EquityPLResult(
        realized_pl=signed_money(realized),
        entry_slippage=entry_slippage,
        entry_fees=entry_fees,
        exit_slippage=exit_slippage,
        exit_fees=exit_fees,
    )


def _side_drag(
    *,
    fill_price: Price,
    order_type: OrderType,
    side: Literal["buy", "sell"],
    quantity: float,
    config: PaperHarness,
    adv_shares: float | None,
    realized_volatility: float | None,
) -> tuple[Money, Money]:
    """Return ``(slippage, fees)`` for one fill side via the paper harness.

    ``slippage = estimated_spread_usd + estimated_impact_usd``;
    ``fees = estimated_regulatory_fees_usd``. A ``None`` harness estimate
    (missing ADV / realized vol) records both as zero (design Step 4).
    """
    estimate = compute_live_execution_estimate(
        fill_price=fill_price,
        fill_quantity=quantity,
        instrument_type=InstrumentType.EQUITY,
        side=side,
        order_type=order_type,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
        config=config,
    )
    if estimate is None:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    slippage = money(estimate.estimated_spread_usd + estimate.estimated_impact_usd)
    return slippage, estimate.estimated_regulatory_fees_usd


# ---------------------------------------------------------------------------
# Step 5 — Public driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EquityReplayResult:
    """The flat per-proposal equity replay outcome the engine driver folds into
    a ``CounterfactualReplayRecord`` (story 08).

    Composes the entry, bracket, and P/L primitives. ``same_bar_ambiguity`` is
    surfaced for the confidence classifier (story 05b). When the entry never
    filled, ``entered`` is ``False``, ``exit_leg`` is
    ``ENTRY_WINDOW_EXPIRED_UNFILLED``, and every monetary field (entry/exit
    slippage and fees, ``realized_pl``) is ``None`` per the record invariant.
    """

    entered: bool
    entry_price: Price | None
    entry_timestamp: datetime | None
    entry_slippage: Money | None
    entry_fees: Money | None
    exit_leg: ExitLeg
    exit_price: Price | None
    exit_timestamp: datetime | None
    exit_slippage: Money | None
    exit_fees: Money | None
    realized_pl: Money | None
    same_bar_ambiguity: bool


def replay_equity_proposal(
    proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
    *,
    paper_harness_config: PaperHarness,
    adv_shares: float | None,
    realized_volatility: float | None,
) -> EquityReplayResult:
    """Replay one equity proposal end-to-end (design Steps 2-4).

    Composes :func:`simulate_equity_entry`, :func:`simulate_equity_brackets`,
    and :func:`compute_equity_pl` into one :class:`EquityReplayResult`.
    """
    entry = simulate_equity_entry(proposal, bars)
    brackets = simulate_equity_brackets(proposal, entry, bars)
    pl = compute_equity_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=paper_harness_config,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
    )
    return EquityReplayResult(
        entered=entry.entered,
        entry_price=entry.entry_price,
        entry_timestamp=entry.entry_timestamp,
        entry_slippage=pl.entry_slippage,
        entry_fees=pl.entry_fees,
        exit_leg=brackets.exit_leg,
        exit_price=brackets.exit_price,
        exit_timestamp=brackets.exit_timestamp,
        exit_slippage=pl.exit_slippage,
        exit_fees=pl.exit_fees,
        realized_pl=pl.realized_pl,
        same_bar_ambiguity=brackets.same_bar_ambiguity,
    )
