"""Tests for :class:`AlpacaMarginCallObserver` (ALP-453).

The observer probes the broker's ``GET /v2/account`` surface on each tick
and surfaces a :class:`MarginCallEvent` when the account is in a
maintenance-margin call state (``equity < maintenance_margin``). Production
wires this as the breach loop's ``margin_call_observer`` so the
emergency-trigger evaluator's margin-call branch fires from live broker
state instead of the test-only :class:`NoMarginCallObserver`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from alphamind.execution.broker_adapter.queries import TradeAccountSnapshot
from alphamind.execution.continuous_monitor.emergency_trigger.margin_call_observer import (
    AlpacaMarginCallObserver,
)


@dataclass
class _FakeAccountQueries:
    """Minimal stand-in for :class:`AccountStateQueries`.

    The observer only consumes :meth:`get_account`, so the fake exposes
    exactly that surface. Each test injects the snapshot it wants.
    """

    snapshot: TradeAccountSnapshot

    def get_account(self) -> TradeAccountSnapshot:
        return self.snapshot


def _snapshot(*, equity: float, maintenance_margin: float) -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="acct-1",
        cash=0.0,
        equity=equity,
        buying_power=0.0,
        regt_buying_power=0.0,
        daytrading_buying_power=0.0,
        maintenance_margin=maintenance_margin,
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


_FIXED_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _FIXED_NOW


@pytest.mark.asyncio
async def test_returns_none_when_equity_exceeds_maintenance_margin() -> None:
    """``equity > maintenance_margin`` → no margin call outstanding."""
    queries = _FakeAccountQueries(_snapshot(equity=100_000.0, maintenance_margin=25_000.0))
    observer = AlpacaMarginCallObserver(queries=queries, now=_fixed_clock)
    assert await observer() is None


@pytest.mark.asyncio
async def test_returns_none_when_equity_equals_maintenance_margin() -> None:
    """Boundary condition: equal values are NOT a margin call.

    Alpaca's contract: a margin call fires when maintenance_margin strictly
    exceeds equity. Equality is the boundary the account stays solvent at.
    """
    queries = _FakeAccountQueries(_snapshot(equity=50_000.0, maintenance_margin=50_000.0))
    observer = AlpacaMarginCallObserver(queries=queries, now=_fixed_clock)
    assert await observer() is None


@pytest.mark.asyncio
async def test_returns_margin_call_event_when_equity_below_maintenance_margin() -> None:
    """``equity < maintenance_margin`` → :class:`MarginCallEvent` with the deficit."""
    queries = _FakeAccountQueries(_snapshot(equity=40_000.0, maintenance_margin=55_000.0))
    observer = AlpacaMarginCallObserver(queries=queries, now=_fixed_clock)
    event = await observer()
    assert event is not None
    assert event.additional_margin_required_usd == pytest.approx(15_000.0)
    assert event.issued_at == _FIXED_NOW


@pytest.mark.asyncio
async def test_now_defaults_to_datetime_now_utc() -> None:
    """Without an injected clock, ``issued_at`` is ``datetime.now(UTC)``."""
    queries = _FakeAccountQueries(_snapshot(equity=10_000.0, maintenance_margin=12_000.0))
    observer = AlpacaMarginCallObserver(queries=queries)
    before = datetime.now(UTC)
    event = await observer()
    after = datetime.now(UTC)
    assert event is not None
    assert before <= event.issued_at <= after


@pytest.mark.asyncio
async def test_polls_queries_each_call() -> None:
    """Each call reads fresh broker state — no caching across ticks.

    The fake's ``get_account`` runs per-call, so the observer surfaces the
    latest snapshot every time. The broker's account state can change
    mid-session (deposits, liquidations); the observer is stateless.
    """

    @dataclass
    class _CountingQueries:
        calls: int = 0

        def get_account(self) -> TradeAccountSnapshot:
            self.calls += 1
            return _snapshot(equity=10_000.0, maintenance_margin=15_000.0)

    queries = _CountingQueries()
    observer = AlpacaMarginCallObserver(queries=queries, now=_fixed_clock)
    await observer()
    await observer()
    await observer()
    assert queries.calls == 3
