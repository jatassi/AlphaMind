"""Tests for ``BrokerEventLogRow`` + ``BrokerEventRecord`` (ALP-843 / W0a).

The append-only broker-event log (ADR-0002/0005). Covers:

* Typed ``BrokerEventRecord`` round-trips faithfully through the SQL row,
  including the nullable broker-carried link fields and ``broker_timestamp``.
* ``event_key`` is UNIQUE — a websocket delivery and a recovery replay of the
  same event collapse to one row (idempotency).
* The ``event_type`` CHECK constraint rejects an out-of-vocabulary value and
  admits a value from each of the four event families.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.broker_event_log_codec import record_to_row, row_to_record

from ._fk_substrate import seed_position_cluster

CAPTURED = datetime(2026, 6, 4, 14, 30, 5, tzinfo=UTC)
BROKER_TS = datetime(2026, 6, 4, 14, 30, 4, tzinfo=UTC)


def _event(
    *,
    event_key: str = "evt-1",
    event_type: BrokerEventType = BrokerEventType.FILL,
    thesis_id: str | None = None,
    invocation_id: str | None = None,
    position_id: str | None = None,
    broker_timestamp: datetime | None = BROKER_TS,
) -> BrokerEventRecord:
    return BrokerEventRecord(
        event_key=event_key,
        event_type=event_type,
        thesis_id=thesis_id,
        invocation_id=invocation_id,
        position_id=position_id,
        raw_payload_json='{"event":"fill","price":"150.25"}',
        broker_timestamp=broker_timestamp,
        captured_at=CAPTURED,
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


class TestBrokerEventLogRoundTrip:
    def test_unlinked_event_round_trips(self, session: Session) -> None:
        record = _event()
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(BrokerEventLogRow, "evt-1")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_linked_event_round_trips(self, session: Session) -> None:
        seed_position_cluster(session)
        record = _event(
            event_key="evt-linked",
            thesis_id="thesis-1",
            position_id="pos-1",
        )
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(BrokerEventLogRow, "evt-linked")
        assert readback is not None
        assert row_to_record(readback) == record

    def test_null_broker_timestamp_round_trips(self, session: Session) -> None:
        record = _event(broker_timestamp=None)
        session.add(record_to_row(record))
        session.commit()

        readback = session.get(BrokerEventLogRow, "evt-1")
        assert readback is not None
        assert row_to_record(readback).broker_timestamp is None


class TestBrokerEventLogConstraints:
    def test_event_key_is_unique(self, session: Session) -> None:
        session.add(record_to_row(_event(event_key="dup")))
        session.commit()
        session.add(record_to_row(_event(event_key="dup")))
        with pytest.raises(IntegrityError):
            session.commit()

    def test_event_type_check_rejects_unknown(self, engine: Engine) -> None:
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO broker_event_log "
                    "(event_key, event_type, raw_payload_json, captured_at) "
                    "VALUES ('x', 'NOT_A_TYPE', '{}', :ts)"
                ),
                {"ts": CAPTURED.isoformat()},
            )

    @pytest.mark.parametrize(
        "event_type",
        [
            BrokerEventType.FILL,
            BrokerEventType.OPEXP,
            BrokerEventType.OPASN,
            BrokerEventType.CA_SPLIT,
            BrokerEventType.TERMINAL_ORDER_STATUS,
        ],
    )
    def test_event_type_check_admits_each_family(
        self, session: Session, event_type: BrokerEventType
    ) -> None:
        record = _event(event_key=f"evt-{event_type.value}", event_type=event_type)
        session.add(record_to_row(record))
        session.commit()
        readback = session.get(BrokerEventLogRow, f"evt-{event_type.value}")
        assert readback is not None
        assert row_to_record(readback).event_type is event_type


class TestBrokerEventLogTableShape:
    def test_event_type_index_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        names = {idx["name"] for idx in insp.get_indexes("broker_event_log")}
        assert "ix_broker_event_log_event_type" in names

    def test_link_foreign_keys_present(self, engine: Engine) -> None:
        insp = inspect(engine)
        referred = {fk["referred_table"] for fk in insp.get_foreign_keys("broker_event_log")}
        assert {"theses", "invocations", "positions"} <= referred
