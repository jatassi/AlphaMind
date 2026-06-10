"""Tests for the safety-core REST latest-quote price feed (ALP-940).

The safety core's price source moved from a competing market-data websocket to a
REST latest-quote poll (the monitor proper keeps the sole websocket; REST is not
connection-limited). These tests cover:

* :func:`run_price_feed` — poll→cache writes, empty-symbol short-circuit,
  per-cycle resilience to a fetch error, and ``CancelledError`` propagation.
* :func:`_latest_quotes_to_underlying` — mid-price + timestamp passthrough and the
  degenerate-row drop semantics (None / zero / one-sided / missing symbol).
* End-to-end: a mapped old-timestamp quote surfaces the ``globally stale`` alarm
  through the unchanged ``read_all`` → ``_surface`` path.

Mock only the sanctioned boundaries: the broker-data fetch (injected
``fetch_quotes``) and the clock (injected ``sleep``). The cache and the
``run_safety_core`` loop are real in-process collaborators — no patching.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockLatestQuoteRequest

from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.continuous_monitor.safety_core.evaluation import SafetyLimits
from alphamind.execution.continuous_monitor.safety_core.loop import run_safety_core
from alphamind.execution.continuous_monitor.safety_core.price_feed import (
    AlpacaLatestQuoteFetcher,
    _latest_quotes_to_underlying,
    run_price_feed,
)
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.execution.process_supervision.heartbeat import FileHeartbeatSink

_AS_OF = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)


class _StopLoopError(Exception):
    """Sentinel a fake ``sleep`` raises to break ``run_price_feed``'s run-forever loop."""


def _sleep_stopping_after(n_cycles: int) -> tuple[object, dict[str, int]]:
    """A fake ``sleep`` that raises :class:`_StopLoopError` once it has slept *n_cycles* times.

    ``run_price_feed`` sleeps at the END of each cycle, so raising on the *n*-th
    sleep lets exactly *n* fetch→write cycles run first. Returns the sleep plus a
    mutable call counter for assertions.
    """
    state = {"sleeps": 0}

    async def _sleep(_seconds: float) -> None:
        state["sleeps"] += 1
        if state["sleeps"] >= n_cycles:
            raise _StopLoopError

    return _sleep, state


@dataclass
class _FakeQuote:
    """A minimal alpaca-py ``Quote`` stand-in for the mapper (the data boundary)."""

    symbol: str
    bid_price: float | None
    ask_price: float | None
    timestamp: datetime


# ---------------------------------------------------------------------------
# run_price_feed — the poll loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_price_feed_writes_one_cache_update_per_polled_quote() -> None:
    """Each polled quote in a cycle becomes one cache update."""
    cache = UnderlyingPriceCache()
    polled = [
        UnderlyingQuote(ticker="AAPL", price=101.0, as_of=_AS_OF),
        UnderlyingQuote(ticker="TSLA", price=202.0, as_of=_AS_OF),
    ]

    async def _fetch(_symbols: frozenset[str]) -> Iterable[UnderlyingQuote]:
        return polled

    sleep, _state = _sleep_stopping_after(1)

    with pytest.raises(_StopLoopError):
        await run_price_feed(
            get_symbols=lambda: frozenset({"AAPL", "TSLA"}),
            fetch_quotes=_fetch,
            cache=cache,
            cadence_seconds=30.0,
            sleep=sleep,  # type: ignore[arg-type]
        )

    assert cache.get("AAPL") == UnderlyingQuote(ticker="AAPL", price=101.0, as_of=_AS_OF)
    assert cache.get("TSLA") == UnderlyingQuote(ticker="TSLA", price=202.0, as_of=_AS_OF)


