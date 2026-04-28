"""Current price provider protocol and supporting types for portfolio state.

This module declares the slim read-only protocol the snapshot assembler uses to
fetch current prices for mark-to-market computations (market value, P/L,
distance-to-target, fill-probability context). Production wiring lands in a
future story; this story provides the Protocol, value object, enum, exception,
and a test-and-fixture-only stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PriceSource(StrEnum):
    """Describes the data-source modality of a price quote."""

    INTRADAY_QUOTE = "INTRADAY_QUOTE"
    OHLCV_CLOSE = "OHLCV_CLOSE"
    STALE_FALLBACK = "STALE_FALLBACK"


class PriceQuote(BaseModel):
    """Immutable price record returned by a CurrentPriceProvider."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    price_usd: float = Field(gt=0)
    as_of_timestamp: datetime
    source: PriceSource
    is_stale: bool


class UnknownTickerError(ValueError):
    """Raised by CurrentPriceProvider.get_quote when the ticker is not tracked."""


@runtime_checkable
class CurrentPriceProvider(Protocol):
    """Read-only protocol for fetching current prices."""

    async def get_quote(
        self,
        ticker: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        """Return a quote for *ticker*.

        ``is_stale`` is ``True`` when ``as_of_timestamp`` is older than
        ``freshness_threshold_seconds``. Raises ``UnknownTickerError`` for
        unrecognised tickers. Never raises on staleness.
        """
        ...

    async def get_quotes(
        self,
        tickers: tuple[str, ...],
        *,
        freshness_threshold_seconds: float,
    ) -> dict[str, PriceQuote]:
        """Return a dict of quotes keyed by ticker.

        Tickers not tracked by the provider are omitted from the result; the
        method never raises for unrecognised tickers. Each quote carries
        ``is_stale`` set independently per the same freshness threshold.
        """
        ...


class StubCurrentPriceProvider:
    """Concrete stub for tests and fixtures — no I/O.

    ``quotes`` is the backing store (keyed by ticker). ``now`` is the stub's
    clock: a tz-aware UTC datetime supplied at construction so tests can
    simulate any point in time without freezing system time.
    """

    def __init__(self, quotes: dict[str, PriceQuote], now: datetime) -> None:
        self._quotes = quotes
        self._now = now

    def _recompute(self, quote: PriceQuote, freshness_threshold_seconds: float) -> PriceQuote:
        age = (self._now - quote.as_of_timestamp).total_seconds()
        is_stale = age > freshness_threshold_seconds
        return quote.model_copy(update={"is_stale": is_stale})

    async def get_quote(
        self,
        ticker: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        if ticker not in self._quotes:
            raise UnknownTickerError(ticker)
        return self._recompute(self._quotes[ticker], freshness_threshold_seconds)

    async def get_quotes(
        self,
        tickers: tuple[str, ...],
        *,
        freshness_threshold_seconds: float,
    ) -> dict[str, PriceQuote]:
        return {
            t: self._recompute(self._quotes[t], freshness_threshold_seconds)
            for t in tickers
            if t in self._quotes
        }


__all__ = [
    "UTC",
    "CurrentPriceProvider",
    "PriceQuote",
    "PriceSource",
    "StubCurrentPriceProvider",
    "UnknownTickerError",
]
