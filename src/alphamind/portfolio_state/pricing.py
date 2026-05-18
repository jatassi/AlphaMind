"""Price provider protocols and supporting types for portfolio state.

This module declares the slim read-only protocols the snapshot assembler uses
to mark positions to market:

* :class:`CurrentPriceProvider` — underlying-equity prices keyed by ticker.
* :class:`OptionPriceProvider` — option-contract prices keyed by OCC symbol
  (the same key the collector writes to ``options_contract_snapshots``).

Each protocol carries a matching ``Stub*`` test/fixture implementation. The
production wiring (in-process underlying-cache projection for the former,
``SqlOptionPriceProvider`` against ``options_contract_snapshots`` for the
latter) lives in sibling modules / packages.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class PriceSource(StrEnum):
    """Describes the data-source modality of a price quote."""

    INTRADAY_QUOTE = "INTRADAY_QUOTE"
    OHLCV_CLOSE = "OHLCV_CLOSE"
    STALE_FALLBACK = "STALE_FALLBACK"


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """Immutable price record returned by a price provider.

    The ``ticker`` field carries the provider's key — an equity ticker for
    :class:`CurrentPriceProvider` quotes, an OCC contract symbol for
    :class:`OptionPriceProvider` quotes.
    """

    ticker: str
    price_usd: float
    as_of_timestamp: datetime
    source: PriceSource
    is_stale: bool

    def __post_init__(self) -> None:
        if not (self.price_usd > 0):
            msg = f"price_usd must be > 0; got {self.price_usd}"
            raise ValueError(msg)


class UnknownTickerError(ValueError):
    """Raised by CurrentPriceProvider.get_quote when the ticker is not tracked."""


class UnknownOptionContractError(ValueError):
    """Raised by OptionPriceProvider.get_quote when the OCC symbol has no snapshot row."""


@runtime_checkable
class CurrentPriceProvider(Protocol):
    """Read-only protocol for fetching current prices.

    Methods are synchronous per ALP-454 Pre-resolved decision (C); the
    in-process providers (cache lookups, fixture stubs) carry no real
    I/O. Revisit when a true remote pricing service lands.
    """

    def get_quote(
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

    def get_quotes(
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


@runtime_checkable
class OptionPriceProvider(Protocol):
    """Read-only protocol for fetching option-contract prices.

    Quotes are keyed by OCC contract symbol — the same string the collector
    writes to ``options_contract_snapshots.contract_ticker`` and the greeks
    refresh task reads. Sync surface mirrors :class:`CurrentPriceProvider`
    per ALP-454 (C).
    """

    def get_quote(
        self,
        occ_symbol: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        """Return a quote for *occ_symbol*.

        ``is_stale`` is ``True`` when ``as_of_timestamp`` is older than
        ``freshness_threshold_seconds``. Raises ``UnknownOptionContractError``
        when no snapshot row exists for *occ_symbol*. Never raises on staleness.
        """
        ...

    def get_quotes(
        self,
        occ_symbols: tuple[str, ...],
        *,
        freshness_threshold_seconds: float,
    ) -> dict[str, PriceQuote]:
        """Return a dict of quotes keyed by OCC symbol.

        Symbols not present in the backing store are omitted; the method
        never raises for unknown symbols. Each quote carries ``is_stale``
        set independently per the same freshness threshold.
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
        return dataclasses.replace(quote, is_stale=is_stale)

    def get_quote(
        self,
        ticker: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        if ticker not in self._quotes:
            raise UnknownTickerError(ticker)
        return self._recompute(self._quotes[ticker], freshness_threshold_seconds)

    def get_quotes(
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


class StubOptionPriceProvider:
    """Concrete stub for tests and fixtures — no I/O.

    ``quotes`` is the backing store (keyed by OCC symbol). Otherwise identical
    in shape to :class:`StubCurrentPriceProvider`.
    """

    def __init__(self, quotes: dict[str, PriceQuote], now: datetime) -> None:
        self._quotes = quotes
        self._now = now

    def _recompute(self, quote: PriceQuote, freshness_threshold_seconds: float) -> PriceQuote:
        age = (self._now - quote.as_of_timestamp).total_seconds()
        is_stale = age > freshness_threshold_seconds
        return dataclasses.replace(quote, is_stale=is_stale)

    def get_quote(
        self,
        occ_symbol: str,
        *,
        freshness_threshold_seconds: float,
    ) -> PriceQuote:
        if occ_symbol not in self._quotes:
            raise UnknownOptionContractError(occ_symbol)
        return self._recompute(self._quotes[occ_symbol], freshness_threshold_seconds)

    def get_quotes(
        self,
        occ_symbols: tuple[str, ...],
        *,
        freshness_threshold_seconds: float,
    ) -> dict[str, PriceQuote]:
        return {
            s: self._recompute(self._quotes[s], freshness_threshold_seconds)
            for s in occ_symbols
            if s in self._quotes
        }


__all__ = [
    "UTC",
    "CurrentPriceProvider",
    "OptionPriceProvider",
    "PriceQuote",
    "PriceSource",
    "StubCurrentPriceProvider",
    "StubOptionPriceProvider",
    "UnknownOptionContractError",
    "UnknownTickerError",
]
