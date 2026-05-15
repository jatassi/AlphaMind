"""Tests for ``fetch_iv_from_options_chains`` + batched variant (story 03a)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.execution.continuous_monitor.greeks_refresh import (
    IVQuote,
    fetch_iv_from_options_chains,
    fetch_iv_quotes_batch,
)
from alphamind.persistence.models import Base, OptionsContracts, OptionsContractSnapshots
from alphamind.persistence.session import make_async_engine, make_async_session_factory

# ---------------------------------------------------------------------------
# Fixtures — on-disk SQLite with the canonical schema and contract / snapshot rows
# ---------------------------------------------------------------------------


@pytest.fixture()
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite database seeded with one underlying contract."""
    db_path = tmp_path / "alphamind.db"
    engine = make_async_engine(str(db_path))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    async with factory() as sess:
        # asset_universe FK is satisfied by the ondelete='RESTRICT' check —
        # contracts table reads from it via FK. We seed an asset row first.
        from alphamind.persistence.models import AssetUniverse

        sess.add(
            AssetUniverse(
                asset_id="asset-AAPL",
                ticker=Symbol("AAPL"),
                full_name="Apple Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2020-01-01T00:00:00Z",
            )
        )
        sess.add(
            OptionsContracts(
                contract_ticker="O:AAPL260619C00200000",
                underlying_ticker=Symbol("AAPL"),
                expiration_date="2026-06-19",
                strike_price=200.0,
                contract_type="call",
                first_seen_at="2026-05-01T00:00:00Z",
                last_seen_at="2026-05-11T14:00:00Z",
                source="polygon",
            )
        )
        await sess.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


def _insert_snapshot(
    *,
    contract_ticker: str,
    snapshot_ts: datetime,
    implied_volatility: float | None,
    underlying_ticker: str = "AAPL",
) -> OptionsContractSnapshots:
    return OptionsContractSnapshots(
        snapshot_ts=snapshot_ts.isoformat().replace("+00:00", "Z"),
        contract_ticker=contract_ticker,
        underlying_ticker=underlying_ticker,
        open_interest=None,
        volume_today=None,
        last_price=None,
        bid=None,
        ask=None,
        implied_volatility=implied_volatility,
        delta=None,
        gamma=None,
        theta=None,
        vega=None,
        rho=None,
        underlying_price=None,
        source="polygon",
        ingested_at=snapshot_ts.isoformat().replace("+00:00", "Z"),
    )


# ---------------------------------------------------------------------------
# fetch_iv_from_options_chains
# ---------------------------------------------------------------------------


class TestFetchIvFromOptionsChains:
    async def test_returns_latest_iv_when_row_exists(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        symbol = "O:AAPL260619C00200000"
        earlier = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
        later = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        async with async_factory() as sess:
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol, snapshot_ts=earlier, implied_volatility=0.25
                )
            )
            sess.add(
                _insert_snapshot(contract_ticker=symbol, snapshot_ts=later, implied_volatility=0.30)
            )
            await sess.commit()

        async with async_factory() as sess:
            quote = await fetch_iv_from_options_chains(sess, occ_symbol=symbol)

        assert quote is not None
        assert quote.occ_symbol == symbol
        assert quote.iv == 0.30
        # tz-aware UTC per the contract; round-trip preserves the timestamp.
        assert quote.as_of.tzinfo is not None
        assert quote.as_of == later

    async def test_returns_none_when_no_row_exists(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as sess:
            quote = await fetch_iv_from_options_chains(sess, occ_symbol="O:NOSUCH000000C00000000")
        assert quote is None

    async def test_skips_rows_with_null_iv(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A newer row with ``implied_volatility=NULL`` does NOT shadow an older
        row that has a valid value — the reader filters NULL out at SQL time."""
        symbol = "O:AAPL260619C00200000"
        earlier = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)
        later = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        async with async_factory() as sess:
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol, snapshot_ts=earlier, implied_volatility=0.25
                )
            )
            sess.add(
                _insert_snapshot(contract_ticker=symbol, snapshot_ts=later, implied_volatility=None)
            )
            await sess.commit()
        async with async_factory() as sess:
            quote = await fetch_iv_from_options_chains(sess, occ_symbol=symbol)
        assert quote is not None
        assert quote.iv == 0.25
        assert quote.as_of == earlier


class TestFetchIvQuotesBatch:
    async def test_single_query_returns_all_symbols(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        symbol_a = "O:AAPL260619C00200000"
        async with async_factory() as sess:
            # Seed a second contract — the FK requires an OptionsContracts row.
            sess.add(
                OptionsContracts(
                    contract_ticker="O:AAPL260619P00200000",
                    underlying_ticker=Symbol("AAPL"),
                    expiration_date="2026-06-19",
                    strike_price=200.0,
                    contract_type="put",
                    first_seen_at="2026-05-01T00:00:00Z",
                    last_seen_at="2026-05-11T14:00:00Z",
                    source="polygon",
                )
            )
            await sess.commit()
        symbol_b = "O:AAPL260619P00200000"
        base_ts = datetime(2026, 5, 11, 13, 0, tzinfo=UTC)
        async with async_factory() as sess:
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol_a, snapshot_ts=base_ts, implied_volatility=0.25
                )
            )
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol_a,
                    snapshot_ts=base_ts + timedelta(minutes=30),
                    implied_volatility=0.27,
                )
            )
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol_b,
                    snapshot_ts=base_ts + timedelta(minutes=30),
                    implied_volatility=0.40,
                )
            )
            await sess.commit()

        async with async_factory() as sess:
            result = await fetch_iv_quotes_batch(sess, occ_symbols=(symbol_a, symbol_b))

        assert set(result.keys()) == {symbol_a, symbol_b}
        assert result[symbol_a] == IVQuote(
            occ_symbol=symbol_a,
            iv=0.27,
            as_of=base_ts + timedelta(minutes=30),
        )
        assert result[symbol_b] == IVQuote(
            occ_symbol=symbol_b,
            iv=0.40,
            as_of=base_ts + timedelta(minutes=30),
        )

    async def test_missing_symbol_absent_from_result(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        symbol_present = "O:AAPL260619C00200000"
        symbol_missing = "O:UNKWN240101C00100000"
        async with async_factory() as sess:
            sess.add(
                _insert_snapshot(
                    contract_ticker=symbol_present,
                    snapshot_ts=datetime(2026, 5, 11, 13, 0, tzinfo=UTC),
                    implied_volatility=0.25,
                )
            )
            await sess.commit()
        async with async_factory() as sess:
            result = await fetch_iv_quotes_batch(sess, occ_symbols=(symbol_present, symbol_missing))
        assert symbol_present in result
        assert symbol_missing not in result

    async def test_empty_input_returns_empty(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as sess:
            result = await fetch_iv_quotes_batch(sess, occ_symbols=())
        assert result == {}
