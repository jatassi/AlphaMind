"""Tests for ``SqlOptionPriceProvider`` (ALP-516).

Each test seeds ``options_contracts`` + ``options_contract_snapshots`` rows
through a sync engine, then exercises :class:`SqlOptionPriceProvider`
against the async session factory's underlying engine URL (the provider
strips ``+aiosqlite`` internally to obtain a sync ``Session``). On-disk
SQLite is required because the sync sessionmaker the provider builds is
backed by a distinct ``Engine`` instance — a shared in-memory DB would
not be visible across engines.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Force a clean load order: ``state.invocation_context`` must be fully
# imported before ``state.repository.__init__`` runs, otherwise the
# repository package triggers a partial-import chain via
# ``activity_log_queries → state.invocation_context.config_change →
# activity_log_queries``. Other ``tests/state/*`` modules accidentally
# avoid this because they already import an ``invocation_context`` symbol
# (e.g. ``InvocationRecord``); we don't, so the priming is explicit.
import alphamind.state.invocation_context  # noqa: F401
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OptionsContracts,
    OptionsContractSnapshots,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.pricing import (
    PriceSource,
    UnknownOptionContractError,
)
from alphamind.state.repository.sql_option_price_provider import (
    SqlOptionPriceProvider,
)

_OCC_AAPL = "O:AAPL260116C00200000"
_OCC_TSLA = "O:TSLA260116P00500000"
_OCC_NVDA = "O:NVDA260116C00500000"

_NOW = datetime(2026, 5, 17, 14, 30, 0, tzinfo=UTC)


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) backed by a fresh on-disk DB."""
    db_path = tmp_path / "alphamind.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def _ensure_underlying(session: object, underlying: str) -> None:
    """Insert the ``asset_universe`` parent row required by the contract FK."""
    session.add(  # type: ignore[attr-defined]
        AssetUniverse(
            asset_id=f"asset-{underlying.lower()}",
            ticker=underlying,
            full_name=f"{underlying} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-01T00:00:00Z",
        )
    )
    session.flush()  # type: ignore[attr-defined]


def _seed_contract_and_snapshot(
    db_path: Path,
    *,
    occ_symbol: str,
    snapshot_ts: datetime,
    last_price: float | None,
    bid: float | None = None,
    ask: float | None = None,
    underlying: str = "AAPL",
) -> None:
    """Insert an ``asset_universe`` + ``options_contracts`` + snapshot triple."""
    sync_engine = make_engine(str(db_path))
    factory = make_session_factory(sync_engine)
    try:
        with factory() as session, session.begin():
            _ensure_underlying(session, underlying)
            session.add(
                OptionsContracts(
                    contract_ticker=occ_symbol,
                    underlying_ticker=underlying,
                    expiration_date="2026-01-16",
                    strike_price=200.0,
                    contract_type="call",
                    first_seen_at="2026-04-01T00:00:00Z",
                    last_seen_at=snapshot_ts.isoformat().replace("+00:00", "Z"),
                    source="test",
                )
            )
            session.flush()
            session.add(
                OptionsContractSnapshots(
                    snapshot_ts=snapshot_ts.isoformat().replace("+00:00", "Z"),
                    contract_ticker=occ_symbol,
                    underlying_ticker=underlying,
                    open_interest=100,
                    volume_today=50,
                    last_price=last_price,
                    bid=bid,
                    ask=ask,
                    implied_volatility=0.30,
                    delta=0.5,
                    gamma=0.05,
                    theta=-0.03,
                    vega=0.10,
                    rho=0.02,
                    underlying_price=200.0,
                    source="test",
                    ingested_at=snapshot_ts.isoformat().replace("+00:00", "Z"),
                )
            )
    finally:
        sync_engine.dispose()


# ---------------------------------------------------------------------------
# Live snapshot — fresh quote returned with bid/ask midpoint
# ---------------------------------------------------------------------------


async def test_live_snapshot_prefers_midpoint_over_last_price(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    """Bid/ask midpoint takes precedence over ``last_price``; the two differ here."""
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=30),
        last_price=4.50,
        bid=5.10,
        ask=5.30,
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert _OCC_AAPL in result
    quote = result[_OCC_AAPL]
    # Midpoint = (5.10 + 5.30) / 2 = 5.20 — distinct from last_price 4.50
    assert quote.price_usd == pytest.approx(5.20)
    assert quote.is_stale is False
    assert quote.source is PriceSource.INTRADAY_QUOTE
    assert quote.ticker == _OCC_AAPL


