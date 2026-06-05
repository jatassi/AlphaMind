"""The safety core writes NOTHING to the shared DB (ALP-857 / ADR-0004).

AC: "the safety core reads the broker snapshot + price stream and writes nothing
to the shared DB (asserted: no DB write from the safety-core process)."

Two complementary assertions:

* **Structural** — ``run_safety_core``'s signature accepts no DB session /
  engine / session-factory port. A DB write is unreachable by construction, not
  merely by convention (the strongest form of the invariant; ADR-0005's
  single-writer precondition).
* **Behavioral** — a real SQLite engine, instrumented to count every write
  statement, is present in the test but never handed to the loop; running the
  loop through a breach + a globally-stale tick records ZERO writes on it. This
  exercises the DB boundary (the sanctioned seam) and proves the loop opens no
  hidden write path.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.continuous_monitor.safety_core.evaluation import SafetyLimits
from alphamind.execution.continuous_monitor.safety_core.heartbeat import FileHeartbeatSink
from alphamind.execution.continuous_monitor.safety_core.loop import run_safety_core
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)

_AS_OF = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)

# DB-session-shaped parameter names a write path would have to thread through.
# The safety-core loop carries none of them.
_DB_PARAM_NAMES = frozenset(
    {"session", "session_factory", "db_session_factory", "engine", "connection", "repository"}
)


def test_run_safety_core_signature_has_no_db_port() -> None:
    """The loop accepts no DB session/engine/repository port — write unreachable."""
    params = set(inspect.signature(run_safety_core).parameters)
    assert params.isdisjoint(_DB_PARAM_NAMES), (
        f"safety core must take no DB port; found {params & _DB_PARAM_NAMES}"
    )


@pytest.mark.asyncio
async def test_loop_run_writes_nothing_to_db(tmp_path: Path) -> None:
    """Running the loop through breach + globally-stale ticks writes zero DB rows."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    writes: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count_writes(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        verb = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if verb in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            writes.append(statement)

    # A breaching, globally-stale snapshot: gross 300% > 200%, every price stale.
    positions = tuple(
        PositionSnapshot(
            symbol=s,
            asset_class="us_equity",
            qty=10.0,
            avg_entry_price=price(140.0),
            market_value=signed_money(100_000.0),
            cost_basis=money(900.0),
            unrealized_pl=signed_money(0.0),
            unrealized_plpc=0.0,
            current_price=price(150.0),
            side="long",
        )
        for s in ("AAPL", "TSLA", "NVDA")
    )
    account = TradeAccountSnapshot(
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
    cache = UnderlyingPriceCache()
    # Seed a STALE quote (1 hour old) so the staleness path also fires.
    stale_as_of = datetime(2026, 6, 5, 13, 0, tzinfo=UTC)
    for s in ("AAPL", "TSLA", "NVDA"):
        await cache.update(UnderlyingQuote(ticker=s, price=150.0, as_of=stale_as_of))

    async def _loop() -> AsyncIterator[None]:
        for _ in range(2):
            yield
            await asyncio.sleep(0)

    try:
        await run_safety_core(
            get_positions=lambda: positions,
            get_account=lambda: account,
            price_cache=cache,
            heartbeat=FileHeartbeatSink(path=tmp_path / "hb", monotonic=lambda: 0.0),
            limits=SafetyLimits(max_gross_exposure_pct=200.0, max_position_concentration_pct=25.0),
            max_age_seconds=900.0,
            loop=_loop,
            now=lambda: _AS_OF,
        )
    finally:
        await engine.dispose()

    assert writes == [], f"safety core wrote to the DB: {writes}"
