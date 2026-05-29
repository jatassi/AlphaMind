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


class _RaisingDataClient:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> dict[str, object]:
        raise self._exc


class _NonDictDataClient:
    """Returns a successful-but-unexpected (non-dict) response."""

    def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> object:
        return ["not", "a", "dict"]


@pytest.mark.asyncio
async def test_latest_quotes_batch_maps_keyed_response() -> None:
    """One API call carries the whole symbol list; the keyed response is mapped
    per symbol into Decimal :class:`TouchQuote` (ALP-753)."""
    client = _FakeDataClient(
        {
            "SCHW": SimpleNamespace(bid_price=85.10, ask_price=85.14),
            "ORCL": SimpleNamespace(bid_price=226.00, ask_price=226.20),
        }
    )
    source = _source(client)

    quotes = await source.latest_quotes(["SCHW", "ORCL"])

    assert quotes == {
        "SCHW": TouchQuote(bid=price("85.10"), ask=price("85.14")),
        "ORCL": TouchQuote(bid=price("226.00"), ask=price("226.20")),
    }
    # A single batched request carries the full symbol list on the IEX feed.
    (req,) = client.requests
    assert req.symbol_or_symbols == ["SCHW", "ORCL"]
    assert req.feed == DataFeed.IEX


@pytest.mark.asyncio
async def test_latest_quotes_drops_missing_one_sided_and_zero() -> None:
    """A symbol that is absent, one-sided, or zero/negative is dropped from the
    batch result (same per-symbol semantics as :meth:`latest_quote`), so the
    caller falls back to a recorded reference for it (ALP-753)."""
    client = _FakeDataClient(
        {
            "GOOD": SimpleNamespace(bid_price=10.00, ask_price=10.02),
            "ZERO": SimpleNamespace(bid_price=0.0, ask_price=10.02),
            "ONESIDED": SimpleNamespace(bid_price=10.00, ask_price=None),
            # "MISSING" intentionally absent from the keyed response.
        }
    )
    source = _source(client)

    quotes = await source.latest_quotes(["GOOD", "ZERO", "ONESIDED", "MISSING"])

    assert quotes == {"GOOD": TouchQuote(bid=price("10.00"), ask=price("10.02"))}


@pytest.mark.asyncio
async def test_latest_quotes_empty_symbols_makes_no_call() -> None:
    """An empty symbol list short-circuits to ``{}`` without hitting the SDK."""
    client = _FakeDataClient({})
    source = _source(client)

    assert await source.latest_quotes([]) == {}
    assert client.requests == []


@pytest.mark.asyncio
async def test_latest_quotes_raises_runtimeerror_on_broker_failure() -> None:
    """A whole-batch broker failure raises ``RuntimeError`` (unlike the singular
    :meth:`latest_quote`, which returns ``None``) so the phase-1 caller can
    degrade the universe layer and flip ``staleness_flag`` (ALP-753)."""
    source = _source(cast("_FakeDataClient", _RaisingDataClient(ConnectionError("feed down"))))

    with pytest.raises(RuntimeError):
        await source.latest_quotes(["SCHW"])


@pytest.mark.asyncio
async def test_latest_quotes_raises_runtimeerror_on_non_dict_response() -> None:
    """A successful-but-non-dict batch response degrades conservatively (raises
    ``RuntimeError`` → phase-1 falls back to bars + flips staleness) rather than
    silently returning an empty map as if every quote were dropped (ALP-753)."""
    source = _source(cast("_FakeDataClient", _NonDictDataClient()))

    with pytest.raises(RuntimeError):
        await source.latest_quotes(["SCHW"])
