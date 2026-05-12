"""Subscription target-set computation (story 02b / ALP-434).

The continuous monitor subscribes the live underlying-price stream lazily —
only to the unique set of equity tickers that back open positions, not the
full asset universe. This module is the pure projection over the repository's
``open_positions`` collection that produces that target set; the stream
consumer task (``task.py``) is the impure caller that diffs target sets and
issues ``subscribe_quotes`` / ``unsubscribe_quotes`` against alpaca-py.

Reading from a narrow ``OpenPositionsReader`` protocol (rather than the full
``PortfolioStateRepository``) keeps the test surface small: any object that
exposes ``async def get_open_positions()`` satisfies the contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
)


@runtime_checkable
class OpenPositionsReader(Protocol):
    """Narrow read surface this story needs from ``PortfolioStateRepository``."""

    async def get_open_positions(self) -> tuple[PositionRecord, ...]: ...


async def compute_target_underlyings(reader: OpenPositionsReader) -> frozenset[str]:
    """Return the set of underlying tickers the stream should subscribe to.

    Walks open positions; emits ``ticker`` for equity, ``underlying_ticker``
    for options, and every leg's ``options.underlying_ticker`` for multi-leg
    strategies. Duplicates collapse via the ``frozenset`` return type.
    """
    positions = await reader.get_open_positions()
    targets: set[str] = set()
    for position in positions:
        targets.update(_underlyings_for_position(position))
    return frozenset(targets)


def _underlyings_for_position(position: PositionRecord) -> tuple[str, ...]:
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return (details.ticker,)
    if isinstance(details, OptionsPositionDetails):
        return (details.underlying_ticker,)
    if isinstance(details, StrategyPositionDetails):
        return tuple(leg.options.underlying_ticker for leg in details.legs)
    return ()
