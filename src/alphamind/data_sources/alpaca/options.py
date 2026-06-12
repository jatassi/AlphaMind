"""Alpaca market-data fetches merged into the options collector (ALP-949).

Polygon's options snapshots on the current plan carry greeks / open
interest but no NBBO quotes and no underlying price; Alpaca's free plan
carries indicative real-time option quotes and IEX stock trades but no
open interest. The two complement exactly, so
:func:`alphamind.data_sources.polygon.options.collect_options_chains`
merges this module's quotes and spot prices into the Polygon-sourced
snapshot rows at collection time.

Exports
-------
OptionQuote
fetch_chain_quotes(underlying, ...)        — full chain, keyed by bare OCC symbol
fetch_underlying_trades(tickers, ...)      — latest IEX trade price per ticker
ALPACA_FETCH_ERRORS                        — exception classes callers degrade on
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any

import requests.exceptions
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest

# The single flip point if the operator subscribes to Alpaca's Algo Trader
# Plus market-data plan (paid; unlocks the real-time OPRA feed). On the
# Basic (free) plan, ``indicative`` is the only available options feed.
_OPTIONS_FEED = OptionsFeed.INDICATIVE

# Exceptions a chain/spot fetch raises on vendor or transport failure —
# callers degrade to Polygon-only rows on these rather than aborting.
# ``ValueError`` covers alpaca-py's unexpected-response-shape error ("The
# data in response does not match any known keys") and pydantic response
# validation (``ValidationError`` subclasses ``ValueError``) — a vendor
# format change must degrade, not abort the collection run.
ALPACA_FETCH_ERRORS: tuple[type[Exception], ...] = (
    APIError,
    requests.exceptions.RequestException,
    ValueError,
)

# alpaca-py issues blocking ``requests`` calls without a timeout, so a hung
# connection would wedge the collector task forever. Mirrors the execution
# layer's client factory (``broker_adapter/client_factory.py``, ALP-841) —
# duplicated because ``data_sources`` and ``execution`` are independent
# siblings under the import-linter layer contract.
_SOCKET_TIMEOUT_SECONDS = 60.0


def _install_socket_timeout(client: Any) -> None:
    """Default a connect/read timeout onto a REST client's ``requests`` session."""
    session: Any = client._session
    original_request = session.request

    @functools.wraps(original_request)
    def request_with_timeout(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _SOCKET_TIMEOUT_SECONDS)
        return original_request(*args, **kwargs)

    session.request = request_with_timeout


@dataclass(frozen=True)
class OptionQuote:
    """NBBO + last trade for one contract, any field absent as ``None``."""

    bid: float | None
    ask: float | None
    last_price: float | None


def make_option_client() -> OptionHistoricalDataClient:
    """Build the option-snapshot client from ``ALPACA_PAPER_*`` env vars.

    The market-data plane accepts paper keys; reading the env directly
    mirrors the :class:`~alphamind.data_sources.polygon.client.PolygonClient`
    convention. Raises ``ValueError`` (from the SDK) when the keys are unset.
    """
    client = OptionHistoricalDataClient(
        api_key=os.environ.get("ALPACA_PAPER_KEY"),
        secret_key=os.environ.get("ALPACA_PAPER_SECRET"),
    )
    _install_socket_timeout(client)
    return client


def make_stock_client() -> StockHistoricalDataClient:
    """Build the stock-trade client from ``ALPACA_PAPER_*`` env vars."""
    client = StockHistoricalDataClient(
        api_key=os.environ.get("ALPACA_PAPER_KEY"),
        secret_key=os.environ.get("ALPACA_PAPER_SECRET"),
    )
    _install_socket_timeout(client)
    return client


def fetch_chain_quotes(underlying: str, *, _client: Any = None) -> dict[str, OptionQuote]:
    """Fetch the full unfiltered chain for *underlying*, keyed by bare OCC symbol.

    Keys are Alpaca's bare OCC symbols (e.g. ``MU260612P01062500`` — no
    Polygon ``O:`` prefix). Bid/ask come from ``latest_quote``, last price
    from ``latest_trade``; each is ``None`` when the sub-object is absent.
    """
    client = _client if _client is not None else make_option_client()
    chain = client.get_option_chain(
        OptionChainRequest(underlying_symbol=underlying, feed=_OPTIONS_FEED)
    )
    quotes: dict[str, OptionQuote] = {}
    for symbol, snap in chain.items():
        lq = snap.latest_quote
        lt = snap.latest_trade
        quotes[symbol] = OptionQuote(
            bid=float(lq.bid_price) if lq is not None and lq.bid_price is not None else None,
            ask=float(lq.ask_price) if lq is not None and lq.ask_price is not None else None,
            last_price=float(lt.price) if lt is not None and lt.price is not None else None,
        )
    return quotes


def fetch_underlying_trades(tickers: list[str], *, _client: Any = None) -> dict[str, float]:
    """Latest IEX trade price per ticker via one multi-symbol request.

    Tickers without a positive-price trade in the response are omitted, so
    callers fall back to ``None`` for them. The IEX feed is the same free
    surface the continuous monitor's safety-core price feed polls.
    """
    if not tickers:
        return {}
    client = _client if _client is not None else make_stock_client()
    response = client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=tickers, feed=DataFeed.IEX)
    )
    out: dict[str, float] = {}
    for ticker in tickers:
        price = getattr(response.get(ticker), "price", None)
        if price is not None and float(price) > 0:
            out[ticker] = float(price)
    return out
