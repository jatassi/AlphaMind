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
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class PriceFreshness(StrEnum):
    """Discriminator for a :data:`PriceRead`.

    Aligns with the ``portfolio_state/freshness.py`` vocabulary
    (``position_ids_priced_fresh`` / ``position_ids_priced_stale`` /
    ``position_ids_unknown_ticker``): ``FRESH`` ↔ priced-fresh, ``STALE`` ↔
    priced-stale. ``MISSING`` is the cache's precise truth for a ticker that was
    never written (the assembler's ``unknown_ticker`` analog) — kept under the
    cache's own name because absence-from-cache, not a bad symbol, is what it
    means here.
    """

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class FreshPrice:
    """A live read: the quote's ``as_of`` is within ``max_age_seconds``.

    The only variant that exposes an actionable ``price``. A safety-critical
    consumer reaches a usable price *only* by narrowing the :data:`PriceRead`
    union to this type, so "stale price treated as live" (ALP-770) is a type
    error rather than a silent runtime read.
    """

    price: float
    as_of: datetime
    freshness: Literal[PriceFreshness.FRESH] = field(default=PriceFreshness.FRESH)


@dataclass(frozen=True, slots=True)
class StalePrice:
    """A read whose quote is older than ``max_age_seconds``.

    Carries the last-observed price as ``last_price`` (deliberately *not*
    ``price``) plus the positive ``age_seconds`` for diagnostics/logging. The
    asymmetric attribute name is the forcing function: a consumer cannot read
    ``.price`` off a stale result through the same attribute a fresh read uses.
    """

    last_price: float
    as_of: datetime
    age_seconds: float
    freshness: Literal[PriceFreshness.STALE] = field(default=PriceFreshness.STALE)


@dataclass(frozen=True, slots=True)
class MissingPrice:
    """A read for a ticker the cache has never observed. Exposes no price."""

    freshness: Literal[PriceFreshness.MISSING] = field(default=PriceFreshness.MISSING)


# Discriminated union a price consumer pattern-matches on. Only ``FreshPrice``
# yields a usable ``price`` — see each variant's docstring.
PriceRead = FreshPrice | StalePrice | MissingPrice


@dataclass(frozen=True, slots=True)
class GlobalStalenessSignal:
    """Cache-boundary signal: the whole underlying feed is cold (ALP-825 Defect B).

    Mirrors :class:`BreachLoopHealthSignal`'s value-object shape — a pure record
    the single emitter (wired in 02d through ``breach_loop``'s health channel)
    carries; no behavior here. Emitted iff *every* expected ticker reads
    ``STALE`` or ``MISSING`` at ``as_of`` — the writer-wedged case where the
    monitor would otherwise enforce stops against frozen prices. ``stale_tickers``
    /``missing_tickers`` partition ``expected_tickers`` (their union equals it)
    so the operator surface can name what is cold.
    """

    as_of: datetime
    max_age_seconds: float
    expected_tickers: frozenset[str]
    stale_tickers: frozenset[str]
    missing_tickers: frozenset[str]


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

    def read(self, ticker: str, *, as_of: datetime, max_age_seconds: float) -> PriceRead:
        """Return a freshness-classified read of *ticker* against ``as_of``.

        The enforcement-path read: ``FreshPrice`` when the quote's ``as_of`` is
        within ``max_age_seconds`` of *as_of*, ``StalePrice`` (with the age)
        when older, ``MissingPrice`` when the ticker has never been observed.
        Lock-free, like ``get``.
        """
        quote = self._quotes.get(ticker)
        if quote is None:
            return MissingPrice()
        age_seconds = (as_of - quote.as_of).total_seconds()
        if age_seconds > max_age_seconds:
            return StalePrice(last_price=quote.price, as_of=quote.as_of, age_seconds=age_seconds)
        return FreshPrice(price=quote.price, as_of=quote.as_of)

    def get_all(self) -> Mapping[str, UnderlyingQuote]:
        """Return a snapshot of every ticker → quote pair currently in the cache.

        The returned mapping is a shallow copy; subsequent ``update`` calls
        do not mutate it, so callers can iterate without holding a lock.
        """
        return dict(self._quotes)
