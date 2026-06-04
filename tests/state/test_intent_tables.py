"""Tests for the Intent tables: ``thesis_pnl_ledger`` + ``capital_reservations``.

Intent (CONTEXT.md, ADR-0005) — authored by the pipeline, never overwritten by
a broker snapshot. Covers that each typed record round-trips faithfully through
its SQL row, including the signed realized-PnL (a loss) and the optional
``released_at`` / invocation provenance fields.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.money import money, signed_money
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.records_intent import (
    CapitalReservationRecord,
    ThesisPnlLedgerRecord,
)
from alphamind.state.tables.capital_reservations import CapitalReservationRow
from alphamind.state.tables.capital_reservations_codec import (
    record_to_row as reservation_to_row,
)
from alphamind.state.tables.capital_reservations_codec import (
    row_to_record as reservation_to_record,
)
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from alphamind.state.tables.thesis_pnl_ledger_codec import (
    record_to_row as ledger_to_row,
)
from alphamind.state.tables.thesis_pnl_ledger_codec import (
    row_to_record as ledger_to_record,
)

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


class TestThesisPnlLedgerRoundTrip:
    def test_profit_entry_round_trips(self, session: Session) -> None:
        seed_position_cluster(session)
        record = ThesisPnlLedgerRecord(
            thesis_id="thesis-1",
            realized_pnl_usd=signed_money("1234.56"),
            cost_basis_usd=money("10000.00"),
            provenance_json='{"event_keys":["evt-1","evt-2"]}',
            derived_from_invocation_id=None,
            updated_at=UPDATED,
        )
        session.add(ledger_to_row(record))
        session.commit()

        readback = session.get(ThesisPnlLedgerRow, "thesis-1")
        assert readback is not None
        assert ledger_to_record(readback) == record

    def test_realized_loss_round_trips_signed(self, session: Session) -> None:
        seed_position_cluster(session)
        record = ThesisPnlLedgerRecord(
            thesis_id="thesis-1",
            realized_pnl_usd=signed_money("-987.65"),
            cost_basis_usd=money("5000.00"),
            provenance_json="{}",
            derived_from_invocation_id=None,
            updated_at=UPDATED,
        )
        session.add(ledger_to_row(record))
        session.commit()

        readback = session.get(ThesisPnlLedgerRow, "thesis-1")
        assert readback is not None
        assert ledger_to_record(readback).realized_pnl_usd == signed_money("-987.65")


class TestCapitalReservationRoundTrip:
    def test_live_reservation_round_trips(self, session: Session) -> None:
        seed_position_cluster(session)
        record = CapitalReservationRecord(
            reservation_id="res-1",
            thesis_id="thesis-1",
            reserved_capital_usd=money("2500.00"),
            reserved_by_invocation_id=None,
            reserved_at=UPDATED,
            released_at=None,
        )
        session.add(reservation_to_row(record))
        session.commit()

        readback = session.get(CapitalReservationRow, "res-1")
        assert readback is not None
        assert reservation_to_record(readback) == record

    def test_released_reservation_round_trips(self, session: Session) -> None:
        seed_position_cluster(session)
        released = datetime(2026, 6, 4, 18, 0, 0, tzinfo=UTC)
        record = CapitalReservationRecord(
            reservation_id="res-2",
            thesis_id="thesis-1",
            reserved_capital_usd=money("2500.00"),
            reserved_by_invocation_id=None,
            reserved_at=UPDATED,
            released_at=released,
        )
        session.add(reservation_to_row(record))
        session.commit()

        readback = session.get(CapitalReservationRow, "res-2")
        assert readback is not None
        assert reservation_to_record(readback).released_at == released
