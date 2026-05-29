"""Tests for the Alpaca-backed QuoteSource — ALP-738.

``AlpacaQuoteSource`` wraps a ``StockHistoricalDataClient`` snapshot
(``get_stock_latest_quote``) into the in-process :class:`QuoteSource` the
marketable-entry rewrite consumes, parsing the broker's float bid/ask into
Decimal :class:`Price` at the boundary and returning ``None`` when no usable
two-sided quote is available.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

from alphamind._kernel.money import price
from alphamind.execution.broker_adapter.entry_pricing import TouchQuote
from alphamind.execution.broker_adapter.quotes import AlpacaQuoteSource


class _FakeDataClient:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
        self.requests: list[StockLatestQuoteRequest] = []

    def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> dict[str, object]:
        self.requests.append(request)
        return self._response


def _source(fake: _FakeDataClient) -> AlpacaQuoteSource:
    return AlpacaQuoteSource(cast(StockHistoricalDataClient, fake))


@pytest.mark.asyncio
async def test_latest_quote_parses_bid_ask_into_price() -> None:
    client = _FakeDataClient({"SCHW": SimpleNamespace(bid_price=85.10, ask_price=85.14)})
    source = _source(client)

    quote = await source.latest_quote("SCHW")

    assert quote == TouchQuote(bid=price("85.10"), ask=price("85.14"))
    # Requests the IEX feed for the asked symbol.
    (req,) = client.requests
    assert req.symbol_or_symbols == "SCHW"
    assert req.feed == DataFeed.IEX


@pytest.mark.asyncio
async def test_latest_quote_none_when_symbol_absent() -> None:
    source = _source(_FakeDataClient({}))
    assert await source.latest_quote("SCHW") is None


@pytest.mark.asyncio
async def test_latest_quote_none_when_quote_one_sided_or_zero() -> None:
    source = _source(_FakeDataClient({"SCHW": SimpleNamespace(bid_price=0.0, ask_price=85.14)}))
    assert await source.latest_quote("SCHW") is None
