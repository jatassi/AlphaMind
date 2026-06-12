"""In-memory fake for :class:`PolygonAPI` plus the record dataclasses
that mirror the polygon-api-client SDK shapes consumed by AlphaMind.

The SDK returns objects with attribute access (``snap.details.ticker``),
so the fake records here are :func:`dataclasses.dataclass` to provide the
same access pattern without depending on the real SDK in tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Record shapes (attribute-bearing fakes of the SDK return types)
# ---------------------------------------------------------------------------


@dataclass
class FakeAgg:
    timestamp: int
    open: float = 100.0
    high: float = 105.0
    low: float = 99.0
    close: float = 102.0
    volume: int = 1_000_000
    vwap: float = 101.5
    transactions: int = 5_000


@dataclass
class FakeContractDetails:
    ticker: str
    expiration_date: str
    strike_price: float
    contract_type: str


@dataclass
class FakeGreeks:
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


@dataclass
class FakeDay:
    volume: int | None = None


@dataclass
class FakeQuote:
    bid: float | None = None
    ask: float | None = None


@dataclass
class FakeTrade:
    price: float | None = None


@dataclass
class FakeUnderlyingAsset:
    ticker: str
    price: float | None = None


@dataclass
class FakeOptionSnapshot:
    details: FakeContractDetails
    greeks: FakeGreeks
    day: FakeDay
    last_quote: FakeQuote
    last_trade: FakeTrade
    underlying_asset: FakeUnderlyingAsset
    implied_volatility: float | None = None
    open_interest: int | None = None
    rho: float | None = None


@dataclass
class FakeDividend:
    id: str
    ticker: str
    cash_amount: float
    ex_dividend_date: str
    record_date: str
    pay_date: str
    declaration_date: str
    dividend_type: str = "CD"


@dataclass
class FakeSplit:
    id: str
    ticker: str
    execution_date: str
    split_from: float
    split_to: float


@dataclass
class FakeTickerDetails:
    ticker: str
    market_cap: float | None
    weighted_shares_outstanding: int | None
    share_class_shares_outstanding: int | None
    name: str = ""
    active: bool = True
    delisted_utc: str | None = None


# ---------------------------------------------------------------------------
# The Fake API itself
# ---------------------------------------------------------------------------


@dataclass
class FakePolygonAPI:
    """Stateful fake implementing :class:`PolygonAPI`."""

    # Maps for the per-method "data shape" approach
    aggs_responses: list[Sequence[FakeAgg]] = field(default_factory=list)
    """Successive ``get_aggs`` calls consume entries from this list in
    order.  Defaults to ``[]``: empty list per call when exhausted."""

    aggs_handler: Callable[..., Sequence[FakeAgg]] | None = None
    """Optional callable overriding :attr:`aggs_responses` for stateful
    pagination / per-ticker dispatch."""

    snapshots_by_underlying: dict[str, Sequence[FakeOptionSnapshot]] = field(default_factory=dict)
    dividends_by_ticker: dict[str, Sequence[FakeDividend]] = field(default_factory=dict)
    splits_by_ticker: dict[str, Sequence[FakeSplit]] = field(default_factory=dict)
    ticker_details: dict[str, FakeTickerDetails] = field(default_factory=dict)

    connectivity: bool = True

    # Call-tracking for assertions
    aggs_calls: list[dict[str, Any]] = field(default_factory=list)
    dividend_calls: list[str] = field(default_factory=list)
    split_calls: list[str] = field(default_factory=list)
    ticker_detail_calls: list[str] = field(default_factory=list)
    snapshot_calls: list[str] = field(default_factory=list)
    rate_limit_acquisitions: int = 0

    def __post_init__(self) -> None:
        self._aggs_index = 0

    # ------------------------------------------------------------------
    # PolygonAPI methods
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def acquire_rate_limit(self) -> None:
        self.rate_limit_acquisitions += 1

    def get_aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_: str,
        to: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 50_000,
    ) -> list[object]:
        call = {
            "ticker": ticker,
            "multiplier": multiplier,
            "timespan": timespan,
            "from_": from_,
            "to": to,
            "adjusted": adjusted,
            "sort": sort,
            "limit": limit,
        }
        self.aggs_calls.append(call)
        if self.aggs_handler is not None:
            return list(self.aggs_handler(**call))
        if not self.aggs_responses:
            return []
        idx = self._aggs_index % len(self.aggs_responses)
        self._aggs_index += 1
        return list(self.aggs_responses[idx])

    def list_snapshot_options_chain(self, underlying: str) -> list[object]:
        self.snapshot_calls.append(underlying)
        return list(self.snapshots_by_underlying.get(underlying, []))

    def list_dividends(self, ticker: str, ex_dividend_date_gte: str | None = None) -> list[object]:
        self.dividend_calls.append(ticker)
        return list(self.dividends_by_ticker.get(ticker, []))

    def list_splits(self, ticker: str, execution_date_gte: str | None = None) -> list[object]:
        self.split_calls.append(ticker)
        return list(self.splits_by_ticker.get(ticker, []))

    def get_ticker_details(self, ticker: str) -> object:
        self.ticker_detail_calls.append(ticker)
        details = self.ticker_details.get(ticker)
        if details is None:
            # Polygon returns details for any valid ticker; an unconfigured
            # ticker stands in for a still-listed name (active, not delisted).
            return FakeTickerDetails(
                ticker=ticker,
                market_cap=None,
                weighted_shares_outstanding=None,
                share_class_shares_outstanding=None,
            )
        return details


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


_SNAP_DEFAULTS = {
    "contract_ticker": "O:AAPL260117C00200000",
    "underlying": "AAPL",
    "exp_date": "2026-01-17",
    "strike": 200.0,
    "contract_type": "call",
    "oi": 1000,
    "volume": 500,
    "bid": 5.0,
    "ask": 5.5,
    "last_price": 5.25,
    "iv": 0.30,
    "delta": 0.45,
    "gamma": 0.02,
    "theta": -0.05,
    "vega": 0.10,
    "rho": 0.01,
    "underlying_price": 195.0,
}


def make_option_snapshot(**overrides: Any) -> FakeOptionSnapshot:
    v: dict[str, Any] = {**_SNAP_DEFAULTS, **overrides}
    return FakeOptionSnapshot(
        details=FakeContractDetails(
            ticker=v["contract_ticker"],
            expiration_date=v["exp_date"],
            strike_price=v["strike"],
            contract_type=v["contract_type"],
        ),
        greeks=FakeGreeks(
            delta=v["delta"],
            gamma=v["gamma"],
            theta=v["theta"],
            vega=v["vega"],
        ),
        day=FakeDay(volume=v["volume"]),
        last_quote=FakeQuote(bid=v["bid"], ask=v["ask"]),
        last_trade=FakeTrade(price=v["last_price"]),
        underlying_asset=FakeUnderlyingAsset(ticker=v["underlying"], price=v["underlying_price"]),
        implied_volatility=v["iv"],
        open_interest=v["oi"],
        rho=v["rho"],
    )


def make_dividend(
    ticker: str = "AAPL",
    div_id: str = "D001",
    cash_amount: float = 0.25,
    ex_date: str = "2026-03-01",
    record_date: str = "2026-03-02",
    pay_date: str = "2026-03-15",
    declaration_date: str = "2026-02-15",
    dividend_type: str = "CD",
) -> FakeDividend:
    return FakeDividend(
        id=div_id,
        ticker=ticker,
        cash_amount=cash_amount,
        ex_dividend_date=ex_date,
        record_date=record_date,
        pay_date=pay_date,
        declaration_date=declaration_date,
        dividend_type=dividend_type,
    )


def make_split(
    ticker: str = "AAPL",
    split_id: str = "S001",
    execution_date: str = "2026-02-01",
    split_from: float = 1.0,
    split_to: float = 4.0,
) -> FakeSplit:
    return FakeSplit(
        id=split_id,
        ticker=ticker,
        execution_date=execution_date,
        split_from=split_from,
        split_to=split_to,
    )


def make_ticker_details(
    ticker: str = "AAPL",
    market_cap: float = 3_000_000_000_000.0,
    shares_outstanding: int = 15_000_000_000,
    float_shares: int = 14_800_000_000,
    name: str = "Apple Inc.",
    active: bool = True,
    delisted_utc: str | None = None,
) -> FakeTickerDetails:
    return FakeTickerDetails(
        ticker=ticker,
        market_cap=market_cap,
        weighted_shares_outstanding=shares_outstanding,
        share_class_shares_outstanding=float_shares,
        name=name,
        active=active,
        delisted_utc=delisted_utc,
    )


def make_agg(
    ts: int,
    o: float = 100.0,
    h: float = 105.0,
    lo: float = 99.0,
    c: float = 102.0,
    v: int = 1_000_000,
    vw: float = 101.5,
    n: int = 5_000,
) -> FakeAgg:
    return FakeAgg(
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
        vwap=vw,
        transactions=n,
    )
