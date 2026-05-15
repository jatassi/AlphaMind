"""Tracer-bullet: verify short_selling table models create correctly in-memory."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    ShortInterestSnapshot,
    ShortVolumeDaily,
)
from alphamind.persistence.session import make_engine, make_session_factory

_UNIVERSE_ROW = {
    "asset_id": "u1",
    "ticker": "AAPL",
    "full_name": "Apple Inc.",
    "asset_class": "equity",
    "asset_role": "universe",
    "exchange": "NASDAQ",
    "is_active": 1,
    "added_date": "2020-01-01",
    "last_updated": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = make_session_factory(engine)
    with sf() as sess:
        sess.add(AssetUniverse(**_UNIVERSE_ROW))
        sess.commit()
    return sf


def test_short_interest_snapshot_table_exists() -> None:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    assert "short_interest_snapshots" in Base.metadata.tables


def test_short_volume_daily_table_exists() -> None:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    assert "short_volume_daily" in Base.metadata.tables


def test_short_interest_snapshot_insert_and_query(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as sess:
        sess.add(
            ShortInterestSnapshot(
                settlement_date="2026-01-15",
                ticker=Symbol("AAPL"),
                current_short_shares=100000,
                previous_short_shares=90000,
                avg_daily_volume_shares=50000000,
                days_to_cover=2.0,
                change_pct=11.1,
                source="finra",
                ingested_at="2026-01-25T10:00:00+00:00",
            )
        )
        sess.commit()

    with session_factory() as sess:
        row = sess.get(ShortInterestSnapshot, ("2026-01-15", "AAPL"))
        assert row is not None
        assert row.current_short_shares == 100000
        assert row.source == "finra"


def test_short_volume_daily_insert_and_query(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as sess:
        sess.add(
            ShortVolumeDaily(
                trade_date="2026-01-24",
                ticker=Symbol("AAPL"),
                market="cnms",
                short_volume=2000000,
                short_exempt_volume=10000,
                total_volume=5000000,
                source="finra",
                ingested_at="2026-01-24T19:00:00+00:00",
            )
        )
        sess.commit()

    with session_factory() as sess:
        row = sess.get(ShortVolumeDaily, ("2026-01-24", "AAPL", "cnms"))
        assert row is not None
        assert row.short_volume == 2000000
        assert row.market == "cnms"
        assert row.source == "finra"
