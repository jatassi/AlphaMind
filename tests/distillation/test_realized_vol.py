"""Tests for per-ticker realized-vol substrate (ALP-530).

Covers the pure compute function, the per-invocation persister, and the
read function. The wiring-site tests live alongside their respective
modules (``tests/scheduler/test_phase1_inputs.py`` and
``tests/execution/continuous_monitor/test_paper_enrichment_wiring.py``).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401  — register InvocationRow on Base
from alphamind.distillation.realized_vol import (
    compute_trailing_realized_vol,
    persist_per_ticker_realized_vol,
    read_realized_vol_map,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    TickerRealizedVolRow,
)
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _seed_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-05-18T00:00:00Z",
        )
    )


_PROCESS_LIFETIME_ID = "proc-test"


def _seed_process_lifetime(session: Session) -> None:
    session.add(
        ProcessLifetimeRow(
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            process_role="pipeline",
            process_start_at="2026-05-18T00:00:00Z",
            process_pid=1,
            hostname="test-host",
            git_sha="0" * 40,
            git_branch="main",
            git_dirty=0,
            python_version="3.13.13",
            pip_freeze_hash="0" * 64,
            pip_freeze_snapshot_path="snapshot/path",
            anthropic_sdk_version="0.0.0",
            claude_agent_sdk_version="0.0.0",
            os_release="darwin",
        )
    )


def _seed_invocation(session: Session, invocation_id: str) -> None:
    session.add(
        InvocationRow(
            invocation_id=invocation_id,
            process_lifetime_id=_PROCESS_LIFETIME_ID,
            start_at="2026-05-18T00:00:00Z",
            phase1_completed_at=None,
            phase2_completed_at=None,
            trigger_type="manual",
            trigger_source="test",
            trigger_reason="test-fixture",
            git_sha_at_invocation="0" * 40,
            active_profile="default",
            active_regime="normal",
            active_mode="normal",
            active_overlays_json="[]",
            resolved_config_hash="0" * 64,
            resolved_config_snapshot_path="snapshot/path",
            feature_flags_snapshot_json="{}",
            data_calibration_state_snapshot_path="snapshot/path",
            data_source_freshness_json="{}",
            fill_collection_summary_json=None,
            command_execution_summary_json=None,
            staleness_flag=0,
            snapshot_metadata_json=None,
        )
    )


def _trending_closes(n: int, *, start: float = 100.0, jitter: float = 0.5) -> tuple[float, ...]:
    """Synthetic close series with non-zero variance for compute tests."""
    return tuple(start + jitter * (-1.0) ** i + i * 0.1 for i in range(n))


def test_compute_returns_nonnegative_float_for_sufficient_data() -> None:
    """With six closes (five log returns) the compute returns a finite
    non-negative scalar — the documented minimum-data threshold."""
    closes = (100.0, 102.0, 101.0, 103.0, 102.5, 104.0)
    result = compute_trailing_realized_vol(closes)
    assert result is not None
    assert result >= 0.0
    assert math.isfinite(result)


def test_compute_returns_none_for_insufficient_data() -> None:
    """Below ``min_returns + 1`` closes (≤ five closes by default) the
    compute returns ``None`` rather than fabricating a vol estimate."""
    too_short = (100.0, 102.0, 101.0, 103.0, 102.5)
    assert compute_trailing_realized_vol(too_short) is None


def test_compute_is_monotone_in_input_volatility() -> None:
    """A constant-volatility series at 0.30 produces a larger realized-vol
    than the same series at 0.20 — the function must respect the volatility
    ordering of its inputs."""
    # Two synthetic geometric-Brownian price paths, identical seed pattern
    # but scaled to different volatilities. The annualized output is the
    # sample-std of log returns * sqrt(252); for a constant-vol input the
    # output reflects the daily-vol scaling.
    daily_returns_low = [0.20 / math.sqrt(252.0) * (-1.0) ** i for i in range(60)]
    daily_returns_high = [0.30 / math.sqrt(252.0) * (-1.0) ** i for i in range(60)]
    closes_low = [100.0]
    closes_high = [100.0]
    for r_low, r_high in zip(daily_returns_low, daily_returns_high, strict=True):
        closes_low.append(closes_low[-1] * math.exp(r_low))
        closes_high.append(closes_high[-1] * math.exp(r_high))
    low = compute_trailing_realized_vol(tuple(closes_low))
    high = compute_trailing_realized_vol(tuple(closes_high))
    assert low is not None
    assert high is not None
    assert high > low


# ---------------------------------------------------------------------------
# Persister tests
# ---------------------------------------------------------------------------


def test_persist_writes_one_row_per_ticker(session: Session) -> None:
    """Each ticker with sufficient closes yields one row in ``ticker_realized_vol``."""
    _seed_ticker(session, "AAPL")
    _seed_ticker(session, "MSFT")
    _seed_process_lifetime(session)
    session.flush()
    _seed_invocation(session, "inv-1")
    session.commit()

    closes_aapl = _trending_closes(40)
    closes_msft = _trending_closes(40, start=400.0)

    written = persist_per_ticker_realized_vol(
        session,
        invocation_id="inv-1",
        tickers=("AAPL", "MSFT"),
        closes_by_ticker={"AAPL": closes_aapl, "MSFT": closes_msft},
        as_of_date=date(2026, 5, 18),
        computed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    session.commit()

    rows = session.execute(select(TickerRealizedVolRow)).scalars().all()
    assert written == 2
    assert {r.ticker for r in rows} == {"AAPL", "MSFT"}
    for row in rows:
        assert row.as_of_date == "2026-05-18"
        assert row.invocation_id == "inv-1"
        assert row.trailing_30d_realized_vol > 0.0
        assert row.computed_at == "2026-05-18T12:00:00Z"


def test_persist_skips_tickers_with_insufficient_closes(session: Session) -> None:
    """Tickers whose close history is too short are silently skipped — no row,
    no exception."""
    _seed_ticker(session, "AAPL")
    _seed_ticker(session, "TOOSHORT")
    _seed_process_lifetime(session)
    session.flush()
    _seed_invocation(session, "inv-1")
    session.commit()

    written = persist_per_ticker_realized_vol(
        session,
        invocation_id="inv-1",
        tickers=("AAPL", "TOOSHORT"),
        closes_by_ticker={
            "AAPL": _trending_closes(40),
            "TOOSHORT": (100.0, 101.0),
        },
        as_of_date=date(2026, 5, 18),
        computed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    session.commit()

    rows = session.execute(select(TickerRealizedVolRow)).scalars().all()
    assert written == 1
    assert {r.ticker for r in rows} == {"AAPL"}


def test_persist_upserts_same_day_reinvocation(session: Session) -> None:
    """A second invocation on the same day overwrites the prior row — same PK,
    new vol / invocation_id / computed_at."""
    _seed_ticker(session, "AAPL")
    _seed_process_lifetime(session)
    session.flush()
    _seed_invocation(session, "inv-1")
    _seed_invocation(session, "inv-2")
    session.commit()

    closes_a = _trending_closes(40)
    closes_b = _trending_closes(40, jitter=2.0)  # higher variance

    persist_per_ticker_realized_vol(
        session,
        invocation_id="inv-1",
        tickers=("AAPL",),
        closes_by_ticker={"AAPL": closes_a},
        as_of_date=date(2026, 5, 18),
        computed_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )
    session.commit()
    first = session.execute(select(TickerRealizedVolRow)).scalar_one()
    first_vol = first.trailing_30d_realized_vol

    persist_per_ticker_realized_vol(
        session,
        invocation_id="inv-2",
        tickers=("AAPL",),
        closes_by_ticker={"AAPL": closes_b},
        as_of_date=date(2026, 5, 18),
        computed_at=datetime(2026, 5, 18, 15, 0, tzinfo=UTC),
    )
    session.commit()

    rows = session.execute(select(TickerRealizedVolRow)).scalars().all()
    assert len(rows) == 1
    upserted = rows[0]
    assert upserted.invocation_id == "inv-2"
    assert upserted.computed_at == "2026-05-18T15:00:00Z"
    assert upserted.trailing_30d_realized_vol != first_vol


# ---------------------------------------------------------------------------
# Read function tests (async — the consumer-side path uses AsyncSession)
# ---------------------------------------------------------------------------


def _seed_realized_vol_row(
    session: Session,
    *,
    ticker: str,
    as_of_date: str,
    vol: float,
    invocation_id: str,
) -> None:
    session.add(
        TickerRealizedVolRow(
            ticker=ticker,
            as_of_date=as_of_date,
            trailing_30d_realized_vol=vol,
            invocation_id=invocation_id,
            computed_at="2026-05-18T12:00:00Z",
        )
    )


async def test_read_returns_latest_row_per_ticker(tmp_path: Any) -> None:
    """Default behavior (no ``as_of_date`` filter) returns each ticker's
    most recent row keyed by the underlying ticker."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
    )

    db_path = tmp_path / "realized_vol.db"
    sync_eng = make_engine(str(db_path))
    Base.metadata.create_all(sync_eng)
    sync_eng.dispose()

    # Seed via a sync session for setup convenience.
    sync_factory = make_session_factory(make_engine(str(db_path)))
    with sync_factory() as sync_sess:
        _seed_ticker(sync_sess, "AAPL")
        _seed_ticker(sync_sess, "MSFT")
        _seed_process_lifetime(sync_sess)
        sync_sess.flush()
        _seed_invocation(sync_sess, "inv-1")
        sync_sess.commit()
        _seed_realized_vol_row(
            sync_sess, ticker="AAPL", as_of_date="2026-05-17", vol=0.18, invocation_id="inv-1"
        )
        _seed_realized_vol_row(
            sync_sess, ticker="AAPL", as_of_date="2026-05-18", vol=0.22, invocation_id="inv-1"
        )
        _seed_realized_vol_row(
            sync_sess, ticker="MSFT", as_of_date="2026-05-18", vol=0.31, invocation_id="inv-1"
        )
        sync_sess.commit()

    async_eng = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_eng)
    try:
        async with factory() as async_sess:
            result = await read_realized_vol_map(async_sess)
    finally:
        await async_eng.dispose()

    assert result == {"AAPL": pytest.approx(0.22), "MSFT": pytest.approx(0.31)}


