"""Tests for src/alphamind/data_sources/alpaca/options.py — ALP-949.

All alpaca-py SDK calls are routed through the fakes in
``tests/data_sources/_fakes/alpaca.py``. No real network traffic.
"""

from __future__ import annotations

from tests.data_sources._fakes.alpaca import (
    FakeAlpacaOptionClient,
    FakeAlpacaStockClient,
    FakeOptionQuoteRecord,
    FakeOptionSnapshotRecord,
    FakeOptionTradeRecord,
    FakeStockTradeRecord,
)

# ---------------------------------------------------------------------------
# fetch_chain_quotes
# ---------------------------------------------------------------------------


class TestFetchChainQuotes:
    def test_keys_by_bare_occ_and_maps_quote_fields(self) -> None:
        from alphamind.data_sources.alpaca.options import OptionQuote, fetch_chain_quotes

        client = FakeAlpacaOptionClient(
            chains_by_underlying={
                "MU": {
                    "MU260612P01062500": FakeOptionSnapshotRecord(
                        latest_quote=FakeOptionQuoteRecord(bid_price=1.05, ask_price=1.15),
                        latest_trade=FakeOptionTradeRecord(price=1.10),
                    ),
                    "MU260612C01062500": FakeOptionSnapshotRecord(),
                }
            }
        )

        quotes = fetch_chain_quotes("MU", _client=client)

        assert quotes == {
            "MU260612P01062500": OptionQuote(bid=1.05, ask=1.15, last_price=1.10),
            "MU260612C01062500": OptionQuote(bid=None, ask=None, last_price=None),
        }

    def test_requests_full_chain_on_the_indicative_feed(self) -> None:
        """The request sent to the vendor carries the underlying and the
        indicative feed (the only options feed entitled on the free plan)
        with no strike/expiration narrowing — the merge needs the full chain."""
        from alpaca.data.enums import OptionsFeed

        from alphamind.data_sources.alpaca.options import fetch_chain_quotes

        client = FakeAlpacaOptionClient()

        fetch_chain_quotes("NVDA", _client=client)

        (request,) = client.chain_calls
        assert request.underlying_symbol == "NVDA"
        assert request.feed == OptionsFeed.INDICATIVE
        assert request.strike_price_gte is None
        assert request.strike_price_lte is None
        assert request.expiration_date is None


# ---------------------------------------------------------------------------
# fetch_underlying_trades
# ---------------------------------------------------------------------------


class TestFetchUnderlyingTrades:
    def test_one_multi_symbol_iex_request_maps_prices_and_omits_missing(self) -> None:
        from alpaca.data.enums import DataFeed

        from alphamind.data_sources.alpaca.options import fetch_underlying_trades

        client = FakeAlpacaStockClient(
            trades_by_symbol={
                "MU": FakeStockTradeRecord(price=106.25),
                "NVDA": FakeStockTradeRecord(price=None),
            }
        )

        spots = fetch_underlying_trades(["MU", "NVDA", "AAPL"], _client=client)

        assert spots == {"MU": 106.25}
        (request,) = client.trade_calls
        assert request.symbol_or_symbols == ["MU", "NVDA", "AAPL"]
        assert request.feed == DataFeed.IEX

    def test_empty_ticker_list_makes_no_request(self) -> None:
        from alphamind.data_sources.alpaca.options import fetch_underlying_trades

        client = FakeAlpacaStockClient()

        assert fetch_underlying_trades([], _client=client) == {}
        assert client.trade_calls == []