async def test_live_snapshot_falls_back_to_last_price_when_quote_missing(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=30),
        last_price=7.50,
        bid=None,
        ask=None,
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert result[_OCC_AAPL].price_usd == pytest.approx(7.50)


async def test_crossed_quote_falls_back_to_last_price(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    """A crossed book (bid > ask) is treated as no-quote; last_price drives the price."""
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=30),
        last_price=6.00,
        bid=5.30,
        ask=5.10,
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert result[_OCC_AAPL].price_usd == pytest.approx(6.00)


# ---------------------------------------------------------------------------
# Stale snapshot — quote returned with is_stale=True
# ---------------------------------------------------------------------------


async def test_stale_snapshot_returns_is_stale_true(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=1800),
        last_price=5.20,
        bid=5.15,
        ask=5.25,
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert _OCC_AAPL in result
    assert result[_OCC_AAPL].is_stale is True


# ---------------------------------------------------------------------------
# No snapshot — symbol omitted from result
# ---------------------------------------------------------------------------


async def test_no_snapshot_omits_symbol(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _async_engine, factory = db
    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert result == {}


async def test_no_snapshot_get_quote_raises_unknown_option_contract_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _async_engine, factory = db
    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)

    with pytest.raises(UnknownOptionContractError):
        provider.get_quote(_OCC_AAPL, freshness_threshold_seconds=900.0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_empty_query_returns_empty_dict(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _async_engine, factory = db
    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    assert provider.get_quotes((), freshness_threshold_seconds=900.0) == {}


async def test_unusable_snapshot_row_omitted(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=30),
        last_price=None,
        bid=None,
        ask=None,
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert result == {}


async def test_returns_latest_snapshot_when_multiple_present(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _async_engine, factory = db
    db_path = tmp_path / "alphamind.db"
    earlier_ts = _NOW - timedelta(seconds=300)
    later_ts = _NOW - timedelta(seconds=30)

    sync_engine = make_engine(str(db_path))
    sync_factory = make_session_factory(sync_engine)
    try:
        with sync_factory() as session, session.begin():
            _ensure_underlying(session, "AAPL")
            session.add(
                OptionsContracts(
                    contract_ticker=_OCC_AAPL,
                    underlying_ticker="AAPL",
                    expiration_date="2026-01-16",
                    strike_price=200.0,
                    contract_type="call",
                    first_seen_at="2026-04-01T00:00:00Z",
                    last_seen_at=later_ts.isoformat().replace("+00:00", "Z"),
                    source="test",
                )
            )
            session.flush()
            for ts, last_price in ((earlier_ts, 4.00), (later_ts, 5.50)):
                session.add(
                    OptionsContractSnapshots(
                        snapshot_ts=ts.isoformat().replace("+00:00", "Z"),
                        contract_ticker=_OCC_AAPL,
                        underlying_ticker="AAPL",
                        open_interest=100,
                        volume_today=50,
                        last_price=last_price,
                        bid=last_price - 0.05,
                        ask=last_price + 0.05,
                        implied_volatility=0.30,
                        delta=0.5,
                        gamma=0.05,
                        theta=-0.03,
                        vega=0.10,
                        rho=0.02,
                        underlying_price=200.0,
                        source="test",
                        ingested_at=ts.isoformat().replace("+00:00", "Z"),
                    )
                )
    finally:
        sync_engine.dispose()

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes((_OCC_AAPL,), freshness_threshold_seconds=900.0)

    assert result[_OCC_AAPL].price_usd == pytest.approx(5.50)


async def test_batch_returns_mix_of_present_and_missing(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    tmp_path: Path,
) -> None:
    _async_engine, factory = db
    _seed_contract_and_snapshot(
        tmp_path / "alphamind.db",
        occ_symbol=_OCC_AAPL,
        snapshot_ts=_NOW - timedelta(seconds=30),
        last_price=5.20,
        bid=5.15,
        ask=5.25,
        underlying="AAPL",
    )

    provider = SqlOptionPriceProvider(session_factory=factory, now=lambda: _NOW)
    result = provider.get_quotes(
        (_OCC_AAPL, _OCC_TSLA, _OCC_NVDA),
        freshness_threshold_seconds=900.0,
    )

    assert _OCC_AAPL in result
    assert _OCC_TSLA not in result
    assert _OCC_NVDA not in result