async def test_read_filters_by_ticker(tmp_path: Any) -> None:
    """When ``tickers=...`` is provided, only those tickers' rows appear."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
    )

    db_path = tmp_path / "realized_vol.db"
    sync_eng = make_engine(str(db_path))
    Base.metadata.create_all(sync_eng)
    sync_eng.dispose()

    sync_factory = make_session_factory(make_engine(str(db_path)))
    with sync_factory() as sync_sess:
        _seed_ticker(sync_sess, "AAPL")
        _seed_ticker(sync_sess, "MSFT")
        _seed_ticker(sync_sess, "GOOG")
        _seed_process_lifetime(sync_sess)
        sync_sess.flush()
        _seed_invocation(sync_sess, "inv-1")
        sync_sess.commit()
        for tkr, v in (("AAPL", 0.20), ("MSFT", 0.30), ("GOOG", 0.40)):
            _seed_realized_vol_row(
                sync_sess, ticker=tkr, as_of_date="2026-05-18", vol=v, invocation_id="inv-1"
            )
        sync_sess.commit()

    async_eng = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_eng)
    try:
        async with factory() as async_sess:
            result = await read_realized_vol_map(async_sess, tickers=("AAPL",))
    finally:
        await async_eng.dispose()

    assert result == {"AAPL": pytest.approx(0.20)}


async def test_read_filters_by_as_of_date(tmp_path: Any) -> None:
    """When ``as_of_date=...`` is provided, only rows pinned to that date are
    returned (and the result is empty when no rows match)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from alphamind.persistence.session import (
        make_async_engine,
        make_async_session_factory,
    )

    db_path = tmp_path / "realized_vol.db"
    sync_eng = make_engine(str(db_path))
    Base.metadata.create_all(sync_eng)
    sync_eng.dispose()

    sync_factory = make_session_factory(make_engine(str(db_path)))
    with sync_factory() as sync_sess:
        _seed_ticker(sync_sess, "AAPL")
        _seed_process_lifetime(sync_sess)
        sync_sess.flush()
        _seed_invocation(sync_sess, "inv-1")
        sync_sess.commit()
        _seed_realized_vol_row(
            sync_sess, ticker="AAPL", as_of_date="2026-05-17", vol=0.18, invocation_id="inv-1"
        )
        _seed_realized_vol_row(
            sync_sess, ticker="AAPL", as_of_date="2026-05-18", vol=0.22, invocation_id="inv-1"
        )
        sync_sess.commit()

    async_eng = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_eng)
    try:
        async with factory() as async_sess:
            hit = await read_realized_vol_map(async_sess, as_of_date=date(2026, 5, 17))
            miss = await read_realized_vol_map(async_sess, as_of_date=date(2020, 1, 1))
    finally:
        await async_eng.dispose()

    assert hit == {"AAPL": pytest.approx(0.18)}
    assert miss == {}
