"""REST latest-quote price feed for the isolated safety core (ALP-940).

Replaces the safety core's competing market-data websocket (removed in ALP-940)
with a REST latest-quote poll. Alpaca permits exactly **one** concurrent
authenticated market-data websocket per account on the free IEX plan; the monitor
proper keeps that sole websocket, and the safety core polls latest quotes over
REST — which is *not* websocket-connection-limited — into the same
:class:`UnderlyingPriceCache` the safety loop reads. ADR-0004 isolation is
preserved: the safety core's price *source* moved, not its process isolation
(no shared stream, no cross-process price coupling).

The poll loop (:func:`run_price_feed`) is the single writer of the safety core's
cache; the safety loop is the sole liveness anchor (the feed does NOT beat the
heartbeat). A wedged or failing feed surfaces through the existing
``globally_stale`` log while breach detection keeps running off the broker
snapshot — fail-safe under the broker floor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from alphamind.execution.broker_adapter import bounded_call
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import _quote_to_underlying

log = logging.getLogger(__name__)

# Loop-level bound on the blocking REST call (seconds). Mirrors
# ``broker_adapter.queries._REST_TIMEOUT_SECONDS`` (30s) and sits below the
# client factory's 60s socket-timeout floor, so the event-loop ``wait_for`` is
# the bound that normally fires and a hung socket never wedges the poll loop.
_FETCH_TIMEOUT_SECONDS = 30.0

# Zero-arg broker-snapshot equity-symbol source (the subscribe set).
GetSymbols = Callable[[], frozenset[str]]
# Async latest-quote fetch for a symbol set → the surviving (two-sided) quotes.
FetchQuotes = Callable[[frozenset[str]], Awaitable[Iterable[UnderlyingQuote]]]


async def run_price_feed(
    *,
    get_symbols: GetSymbols,
    fetch_quotes: FetchQuotes,
    cache: UnderlyingPriceCache,
    cadence_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run-forever REST latest-quote poll feeding the safety core's price cache.

    Each cycle: compute the broker-snapshot equity set, fetch latest quotes for
    it (when non-empty), and write each surviving quote to the cache the safety
    loop reads. A transient fetch error degrades to no-fresh-price for that cycle
    (the cache simply isn't refreshed → ``globally_stale`` surfaces if it stays
    cold) rather than killing the process; :class:`asyncio.CancelledError`
    propagates so supervised shutdown is honored.
    """
    while True:
        try:
            symbols = get_symbols()
            if symbols:
                for quote in await fetch_quotes(symbols):
                    await cache.update(quote)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("safety_core price feed cycle failed; continuing to next cycle")
        await sleep(cadence_seconds)


def _latest_quotes_to_underlying(response: object, symbols: Iterable[str]) -> list[UnderlyingQuote]:
    """Map a keyed latest-quote response to :class:`UnderlyingQuote` rows.

    Drops any symbol whose quote is missing / one-sided / zero / non-positive
    bid-ask (mirroring ``broker_adapter.quotes._touch_from_quote``) so a
    degenerate quote writes nothing rather than a skewed mid. Each surviving row
    is built via :func:`_quote_to_underlying` — the same mid-price ``(bid+ask)/2``
    + the quote's own tz-aware timestamp the websocket path used, so the cache's
    freshness classification is unchanged. A non-mapping response yields no rows.
    """
    if not isinstance(response, Mapping):
        return []
    quotes: list[UnderlyingQuote] = []
    for symbol in symbols:
        quote = response.get(symbol)
        bid = getattr(quote, "bid_price", None)
        ask = getattr(quote, "ask_price", None)
        if not bid or not ask or bid <= 0 or ask <= 0:
            continue
        quotes.append(_quote_to_underlying(quote))
    return quotes


@dataclass(frozen=True)
class AlpacaLatestQuoteFetcher:
    """REST latest-quote fetcher: one IEX snapshot call per polled symbol set.

    Wraps a ``StockHistoricalDataClient`` (IEX feed, sharing the trading
    credentials, socket-timeout installed by the client factory). The synchronous
    SDK call runs off the event loop via :func:`bounded_call` so a hung socket
    never wedges the poll loop. The keyed response is mapped through the pure
    :func:`_latest_quotes_to_underlying`.
    """

    client: StockHistoricalDataClient
    feed: DataFeed = DataFeed.IEX

    async def __call__(self, symbols: frozenset[str]) -> list[UnderlyingQuote]:
        ordered = sorted(symbols)
        request = StockLatestQuoteRequest(symbol_or_symbols=ordered, feed=self.feed)
        response = await bounded_call(
            self.client.get_stock_latest_quote, request, timeout_seconds=_FETCH_TIMEOUT_SECONDS
        )
        return _latest_quotes_to_underlying(response, ordered)
