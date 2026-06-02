"""Marketable-limit entry pricing for enter-now theses — ALP-738.

An analyst "enter-now" equity entry (``type=limit`` with no ``entry_window``)
is rewritten from its static away-from-market limit into a *marketable* limit
priced through the touch, so it crosses the spread and fills at the prevailing
quote instead of resting above a falling market (the 2026-05-28 zero-fill
incident, parent investigation ALP-735).

Patient-retest entries — a limit carrying an analyst ``entry_window`` — are left
verbatim: their deadline + reprice/escalate handling is the follow-up to this
issue, and ALP-737's entry_window watcher already auto-cancels them at the
deadline. Market / stop_limit entries and non-equity instruments are also left
verbatim (the incident was equity short ``SELL LIMIT`` brackets).

The module is deliberately I/O-free: :func:`marketable_limit_price` is a pure
Decimal computation and :func:`rewrite_enter_now_entries` takes the live quote
through an injected :class:`QuoteSource`. The Alpaca-backed implementation lives
in :mod:`alphamind.execution.broker_adapter.quotes`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from alphamind._kernel.money import Price, money, price
from alphamind.commands.command_models import EquityInstrument, OpenCommand, PriceLeg

if TYPE_CHECKING:
    from alphamind.commands.command_models import Direction, OMSCommand

logger = logging.getLogger(__name__)

_PENNY = Decimal("0.01")
_SUBPENNY = Decimal("0.0001")
# US equities quote in pennies at/above $1.00 and sub-pennies below it
# (Reg NMS Rule 612 minimum pricing increment).
_SUBPENNY_THRESHOLD = Decimal("1.00")
_BPS_DENOMINATOR = Decimal(10_000)


@dataclass(frozen=True)
class TouchQuote:
    """The live touch (best bid / best ask) for a symbol."""

    bid: Price
    ask: Price

    @property
    def mid(self) -> Price:
        """The touch midpoint ``(bid + ask) / 2`` — a coarse current-spot proxy.

        Used by the ALP-753 phase-1 reference anchor as the single scalar that
        mirrors a held position's ``current_price``. ``bid``/``ask`` are both
        strictly positive (constructed via :func:`price`), so the mid is too.
        """
        return price((self.bid + self.ask) / 2)


@runtime_checkable
class QuoteSource(Protocol):
    """Fetches the live touch for a symbol; ``None`` when no quote is available."""

    async def latest_quote(self, symbol: str) -> TouchQuote | None: ...


@runtime_checkable
class BatchQuoteSource(Protocol):
    """Fetches live touches for many symbols in one batch (ALP-753).

    The returned mapping contains only symbols with a usable two-sided quote;
    a missing / one-sided / zero touch is dropped per symbol, so the caller
    falls back to a recorded reference for the dropped symbol. A whole-batch
    broker failure raises ``RuntimeError`` (rather than the singular
    :class:`QuoteSource`'s ``None``) so the phase-1 caller degrades the entire
    reference layer and flips its staleness flag.
    """

    async def latest_quotes(self, symbols: Sequence[str]) -> Mapping[str, TouchQuote]: ...


def _tick_for(value: Decimal) -> Decimal:
    return _SUBPENNY if value < _SUBPENNY_THRESHOLD else _PENNY


def marketable_limit_price(
    *, direction: Direction, quote: TouchQuote, bps_through_touch: float
) -> Price:
    """Price a marketable limit that crosses the spread to secure a fill.

    A short (SELL) entry is priced at/below the **bid** so it trades against
    resting bids; a long (BUY) entry at/above the **ask**. ``bps_through_touch``
    pushes the limit a few basis points past the touch to absorb depth, and the
    result is rounded to the minimum pricing increment in the aggressive
    direction (SELL down, BUY up) so it stays marketable after tick conformance.
    """
    # quote.bid / quote.ask are already Decimal (Price is a Decimal NewType).
    factor = Decimal(str(bps_through_touch)) / _BPS_DENOMINATOR
    if direction == "short":
        raw = quote.bid * (Decimal(1) - factor)
        return price(raw.quantize(_tick_for(raw), rounding=ROUND_DOWN))
    raw = quote.ask * (Decimal(1) + factor)
    return price(raw.quantize(_tick_for(raw), rounding=ROUND_UP))


def _is_enter_now_equity(command: OMSCommand) -> bool:
    """True when *command* is an equity OPEN the analyst wants filled now.

    "Enter-now" is a ``limit`` entry carrying no ``entry_window``: the analyst
    expressed a price but no patience window, so it should fill at the quote
    rather than rest. A limit *with* an ``entry_window`` is a patient retest
    (left verbatim, handled by ALP-737's deadline watcher); ``market`` already
    fills and ``stop_limit`` is a conditional breakout entry.

    Scope is OPEN bracket entries only — the ``isinstance(command, OpenCommand)``
    guard intentionally excludes ``AddCommand`` (which also carries an
    ``entry_order`` but no ``entry_window`` field); adds-to-existing-positions
    are out of scope for this incident (ALP-738).
    """
    return (
        isinstance(command, OpenCommand)
        and isinstance(command.instrument, EquityInstrument)
        and command.entry_order.type == "limit"
        and command.entry_window is None
    )


async def rewrite_enter_now_entries(
    commands: tuple[OMSCommand, ...],
    *,
    quote_source: QuoteSource,
    bps_through_touch: float,
) -> tuple[OMSCommand, ...]:
    """Rewrite each enter-now equity OPEN to a marketable limit through the touch.

    Returns a new tuple with enter-now entries re-priced and every other command
    unchanged (same object). On a missing quote or a pricing failure the entry is
    left verbatim and a warning is logged — the rewrite never aborts the
    submission (a stranded limit is caught downstream by ALP-739's no-fill
    alert), and it is intentionally a no-op when no enter-now entry is present.
    """
    return tuple(
        [
            await _rewrite_one(command, quote_source=quote_source, bps=bps_through_touch)
            if _is_enter_now_equity(command)
            else command
            for command in commands
        ]
    )


async def _rewrite_one(command: OMSCommand, *, quote_source: QuoteSource, bps: float) -> OMSCommand:
    assert isinstance(command, OpenCommand)
    assert isinstance(command.instrument, EquityInstrument)
    ticker = command.instrument.ticker
    direction = command.instrument.direction
    # One guard around the whole resolve: a quote-source raising (a misbehaving
    # QuoteSource — the Alpaca one already returns None on error) or
    # marketable_limit_price raising on a degenerate touch both degrade to
    # leaving the entry verbatim rather than aborting the submission.
    try:
        quote = await quote_source.latest_quote(ticker)
        if quote is None:
            logger.warning(
                "entry_pricing: no quote for %s; leaving enter-now limit %s verbatim",
                ticker,
                command.entry_order.limit_price,
            )
            return command
        new_limit = marketable_limit_price(direction=direction, quote=quote, bps_through_touch=bps)
    except Exception:
        logger.warning(
            "entry_pricing: could not resolve a marketable limit for %s; leaving verbatim",
            ticker,
            exc_info=True,
        )
        return command
    if not _preserves_bracket_geometry(direction, new_limit, command):
        logger.warning(
            "entry_pricing: marketable limit %s for %s %s would invert the bracket "
            "(take_profit=%s); leaving enter-now entry verbatim",
            new_limit,
            ticker,
            direction,
            command.target.price,
        )
        return command
    logger.info(
        "entry_pricing: %s %s enter-now limit %s -> marketable %s (bid=%s ask=%s)",
        ticker,
        direction,
        command.entry_order.limit_price,
        new_limit,
        quote.bid,
        quote.ask,
    )
    # model_copy (not a fresh EntryOrder) so any future entry_order fields ride
    # through unchanged and we don't re-assert the type=="limit"/no-stop_price
    # invariant the classifier already guarantees.
    # ALP-765: recompute dollar_value so limit_price x quantity == dollar_value.
    new_dollar_value = money(new_limit * Decimal(str(command.position_size.quantity)))
    new_entry = command.entry_order.model_copy(update={"limit_price": new_limit})
    new_position_size = command.position_size.model_copy(update={"dollar_value": new_dollar_value})
    return command.model_copy(update={"entry_order": new_entry, "position_size": new_position_size})


def _first_price_leg(command: OpenCommand) -> PriceLeg | None:
    """The bracket's protective-stop leg — the first price-invalidation leg, or
    ``None`` for an OTO bracket (only a hard time leg). Mirrors
    ``validation._protective_stop_price`` and the equity OPEN bracket builder,
    which both take the first price leg as the bracket's stop."""
    return next((leg for leg in command.invalidation_legs if isinstance(leg, PriceLeg)), None)


def _preserves_bracket_geometry(
    direction: Direction, new_limit: Price, command: OpenCommand
) -> bool:
    """True if *new_limit* keeps the bracket's entry/target/stop ordering valid.

    Re-pricing moves a short entry **down** toward the bid / a long entry **up**
    toward the ask. If the analyst paired the entry with a tight take-profit (or
    stop), the marketable price can cross it and invert the bracket — which the
    broker rejects outright (a worse outcome than the original no-fill). When
    that would happen we leave the entry verbatim, preserving the analyst's
    self-consistent bracket rather than submitting one the broker will reject.

    A short bracket needs ``take_profit < entry < stop``; a long bracket needs
    ``stop < entry < take_profit``. ``Target.price`` is present for every
    target_type (Target's validator); the price-stop leg is optional (an OTO
    bracket has none).
    """
    target = command.target.price
    assert target is not None  # Target validator guarantees a price for every target_type.
    price_leg = _first_price_leg(command)
    stop = price_leg.condition.trigger_price if price_leg is not None else None
    if direction == "short":
        if new_limit <= target:
            return False
        return stop is None or new_limit < stop
    if new_limit >= target:
        return False
    return stop is None or new_limit > stop


def fills_at_live_quote_equity(command: OMSCommand) -> bool:
    """True when *command* is an equity OPEN whose entry fills at the live quote.

    A ``market`` order fills at the prevailing quote unconditionally — any
    ``entry_window`` annotation on it is vestigial (a market order never rests),
    so it is always in scope. An enter-now ``limit`` (a ``limit`` with no
    ``entry_window``) is repriced to marketable by ALP-738 and likewise fills at
    the touch, so it is in scope too. A patient-retest ``limit`` (carries an
    ``entry_window``) and a ``stop_limit`` breakout deliberately rest away from
    the touch and are exempt; non-equity and non-OPEN commands are out of scope.
    Callers gate the dispatch-time live-coherence check
    (:func:`live_bracket_incoherence_reason`) on this predicate so a quote is
    fetched only when the check can apply.
    """
    if not (isinstance(command, OpenCommand) and isinstance(command.instrument, EquityInstrument)):
        return False
    entry_type = command.entry_order.type
    if entry_type == "market":
        return True
    if entry_type == "limit":
        return command.entry_window is None
    return False  # stop_limit breakout — anchors away from the touch


def live_bracket_incoherence_reason(command: OpenCommand, *, quote: TouchQuote) -> str | None:
    """Reason *command*'s bracket is directionally incoherent vs the live touch,
    or ``None`` when it is coherent.

    The deterministic dispatch-time complement of :func:`rewrite_enter_now_entries`
    (ALP-747): where the rewrite makes an enter-now entry fill *at* the quote,
    this rejects a bracket whose target/stop cannot work at the live price — a
    long target at/below where you'd buy (the ORCL stale-anchor mispricing:
    entry $198 / target $212 with ORCL trading $226), or a short target at/above
    where you'd sell. Such a bracket straddles its *stale* anchor
    self-consistently, so the analyst-side ALP-742 guard and the broker's own
    bracket validation both pass it; checked against the *live* touch at
    submission it is exposed as mispriced and fails closed before dispatch.

    Scope is :func:`fills_at_live_quote_equity` (equity OPEN, market or enter-now
    limit); this function re-checks it so an out-of-scope command always returns
    ``None`` even if a caller forgets to gate. The directional touch is the price
    the entry transacts at — the ask for a long, the bid for a short. The
    protective stop is the first price-invalidation leg (an OTO bracket has none).
    """
    if not fills_at_live_quote_equity(command):
        return None
    assert isinstance(command.instrument, EquityInstrument)
    target = command.target.price
    assert target is not None  # Target validator guarantees a price for every target_type.
    direction = command.instrument.direction
    ticker = command.instrument.ticker
    base = quote.ask if direction == "long" else quote.bid
    price_leg = _first_price_leg(command)
    stop = price_leg.condition.trigger_price if price_leg is not None else None
    if direction == "long":
        return _long_bracket_incoherence(ticker, target=target, stop=stop, base=base)
    return _short_bracket_incoherence(ticker, target=target, stop=stop, base=base)


def _long_bracket_incoherence(
    ticker: str, *, target: Price, stop: Price | None, base: Price
) -> str | None:
    """Long bracket vs the live ask: the target must clear it and the stop sit below it."""
    if target <= base:
        return (
            f"{ticker} long: target {target} is at/below the live ask {base}; the bracket was "
            "sized against a stale price and cannot profit at a fill near the live quote"
        )
    if stop is not None and stop >= base:
        return (
            f"{ticker} long: protective stop {stop} is at/above the live ask {base}; a fill "
            "near the live quote would stop out immediately"
        )
    return None


def _short_bracket_incoherence(
    ticker: str, *, target: Price, stop: Price | None, base: Price
) -> str | None:
    """Short bracket vs the live bid: the target must sit below it and the stop above it."""
    if target >= base:
        return (
            f"{ticker} short: target {target} is at/above the live bid {base}; the bracket was "
            "sized against a stale price and cannot profit at a fill near the live quote"
        )
    if stop is not None and stop <= base:
        return (
            f"{ticker} short: protective stop {stop} is at/below the live bid {base}; a fill "
            "near the live quote would stop out immediately"
        )
    return None
