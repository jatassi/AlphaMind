"""Alpaca-backed live-quote source for marketable entry pricing — ALP-738.

:class:`AlpacaQuoteSource` adapts a ``StockHistoricalDataClient`` snapshot
(``get_stock_latest_quote``) to the in-process
:class:`alphamind.execution.broker_adapter.entry_pricing.QuoteSource` Protocol the
marketable-entry rewrite depends on. The synchronous SDK call runs off the event
loop via :func:`asyncio.to_thread`; the broker's float bid/ask are parsed into
Decimal :class:`Price` at the boundary, and a one-sided / zero / missing quote
returns ``None`` so the rewrite leaves the entry verbatim.

The IEX feed mirrors the continuous monitor's ``underlying_stream`` (the paper
account's available real-time feed); the Data API shares the Trading API
credentials, so no separate market-data credential block is required.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from alphamind._kernel.money import price
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlpacaQuoteSource:
    """A :class:`QuoteSource` backed by an Alpaca latest-quote snapshot."""

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
        if quote is None:
            return None
        bid = getattr(quote, "bid_price", None)
        ask = getattr(quote, "ask_price", None)
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        # price() parses float at the boundary (str-converts internally to dodge
        # binary-float drift), so pass the SDK float directly.
        return TouchQuote(bid=price(bid), ask=price(ask))
