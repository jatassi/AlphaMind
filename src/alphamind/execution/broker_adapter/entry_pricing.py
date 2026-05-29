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
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from alphamind._kernel.money import Price, price
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


@runtime_checkable
class QuoteSource(Protocol):
    """Fetches the live touch for a symbol; ``None`` when no quote is available."""

    async def latest_quote(self, symbol: str) -> TouchQuote | None: ...


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
    new_entry = command.entry_order.model_copy(update={"limit_price": new_limit})
    return command.model_copy(update={"entry_order": new_entry})


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
    price_leg = next((leg for leg in command.invalidation_legs if isinstance(leg, PriceLeg)), None)
    stop = price_leg.condition.trigger_price if price_leg is not None else None
    if direction == "short":
        if new_limit <= target:
            return False
        return stop is None or new_limit < stop
    if new_limit >= target:
        return False
    return stop is None or new_limit > stop
