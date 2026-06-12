"""In-memory fakes for the alpaca-py market-data clients consumed by the
options collector (ALP-949).

The SDK returns mapping responses keyed by symbol whose values carry
attribute access (``snap.latest_quote.bid_price``), so the fakes here are
:func:`dataclasses.dataclass` records mirroring those shapes without
depending on the real SDK in tests. Request objects are the real SDK
pydantic models (no network is involved in constructing them); the fake
clients read the fields the production code sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Record shapes (attribute-bearing fakes of the SDK return types)
# ---------------------------------------------------------------------------


@dataclass
class FakeOptionQuoteRecord:
    bid_price: float | None = None
    ask_price: float | None = None


@dataclass
class FakeOptionTradeRecord:
    price: float | None = None


@dataclass
class FakeOptionSnapshotRecord:
    latest_quote: FakeOptionQuoteRecord | None = None
    latest_trade: FakeOptionTradeRecord | None = None


@dataclass
class FakeStockTradeRecord:
    price: float | None = None


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


@dataclass
class FakeAlpacaOptionClient:
    """Fake of ``OptionHistoricalDataClient`` keyed by underlying symbol."""

    chains_by_underlying: dict[str, dict[str, FakeOptionSnapshotRecord]] = field(
        default_factory=dict
    )
    chain_calls: list[Any] = field(default_factory=list)

    def get_option_chain(self, request_params: Any) -> dict[str, FakeOptionSnapshotRecord]:
        self.chain_calls.append(request_params)
        return dict(self.chains_by_underlying.get(request_params.underlying_symbol, {}))


@dataclass
class FakeAlpacaStockClient:
    """Fake of ``StockHistoricalDataClient`` serving latest trades."""

    trades_by_symbol: dict[str, FakeStockTradeRecord] = field(default_factory=dict)
    trade_calls: list[Any] = field(default_factory=list)

    def get_stock_latest_trade(self, request_params: Any) -> dict[str, FakeStockTradeRecord]:
        self.trade_calls.append(request_params)
        symbols = request_params.symbol_or_symbols
        if isinstance(symbols, str):
            symbols = [symbols]
        return {s: self.trades_by_symbol[s] for s in symbols if s in self.trades_by_symbol}
