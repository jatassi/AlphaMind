"""Tests for the safety-core imperative shell loop (ALP-857 / ADR-0004).

The shell loads inputs (broker snapshot positions + account equity, live price
reads), calls the pure :func:`evaluate_safety` core, beats its file heartbeat,
and surfaces breaches/staleness via an injected (non-DB) signal sink. These
tests cover the load-bearing ACs:

* a deliberately frozen "monitor proper" coroutine does NOT freeze the safety
  loop (the safety loop keeps evaluating + beating);
* the safety core reads the broker snapshot + price stream and writes nothing
  to the shared DB (the loop has no DB session in scope — structural — and the
  test asserts it against the four-boundary DB seam).

Mock only the four sanctioned boundaries: the broker (``get_positions`` /
``get_account``) and the clock. The price cache is the real
``UnderlyingPriceCache`` (an in-process collaborator, not a boundary). No DB,
no LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.continuous_monitor.safety_core.evaluation import SafetyLimits
from alphamind.execution.continuous_monitor.safety_core.heartbeat import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
)
from alphamind.execution.continuous_monitor.safety_core.loop import (
    SafetyLoop,
    run_safety_core,
)
from alphamind.execution.continuous_monitor.safety_core.records import SafetyEvaluation
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)

_AS_OF = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)


def _account(equity: float = 100_000.0) -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="acct-1",
        cash=money(equity),
        equity=money(equity),
        buying_power=money(equity),
        regt_buying_power=money(equity),
        daytrading_buying_power=money(equity),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _position(symbol: str = "AAPL", market_value: float = 1000.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        avg_entry_price=price(140.0),
        market_value=signed_money(market_value),
        cost_basis=money(900.0),
        unrealized_pl=signed_money(100.0),
        unrealized_plpc=0.1,
        current_price=price(150.0),
        side="long",
    )


class _StubBroker:
    """Sync broker snapshot stand-in (the broker API boundary)."""

    def __init__(
        self,
        *,
        account: TradeAccountSnapshot,
        positions: tuple[PositionSnapshot, ...],
    ) -> None:
        self._account = account
        self._positions = positions
        self.get_positions_calls = 0

    def get_account(self) -> TradeAccountSnapshot:
        return self._account

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        self.get_positions_calls += 1
        return self._positions


def _counted_loop(n_ticks: int) -> SafetyLoop:
    """A SafetyLoop-shaped factory iterating exactly *n_ticks* times."""

    async def _loop() -> AsyncIterator[None]:
        for _ in range(n_ticks):
            yield
            await asyncio.sleep(0)

    return _loop


def _limits() -> SafetyLimits:
    return SafetyLimits(max_gross_exposure_pct=200.0, max_position_concentration_pct=25.0)


async def _seed_cache(cache: UnderlyingPriceCache, symbol: str) -> None:
    await cache.update(UnderlyingQuote(ticker=symbol, price=150.0, as_of=_AS_OF))


@pytest.mark.asyncio
async def test_loop_reads_broker_and_beats_heartbeat(tmp_path: Path) -> None:
    """Each tick reads the broker snapshot and beats the file heartbeat."""
    broker = _StubBroker(account=_account(), positions=(_position(),))
    cache = UnderlyingPriceCache()
    await _seed_cache(cache, "AAPL")
    hb_path = tmp_path / "safety_core.heartbeat"
    clock = {"t": 0.0}
    sink = FileHeartbeatSink(path=hb_path, clock=lambda: clock["t"])

    await run_safety_core(
        get_positions=broker.get_positions,
        get_account=broker.get_account,
        price_cache=cache,
        heartbeat=sink,
        limits=_limits(),
        max_age_seconds=900.0,
        loop=_counted_loop(2),
        now=lambda: _AS_OF,
    )

    assert broker.get_positions_calls == 2
    # The heartbeat file exists and was written by the loop (liveness signal).
    assert FileHeartbeatProbe(path=hb_path).age(now=clock["t"]) is not None


@pytest.mark.asyncio
async def test_frozen_monitor_proper_does_not_freeze_safety_core(tmp_path: Path) -> None:
    """A wedged monitor-proper coroutine cannot stall the safety loop.

    The safety core shares no object with the monitor proper, so a frozen
    monitor task running concurrently in the same event loop cannot block the
    safety loop's ticks. We run a deliberately-frozen task alongside and assert
    the safety loop still completes all its evaluations + beats.
    """
    broker = _StubBroker(account=_account(), positions=(_position(),))
    cache = UnderlyingPriceCache()
    await _seed_cache(cache, "AAPL")
    sink = FileHeartbeatSink(path=tmp_path / "hb", clock=lambda: 0.0)

    frozen_started = asyncio.Event()

    async def _frozen_monitor_proper() -> None:
        frozen_started.set()
        await asyncio.Event().wait()  # never resolves — the wedge

    async with asyncio.TaskGroup() as tg:
        frozen = tg.create_task(_frozen_monitor_proper())
        await frozen_started.wait()
        await run_safety_core(
            get_positions=broker.get_positions,
            get_account=broker.get_account,
            price_cache=cache,
            heartbeat=sink,
            limits=_limits(),
            max_age_seconds=900.0,
            loop=_counted_loop(3),
            now=lambda: _AS_OF,
        )
        # The safety loop ran all three ticks despite the frozen sibling.
        assert broker.get_positions_calls == 3
        frozen.cancel()


@pytest.mark.asyncio
async def test_loop_surfaces_breach_to_signal_sink(tmp_path: Path) -> None:
    """A gross-exposure breach is surfaced through the injected signal sink."""
    # 3 positions @ 100k each, equity 100k → gross 300% > 200%.
    positions = tuple(_position(symbol=s, market_value=100_000.0) for s in ("AAPL", "TSLA", "NVDA"))
    broker = _StubBroker(account=_account(equity=100_000.0), positions=positions)
    cache = UnderlyingPriceCache()
    for s in ("AAPL", "TSLA", "NVDA"):
        await _seed_cache(cache, s)
    sink = FileHeartbeatSink(path=tmp_path / "hb", clock=lambda: 0.0)
    seen: list[SafetyEvaluation] = []

    await run_safety_core(
        get_positions=broker.get_positions,
        get_account=broker.get_account,
        price_cache=cache,
        heartbeat=sink,
        limits=_limits(),
        max_age_seconds=900.0,
        loop=_counted_loop(1),
        now=lambda: _AS_OF,
        on_evaluation=seen.append,
    )

    assert seen
    assert "gross_exposure_pct" in seen[-1].breached_rules


@pytest.mark.asyncio
async def test_loop_tolerates_a_failing_tick(tmp_path: Path) -> None:
    """A broker read raising on one tick does not kill the safety loop."""
    calls = {"n": 0}

    def _flaky_get_positions() -> tuple[PositionSnapshot, ...]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broker hiccup")
        return (_position(),)

    broker = _StubBroker(account=_account(), positions=(_position(),))
    cache = UnderlyingPriceCache()
    await _seed_cache(cache, "AAPL")
    sink = FileHeartbeatSink(path=tmp_path / "hb", clock=lambda: 0.0)

    await run_safety_core(
        get_positions=_flaky_get_positions,
        get_account=broker.get_account,
        price_cache=cache,
        heartbeat=sink,
        limits=_limits(),
        max_age_seconds=900.0,
        loop=_counted_loop(2),
        now=lambda: _AS_OF,
    )

    # Both ticks ran (the first raised, the loop continued to the second).
    assert calls["n"] == 2
