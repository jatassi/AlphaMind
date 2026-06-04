"""Tests for ``PositionGreeksRow`` + ``PositionGreeksRecord`` (ALP-843 / W0a).

The single-writer (monitor-owned) greeks side table, keyed by ``position_id``
(ADR-0005). Covers that the typed record round-trips, that the table is keyed
to ``positions`` by FK, and — the load-bearing invariant — that **no** greeks
columns were added to the ``positions`` row.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.records_position_greeks import PositionGreeksRecord
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.position_greeks_codec import record_to_row, row_to_record

from ._fk_substrate import seed_position_cluster

UPDATED = datetime(2026, 6, 4, 14, 30, 5, tzinfo=UTC)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with make_session_factory(engine)() as sess:
        yield sess


class TestPositionGreeksRoundTrip:
    def test_greeks_round_trip(self, session: Session) -> None:
        seed_position_cluster(session)
        record = PositionGreeksRecord(
            position_id="pos-1",
            delta=0.55,
            gamma=0.012,
            theta=-0.08,
            vega=0.21,
            iv=0.34,
            updated_at=UPDATED,
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(PositionGreeksRow, "pos-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_null_iv_round_trips(self, session: Session) -> None:
        seed_position_cluster(session)
        record = PositionGreeksRecord(
            position_id="pos-1",
            delta=-0.40,
            gamma=0.01,
            theta=-0.05,
            vega=0.18,
            iv=None,
            updated_at=UPDATED,
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(PositionGreeksRow, "pos-1")
        assert readback is not None
        assert row_to_record(readback).iv is None


class TestPositionGreeksTableShape:
    def test_keyed_by_position_id_fk(self, engine: Engine) -> None:
        insp = inspect(engine)
        pk = insp.get_pk_constraint("position_greeks")
        assert pk["constrained_columns"] == ["position_id"]
        fks = insp.get_foreign_keys("position_greeks")
        assert any(
            fk["referred_table"] == "positions"
            and fk["constrained_columns"] == ["position_id"]
            for fk in fks
        )

    def test_no_greeks_columns_on_positions_row(self, engine: Engine) -> None:
        """ADR-0005: greeks live in the side table, never RMW'd onto positions."""
        insp = inspect(engine)
        position_cols = {c["name"] for c in insp.get_columns("positions")}
        assert position_cols.isdisjoint({"delta", "gamma", "theta", "vega", "iv"})
