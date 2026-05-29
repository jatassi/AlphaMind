"""Alpaca-backed live-quote source for marketable entry pricing — ALP-738.

:class:`AlpacaQuoteSource` adapts a ``StockHistoricalDataClient`` snapshot
(``get_stock_latest_quote``) to the in-process
:class:`alphamind.execution.broker_adapter.entry_pricing.QuoteSource` Protocol the
marketable-entry rewrite depends on. The synchronous SDK call runs off the event
loop via :func:`asyncio.to_thread`; the broker's float bid/ask are parsed into
Decimal :class:`Price` at the boundary, and a one-sided / zero / missing quote
returns ``None`` so the rewrite leaves the entry verbatim.

ALP-753 adds the batch :meth:`AlpacaQuoteSource.latest_quotes` (one API call for
many symbols) so phase-1 input gathering can anchor every active-universe
candidate on a freshest-possible live quote, not a recorded bar.

The IEX feed mirrors the continuous monitor's ``underlying_stream`` (the paper
account's available real-time feed); the Data API shares the Trading API
credentials, so no separate market-data credential block is required.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from alphamind._kernel.money import price
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote

logger = logging.getLogger(__name__)


def _touch_from_quote(quote: object | None) -> TouchQuote | None:
    """Parse a broker latest-quote row into a two-sided :class:`TouchQuote`.

    Returns ``None`` for a missing quote or one whose bid/ask is one-sided,
    zero, or non-positive — the shared per-symbol drop semantics both the
    singular and batch fetch paths apply. ``price()`` parses the SDK float at
    the boundary (str-converts internally to dodge binary-float drift).
    """
    if quote is None:
        return None
    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    return TouchQuote(bid=price(bid), ask=price(ask))


@dataclass(frozen=True)
class AlpacaQuoteSource:
    """A :class:`QuoteSource` / :class:`BatchQuoteSource` backed by Alpaca snapshots."""

    client: StockHistoricalDataClient
    feed: DataFeed = DataFeed.IEX

    async def latest_quote(self, symbol: str) -> TouchQuote | None:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
        try:
            response = await asyncio.to_thread(self.client.get_stock_latest_quote, request)
        except Exception:
            logger.warning("quotes: latest-quote snapshot raised for %s", symbol, exc_info=True)
            return None
        quote = response.get(symbol) if isinstance(response, dict) else None
        return _touch_from_quote(quote)

    async def latest_quotes(self, symbols: Sequence[str]) -> Mapping[str, TouchQuote]:
        """Batch latest-quote snapshot for many symbols in one API call (ALP-753).

        Passes the whole symbol list as ``StockLatestQuoteRequest.symbol_or_symbols``
        (one request) and maps the keyed response per symbol, dropping any symbol
        whose quote is missing / one-sided / zero — the same per-symbol semantics
        as :meth:`latest_quote`, so the caller falls back to a recorded reference
        for a dropped symbol.

        Unlike :meth:`latest_quote` (which degrades a raising snapshot to ``None``
        for the marketable-entry rewrite's leave-verbatim path), a whole-batch
        broker failure here is translated into ``RuntimeError`` — the phase-1
        gatherer catches it to degrade the entire reference layer to recorded
        bars and flip ``staleness_flag``, mirroring ``AccountStateQueries``'
        RuntimeError vocabulary. An empty symbol list short-circuits to ``{}``
        without an API call.
        """
        symbol_list = list(symbols)
        if not symbol_list:
            return {}
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol_list, feed=self.feed)
        try:
            response = await asyncio.to_thread(self.client.get_stock_latest_quote, request)
        except Exception as exc:
            # Warranted broad except (ALP-753): the alpaca-py snapshot can raise
            # APIError, transport (httpx/requests) errors, or parse failures; all
            # mean "the batch fetch failed". Re-raise as RuntimeError so the
            # phase-1 degradation path (which catches RuntimeError) flips
            # staleness_flag rather than the invocation aborting on a raw SDK error.
            msg = f"batch latest-quote snapshot failed for {len(symbol_list)} symbol(s)"
            raise RuntimeError(msg) from exc
        if not isinstance(response, dict):
            return {}
        quotes: dict[str, TouchQuote] = {}
        for symbol in symbol_list:
            touch = _touch_from_quote(response.get(symbol))
            if touch is not None:
                quotes[symbol] = touch
        return quotes
