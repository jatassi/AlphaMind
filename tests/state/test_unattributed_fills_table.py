"""Tests for ``UnattributedFillRow`` + Pydantic ``UnattributedFill`` (ALP-763).

The transient retry queue holds raw broker fill events whose local ``orders``
row was not yet committed when the continuous monitor received them. Covers:

* Typed ``UnattributedFill`` round-trips faithfully through the SQL row,
  including the nullable ``last_retry_at`` and the ``alerted`` bool ↔ 0/1
  storage convention.
* Column shape and the ``alpaca_order_id`` index.
* The table carries **no** ForeignKey (the order may not exist yet).
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
from alphamind.state.records import UnattributedFill
from alphamind.state.tables.unattributed_fills import UnattributedFillRow
from alphamind.state.tables.unattributed_fills_codec import record_to_row, row_to_record

FIRST_SEEN = datetime(2026, 6, 1, 14, 30, 5, tzinfo=UTC)
FILL_AT = datetime(2026, 6, 1, 14, 30, 4, tzinfo=UTC)

_EXPECTED_COLS = {
    "broker_fill_key",
    "alpaca_order_id",
    "client_order_id",
    "event_type",
    "fill_timestamp",
    "fill_price",
    "fill_quantity",
    "raw_report_json",
    "first_seen_at",
    "last_retry_at",
    "retry_count",
    "alerted",
    "escalated",
}


def _unattributed_fill(
    *,
    broker_fill_key: str = "bfk-1",
    last_retry_at: datetime | None = None,
    retry_count: int = 0,
    alerted: bool = False,
    escalated: bool = False,
) -> UnattributedFill:
    return UnattributedFill(
        broker_fill_key=broker_fill_key,
        alpaca_order_id="alpaca-ord-1",
        client_order_id="client-ord-1",
        event_type="fill",
        fill_timestamp=FILL_AT,
        fill_price=150.25,
        fill_quantity=10.0,
        raw_report_json='{"event":"fill","price":150.25}',
        first_seen_at=FIRST_SEEN,
        last_retry_at=last_retry_at,
        retry_count=retry_count,
        alerted=alerted,
        escalated=escalated,
    )


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


class TestUnattributedFillRoundTrip:
    def test_basic_fill_round_trips(self, session: Session) -> None:
        record = _unattributed_fill()
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(UnattributedFillRow, "bfk-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_alerted_and_retry_metadata_round_trip(self, session: Session) -> None:
        record = _unattributed_fill(
            last_retry_at=datetime(2026, 6, 1, 14, 35, 0, tzinfo=UTC),
            retry_count=3,
            alerted=True,
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(UnattributedFillRow, "bfk-1")
        assert readback is not None
        assert readback.alerted == 1
        assert row_to_record(readback) == record

    def test_escalated_flag_round_trips(self, session: Session) -> None:
        record = _unattributed_fill(alerted=True, escalated=True)
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(UnattributedFillRow, "bfk-1")
        assert readback is not None
        assert readback.escalated == 1
        assert row_to_record(readback).escalated is True

    def test_escalated_defaults_false(self, session: Session) -> None:
        record = _unattributed_fill()
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(UnattributedFillRow, "bfk-1")
        assert readback is not None
        assert readback.escalated == 0
        assert row_to_record(readback).escalated is False


class TestUnattributedFillsTableShape:
    def test_table_has_expected_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        assert {c["name"] for c in insp.get_columns("unattributed_fills")} == _EXPECTED_COLS

    def test_alpaca_order_id_index_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        names = {idx["name"] for idx in insp.get_indexes("unattributed_fills")}
        assert "ix_unattributed_fills_alpaca_order_id" in names

    def test_no_foreign_keys(self, engine: Engine) -> None:
        insp = inspect(engine)
        assert insp.get_foreign_keys("unattributed_fills") == []
