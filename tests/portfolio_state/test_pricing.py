"""Tests for pricing.py — CurrentPriceProvider protocol, PriceQuote, PriceSource."""
# mypy: disable-error-code="arg-type,call-arg,dict-item,misc,no-untyped-def,no-untyped-call,unused-ignore,no-any-return,var-annotated"

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.portfolio_state.pricing import (
    CurrentPriceProvider,
    PriceQuote,
    PriceSource,
    StubCurrentPriceProvider,
    UnknownTickerError,
)


def _quote(
    ticker: str = "AAPL",
    price_usd: float = 150.0,
    as_of: datetime | None = None,
    source: PriceSource = PriceSource.OHLCV_CLOSE,
    is_stale: bool = False,
) -> PriceQuote:
    if as_of is None:
        as_of = datetime.now(UTC)
    return PriceQuote(
        ticker=ticker,
        price_usd=price_usd,
        as_of_timestamp=as_of,
        source=source,
        is_stale=is_stale,
    )


# ---------------------------------------------------------------------------
# PriceSource
# ---------------------------------------------------------------------------


def test_price_source_members() -> None:
    members = {m.name for m in PriceSource}
    assert members == {"INTRADAY_QUOTE", "OHLCV_CLOSE", "STALE_FALLBACK"}


# ---------------------------------------------------------------------------
# PriceQuote
# ---------------------------------------------------------------------------


def test_price_quote_rejects_zero_price() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _quote(price_usd=0.0)


def test_price_quote_rejects_negative_price() -> None:
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        _quote(price_usd=-1.0)


def test_price_quote_accepts_positive_price() -> None:
    q = _quote(price_usd=0.01)
    assert q.price_usd == 0.01


def test_price_quote_is_frozen() -> None:
    q = _quote()
    with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
        q.price_usd = 999.0


def test_price_quote_fields() -> None:
    now = datetime.now(UTC)
    q = PriceQuote(
        ticker=Symbol("TSLA"),
        price_usd=200.0,
        as_of_timestamp=now,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )
    assert q.ticker == "TSLA"
    assert q.price_usd == 200.0
    assert q.as_of_timestamp == now
    assert q.source == PriceSource.INTRADAY_QUOTE
    assert q.is_stale is False


# ---------------------------------------------------------------------------
# UnknownTickerError
# ---------------------------------------------------------------------------


def test_unknown_ticker_error_is_value_error() -> None:
    err = UnknownTickerError("XYZ")
    assert isinstance(err, ValueError)


# ---------------------------------------------------------------------------
# StubCurrentPriceProvider
# ---------------------------------------------------------------------------


def _make_stub(
    ticker: str = "AAPL",
    price_usd: float = 150.0,
    age_seconds: float = 60.0,
    now: datetime | None = None,
) -> tuple[StubCurrentPriceProvider, datetime]:
    """Return (stub, now) with one quote whose as_of is `age_seconds` before `now`."""
    if now is None:
        now = datetime.now(UTC)
    as_of = now - timedelta(seconds=age_seconds)
    quote = _quote(ticker=ticker, price_usd=price_usd, as_of=as_of)
    stub = StubCurrentPriceProvider(quotes={ticker: quote}, now=now)
    return stub, now


def test_get_quote_fresh_returns_is_stale_false() -> None:
    stub, _ = _make_stub(age_seconds=30.0)
    result = stub.get_quote("AAPL", freshness_threshold_seconds=60.0)
    assert result.is_stale is False


def test_get_quote_stale_returns_is_stale_true() -> None:
    stub, _ = _make_stub(age_seconds=120.0)
    result = stub.get_quote("AAPL", freshness_threshold_seconds=60.0)
    assert result.is_stale is True


def test_get_quote_unknown_ticker_raises() -> None:
    stub, _ = _make_stub()
    with pytest.raises(UnknownTickerError):
        stub.get_quote("UNKNOWN", freshness_threshold_seconds=60.0)


def test_get_quote_does_not_mutate_backing_dict() -> None:
    now = datetime.now(UTC)
    as_of = now - timedelta(seconds=120.0)
    original_quote = _quote(ticker=Symbol("AAPL"), as_of=as_of, is_stale=False)
    quotes: dict[str, PriceQuote] = {"AAPL": original_quote}
    stub = StubCurrentPriceProvider(quotes=quotes, now=now)
    result = stub.get_quote("AAPL", freshness_threshold_seconds=60.0)
    assert result.is_stale is True
    assert quotes["AAPL"].is_stale is False


def test_get_quotes_omits_unknown_tickers() -> None:
    stub, _ = _make_stub(ticker=Symbol("AAPL"))
    result = stub.get_quotes(("AAPL", "UNKNOWN"), freshness_threshold_seconds=60.0)
    assert "AAPL" in result
    assert "UNKNOWN" not in result


def test_get_quotes_does_not_raise_for_unknown() -> None:
    stub, _ = _make_stub(ticker=Symbol("AAPL"))
    result = stub.get_quotes(("UNKNOWN1", "UNKNOWN2"), freshness_threshold_seconds=60.0)
    assert result == {}


def test_get_quotes_returns_staleness_per_ticker() -> None:
    now = datetime.now(UTC)
    fresh_as_of = now - timedelta(seconds=10.0)
    stale_as_of = now - timedelta(seconds=200.0)
    quotes = {
        "FRESH": _quote(ticker=Symbol("FRESH"), as_of=fresh_as_of),
        "STALE": _quote(ticker=Symbol("STALE"), as_of=stale_as_of),
    }
    stub = StubCurrentPriceProvider(quotes=quotes, now=now)
    result = stub.get_quotes(("FRESH", "STALE"), freshness_threshold_seconds=60.0)
    assert result["FRESH"].is_stale is False
    assert result["STALE"].is_stale is True


# ---------------------------------------------------------------------------
# Protocol runtime check
# ---------------------------------------------------------------------------


def test_isinstance_stub_is_current_price_provider() -> None:
    stub, _ = _make_stub()
    assert isinstance(stub, CurrentPriceProvider)