@pytest.mark.asyncio
async def test_run_price_feed_skips_fetch_when_no_equity_symbols() -> None:
    """An empty subscribe set short-circuits before the fetch (no API call, no write)."""
    cache = UnderlyingPriceCache()
    fetch_calls = {"n": 0}

    async def _fetch(_symbols: frozenset[str]) -> Iterable[UnderlyingQuote]:
        fetch_calls["n"] += 1
        return []

    sleep, _state = _sleep_stopping_after(1)

    with pytest.raises(_StopLoopError):
        await run_price_feed(
            get_symbols=frozenset,  # zero-arg call → empty frozenset
            fetch_quotes=_fetch,
            cache=cache,
            cadence_seconds=30.0,
            sleep=sleep,  # type: ignore[arg-type]
        )

    assert fetch_calls["n"] == 0
    assert cache.get_all() == {}


@pytest.mark.asyncio
async def test_run_price_feed_survives_a_fetch_error_and_keeps_polling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fetch error in one cycle is caught + logged; a later cycle still writes."""
    cache = UnderlyingPriceCache()
    fetch_calls = {"n": 0}

    async def _fetch(_symbols: frozenset[str]) -> Iterable[UnderlyingQuote]:
        fetch_calls["n"] += 1
        if fetch_calls["n"] == 1:
            raise RuntimeError("transient broker-data error")
        return [UnderlyingQuote(ticker="AAPL", price=150.0, as_of=_AS_OF)]

    sleep, _state = _sleep_stopping_after(2)

    with caplog.at_level(logging.ERROR), pytest.raises(_StopLoopError):
        await run_price_feed(
            get_symbols=lambda: frozenset({"AAPL"}),
            fetch_quotes=_fetch,
            cache=cache,
            cadence_seconds=30.0,
            sleep=sleep,  # type: ignore[arg-type]
        )

    assert fetch_calls["n"] == 2
    assert cache.get("AAPL") == UnderlyingQuote(ticker="AAPL", price=150.0, as_of=_AS_OF)
    assert "price feed cycle failed" in caplog.text


@pytest.mark.asyncio
async def test_run_price_feed_propagates_cancellation() -> None:
    """``CancelledError`` from the fetch is NOT swallowed — supervised shutdown is honored."""
    cache = UnderlyingPriceCache()

    async def _fetch(_symbols: frozenset[str]) -> Iterable[UnderlyingQuote]:
        raise asyncio.CancelledError

    sleep, _state = _sleep_stopping_after(1)

    with pytest.raises(asyncio.CancelledError):
        await run_price_feed(
            get_symbols=lambda: frozenset({"AAPL"}),
            fetch_quotes=_fetch,
            cache=cache,
            cadence_seconds=30.0,
            sleep=sleep,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# _latest_quotes_to_underlying — the pure mapper
# ---------------------------------------------------------------------------


def test_mapper_two_sided_quote_yields_mid_and_timestamp() -> None:
    """A two-sided quote maps to the mid price and the quote's own timestamp."""
    ts = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
    response = {"AAPL": _FakeQuote(symbol="AAPL", bid_price=100.0, ask_price=102.0, timestamp=ts)}

    result = _latest_quotes_to_underlying(response, ["AAPL"])

    assert result == [UnderlyingQuote(ticker="AAPL", price=101.0, as_of=ts)]


def test_mapper_drops_zero_one_sided_and_missing_rows() -> None:
    """Zero-bid, one-sided (None ask), and missing-symbol rows yield no quote."""
    ts = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
    response = {
        "ZERO": _FakeQuote(symbol="ZERO", bid_price=0.0, ask_price=102.0, timestamp=ts),
        "ONESIDED": _FakeQuote(symbol="ONESIDED", bid_price=100.0, ask_price=None, timestamp=ts),
        # "MISSING" is requested but absent from the response.
    }

    result = _latest_quotes_to_underlying(response, ["ZERO", "ONESIDED", "MISSING"])

    assert result == []


def test_mapper_keeps_only_the_two_sided_rows_in_a_mixed_batch() -> None:
    """A mixed batch keeps the live row and drops the degenerate one."""
    ts = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
    response = {
        "AAPL": _FakeQuote(symbol="AAPL", bid_price=100.0, ask_price=102.0, timestamp=ts),
        "BAD": _FakeQuote(symbol="BAD", bid_price=-1.0, ask_price=102.0, timestamp=ts),
    }

    result = _latest_quotes_to_underlying(response, ["AAPL", "BAD"])

    assert result == [UnderlyingQuote(ticker="AAPL", price=101.0, as_of=ts)]


