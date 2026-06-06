"""Per-proposal equity replay primitives (ALP-559, design Steps 2–4).

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

from alphamind._kernel.money import Price, price
from alphamind.decision.analyst.models import InstrumentEquity, Recommendation
from alphamind.execution.counterfactual_replay_engine.repos import OhlcvBar

__all__ = [
    "EquityEntryResult",
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

    The bar sequence starts at the proposal timestamp (window_start), so the
    proposal-following bar is the second element. A sequence with only the
    proposal bar (or empty) yields no fill.
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
