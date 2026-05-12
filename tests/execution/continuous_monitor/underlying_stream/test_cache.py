"""Tests for ``UnderlyingPriceCache`` (story 02b / ALP-434).

The cache is the typed reader the breach loop (03b), greeks refresh (03a), and
options bracket-stop watcher (04c) consume. Writers (the stream consumer task)
call ``update`` with each freshly-translated quote; readers call ``get`` for a
single ticker or ``get_all`` for a snapshot.

The cache is asyncio-native — concurrent ``update`` calls must serialise via an
``asyncio.Lock`` so last-writer-wins observation is well-defined under
high-volume IEX traffic. ``get`` is lock-free: it returns the current dict
contents at the moment of call.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from alphamind.execution.continuous_monitor.underlying_stream import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)


def _quote(ticker: str, price: float, as_of: datetime | None = None) -> UnderlyingQuote:
    return UnderlyingQuote(
        ticker=ticker,
        price=price,
        as_of=as_of or datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC),
    )


class TestUnderlyingQuoteSchema:
    def test_is_frozen(self) -> None:
        q = _quote("SPY", 500.0)
        with pytest.raises((AttributeError, TypeError)):
            q.price = 501.0  # type: ignore[misc]

    def test_naive_as_of_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            UnderlyingQuote(
                ticker="SPY",
                price=500.0,
                as_of=datetime(2026, 5, 11, 14, 30, 0),  # noqa: DTZ001 — test
            )


class TestUnderlyingPriceCacheBasics:
    async def test_get_missing_returns_none(self) -> None:
        cache = UnderlyingPriceCache()
        assert cache.get("SPY") is None

    async def test_update_then_get_returns_latest_quote(self) -> None:
        cache = UnderlyingPriceCache()
        q = _quote("SPY", 500.25)
        await cache.update(q)
        assert cache.get("SPY") == q

    async def test_update_replaces_prior_quote(self) -> None:
        cache = UnderlyingPriceCache()
        ts_old = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
        ts_new = datetime(2026, 5, 11, 14, 30, 1, tzinfo=UTC)
        await cache.update(_quote("SPY", 500.0, ts_old))
        await cache.update(_quote("SPY", 500.5, ts_new))
        latest = cache.get("SPY")
        assert latest is not None
        assert latest.price == 500.5
        assert latest.as_of == ts_new

    async def test_get_all_snapshot_independent_of_subsequent_writes(self) -> None:
        cache = UnderlyingPriceCache()
        await cache.update(_quote("SPY", 500.0))
        await cache.update(_quote("AAPL", 200.0))
        snapshot = cache.get_all()
        assert set(snapshot) == {"SPY", "AAPL"}

        # Mutating after snapshot does not leak into the prior snapshot.
        await cache.update(_quote("MSFT", 400.0))
        assert "MSFT" not in snapshot
        assert "MSFT" in cache.get_all()


class TestUnderlyingPriceCacheConcurrency:
    async def test_concurrent_updates_preserve_last_writer_wins(self) -> None:
        """Many concurrent writers; final state for each ticker is some valid quote."""
        cache = UnderlyingPriceCache()
        ts_base = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)

        async def writer(ticker: str, price: float) -> None:
            await cache.update(_quote(ticker, price, ts_base))

        await asyncio.gather(
            *(writer(f"T{i}", float(i)) for i in range(50)),
            writer("SPY", 500.0),
            writer("SPY", 501.0),
            writer("SPY", 502.0),
        )
        snapshot = cache.get_all()
        # Every writer's ticker is present.
        assert "SPY" in snapshot
        for i in range(50):
            assert f"T{i}" in snapshot
        # SPY price is one of the values we wrote (last-writer-wins; we can't
        # predict which one wins under cooperative scheduling, but it must be
        # exactly one of the candidates).
        spy = snapshot["SPY"]
        assert spy is not None
        assert spy.price in {500.0, 501.0, 502.0}
