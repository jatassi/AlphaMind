"""``UnderlyingPriceCache`` — in-memory ticker → latest-quote reader (story 02b).

The breach-evaluation loop (story 03b), greeks refresh task (story 03a), and
options bracket-stop watcher (story 04c) all need cheap, low-latency reads of
"what is the underlying trading at right now?" without coupling to the
``StockDataStream`` transport. The cache is the seam:

* The stream consumer (``run_underlying_stream``) is the single writer; each
  quote translated from alpaca-py's ``Quote`` payload becomes one ``update``.
* Readers call ``get(ticker)`` for a single fresh tick or ``get_all()`` for a
  snapshot suitable for iterating without holding a lock.

Concurrency model — single-writer, multiple-readers, asyncio-only:

* ``update`` is awaitable and serialises through an ``asyncio.Lock`` so
  interleaved writers cannot produce torn state. Writers within one event
  loop already serialise cooperatively; the lock guards correctness when
  reconnect logic launches an additional drainer.
* ``get`` and ``get_all`` are synchronous and lock-free. They observe the
  dictionary state at the moment they read; ``get_all`` returns a shallow
  copy so the caller sees a stable snapshot even if writes continue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UnderlyingQuote:
    """Single underlying-equity quote observed on the live stream.

    ``as_of`` carries Alpaca's event timestamp (tz-aware UTC) — the same
    convention the fill-stream and snapshot-assembler primitives use so the
    monitor's logs and breach-evaluator can reason about freshness uniformly.
    """

    ticker: str
    price: float
    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            msg = "UnderlyingQuote.as_of must be tz-aware UTC"
            raise ValueError(msg)


class UnderlyingPriceCache:
    """Asyncio-safe in-memory mapping of underlying ticker → latest quote."""

    def __init__(self) -> None:
        self._quotes: dict[str, UnderlyingQuote] = {}
        self._lock = asyncio.Lock()

    async def update(self, quote: UnderlyingQuote) -> None:
        """Record *quote* as the latest observation for ``quote.ticker``.

        Acquires the write lock for the duration of the assignment so a
        reconnect-time concurrent writer cannot produce torn state.
        """
        async with self._lock:
            self._quotes[quote.ticker] = quote

    def get(self, ticker: str) -> UnderlyingQuote | None:
        """Return the latest quote for *ticker* or ``None`` if not yet seen."""
        return self._quotes.get(ticker)

    def get_all(self) -> Mapping[str, UnderlyingQuote]:
        """Return a snapshot of every ticker → quote pair currently in the cache.

        The returned mapping is a shallow copy; subsequent ``update`` calls
        do not mutate it, so callers can iterate without holding a lock.
        """
        return dict(self._quotes)