# ---------------------------------------------------------------------------
# AlpacaLatestQuoteFetcher — request building + response mapping (broker boundary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_builds_sorted_iex_request_and_maps_response() -> None:
    """The fetcher passes sorted symbols + IEX feed to the SDK and maps the keyed response.

    The Alpaca ``StockHistoricalDataClient`` is the sanctioned broker boundary —
    faked here. Asserts the glue the unit tests above don't cover: ``sorted(symbols)``
    → ``StockLatestQuoteRequest`` (IEX) → ``_latest_quotes_to_underlying``, including
    the degenerate-row drop on the real fetch path.
    """
    ts = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
    captured: dict[str, StockLatestQuoteRequest] = {}

    class _FakeClient:
        def get_stock_latest_quote(self, request: StockLatestQuoteRequest) -> dict[str, _FakeQuote]:
            captured["request"] = request
            return {
                "AAPL": _FakeQuote(symbol="AAPL", bid_price=100.0, ask_price=102.0, timestamp=ts),
                # Zero-bid row must be dropped on the real fetch path too.
                "MSFT": _FakeQuote(symbol="MSFT", bid_price=0.0, ask_price=50.0, timestamp=ts),
            }

    fetcher = AlpacaLatestQuoteFetcher(client=_FakeClient())  # type: ignore[arg-type]

    result = await fetcher(frozenset({"MSFT", "AAPL"}))

    assert result == [UnderlyingQuote(ticker="AAPL", price=101.0, as_of=ts)]
    request = captured["request"]
    assert request.symbol_or_symbols == ["AAPL", "MSFT"]  # sorted before the request is built
    assert request.feed == DataFeed.IEX


# ---------------------------------------------------------------------------
# End-to-end: mapped old-timestamp quote → globally_stale alarm
# ---------------------------------------------------------------------------


def _position(symbol: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        avg_entry_price=price(140.0),
        market_value=signed_money(1000.0),
        cost_basis=money(900.0),
        unrealized_pl=signed_money(100.0),
        unrealized_plpc=0.1,
        current_price=price(150.0),
        side="long",
    )


def _account() -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="acct-1",
        cash=money(100_000.0),
        equity=money(100_000.0),
        buying_power=money(100_000.0),
        regt_buying_power=money(100_000.0),
        daytrading_buying_power=money(100_000.0),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


@pytest.mark.asyncio
async def test_mapped_old_timestamp_quote_surfaces_globally_stale(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A mapped quote carrying an old timestamp trips the ``globally stale`` alarm.

    The REST path's timestamp passthrough flows unchanged through ``read_all``:
    a quote older than ``max_age_seconds`` reads ``StalePrice``, so the whole feed
    classifies ``globally_stale`` and ``_surface`` logs the operator alarm.
    """
    old_ts = datetime(2026, 6, 5, 13, 0, tzinfo=UTC)  # 1.5h before _AS_OF (> 900s)
    response = {
        "AAPL": _FakeQuote(symbol="AAPL", bid_price=100.0, ask_price=102.0, timestamp=old_ts),
    }

    cache = UnderlyingPriceCache()
    for quote in _latest_quotes_to_underlying(response, ["AAPL"]):
        await cache.update(quote)

    async def _one_tick() -> AsyncIterator[None]:
        yield

    with caplog.at_level(logging.ERROR):
        await run_safety_core(
            get_positions=lambda: (_position("AAPL"),),
            get_account=_account,
            price_cache=cache,
            heartbeat=FileHeartbeatSink(path=tmp_path / "hb", clock=lambda: 0.0),
            limits=SafetyLimits(max_gross_exposure_pct=200.0, max_position_concentration_pct=25.0),
            max_age_seconds=900.0,
            loop=_one_tick,
            now=lambda: _AS_OF,
        )

    assert "globally stale" in caplog.text
