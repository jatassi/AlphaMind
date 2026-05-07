"""Schema-level + round-trip tests for ``cash_ledger`` (story 04e / ALP-362).

Singleton-row table: a single row with ``id = "current"`` enforced by a
CHECK constraint. The ``CashLedger`` typed record is the source of truth
for the persisted-subset column names; computed read-time fields
(``cash_pct_of_portfolio``, ``true_deployable_capital_usd``,
``regt_excess_*``) are not stored — they are supplied at reconstitution
time by the read-path caller.
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
from alphamind.portfolio_state.records import CashLedger, UnsettledProceedsEntry


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
    # Side-effect import: registers state-persistence tables on
    # ``Base.metadata`` so ``create_all`` materializes them.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------


_LAST_UPDATED_AT = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


def _make_full_cash_ledger(
    unsettled_proceeds: tuple[UnsettledProceedsEntry, ...] = (),
) -> CashLedger:
    """Construct a typed ``CashLedger`` record for round-trip tests.

    The record carries both persisted and computed fields; the codec
    persists only the persisted subset and re-supplies the computed
    fields at reconstitution time.
    """
    return CashLedger(
        current_cash_usd=125_000.50,
        settled_cash_usd=120_000.50,
        reserved_capital_usd=15_000.00,
        available_buying_power_usd=105_000.50,
        margin_held_usd=10_000.00,
        unsettled_proceeds=unsettled_proceeds,
        cash_pct_of_portfolio=12.34,
        true_deployable_capital_usd=95_000.50,
        regt_excess_trailing_30d_usd=4_500.0,
        regt_excess_trailing_90d_usd=4_200.0,
        regt_excess_lifetime_usd=4_000.0,
    )


def _row_kwargs(
    *,
    id_: str = "current",
    unsettled_proceeds_json: str = "[]",
) -> dict[str, object]:
    return {
        "id": id_,
        "current_cash_usd": 125_000.50,
        "settled_cash_usd": 120_000.50,
        "reserved_capital_usd": 15_000.00,
        "available_buying_power_usd": 105_000.50,
        "margin_held_usd": 10_000.00,
        "unsettled_proceeds_json": unsettled_proceeds_json,
        "last_updated_at": "2026-05-07T14:30:00Z",
    }


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestCashLedgerTableShape:
    def test_table_has_eight_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("cash_ledger")}
        expected = {
            "id",
            "current_cash_usd",
            "settled_cash_usd",
            "reserved_capital_usd",
            "available_buying_power_usd",
            "margin_held_usd",
            "unsettled_proceeds_json",
            "last_updated_at",
        }
        assert cols == expected
        pk = insp.get_pk_constraint("cash_ledger")
        assert pk["constrained_columns"] == ["id"]


# ---------------------------------------------------------------------------
# Singleton CHECK constraint
# ---------------------------------------------------------------------------


class TestCashLedgerSingletonInvariant:
    def test_inserting_id_current_is_accepted(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger import (
            CashLedgerRow,
        )

        row = CashLedgerRow(**_row_kwargs())
        session.add(row)
        session.commit()
        readback = session.get(CashLedgerRow, "current")
        assert readback is not None

    def test_inserting_non_current_id_is_rejected(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger import (
            CashLedgerRow,
        )

        session.add(CashLedgerRow(**_row_kwargs(id_="other")))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec
# ---------------------------------------------------------------------------


class TestCashLedgerRoundTrip:
    def test_round_trip_with_empty_unsettled_proceeds(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger import (
            CashLedgerRow,
        )
        from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
            cash_ledger_record_from_row,
            cash_ledger_record_to_row,
        )

        record = _make_full_cash_ledger()
        row = cash_ledger_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        session.add(row)
        session.commit()

        readback = session.get(CashLedgerRow, "current")
        assert readback is not None
        rehydrated = cash_ledger_record_from_row(
            readback,
            cash_pct_of_portfolio=record.cash_pct_of_portfolio,
            true_deployable_capital_usd=record.true_deployable_capital_usd,
            regt_excess_trailing_30d_usd=record.regt_excess_trailing_30d_usd,
            regt_excess_trailing_90d_usd=record.regt_excess_trailing_90d_usd,
            regt_excess_lifetime_usd=record.regt_excess_lifetime_usd,
        )
        assert rehydrated == record
        # Empty default preserved.
        assert rehydrated.unsettled_proceeds == ()

    def test_round_trip_with_multiple_unsettled_proceeds(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger import (
            CashLedgerRow,
        )
        from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
            cash_ledger_record_from_row,
            cash_ledger_record_to_row,
        )

        proceeds = (
            UnsettledProceedsEntry(
                settlement_date=datetime(2026, 5, 9, tzinfo=UTC),
                amount_usd=2_500.0,
                source_transaction_id="fill-001",
            ),
            UnsettledProceedsEntry(
                settlement_date=datetime(2026, 5, 10, tzinfo=UTC),
                amount_usd=1_750.25,
                source_transaction_id="fill-002",
            ),
            UnsettledProceedsEntry(
                settlement_date=datetime(2026, 5, 11, tzinfo=UTC),
                amount_usd=900.10,
                source_transaction_id="fill-003",
            ),
        )
        record = _make_full_cash_ledger(unsettled_proceeds=proceeds)
        row = cash_ledger_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        session.add(row)
        session.commit()

        readback = session.get(CashLedgerRow, "current")
        assert readback is not None
        rehydrated = cash_ledger_record_from_row(
            readback,
            cash_pct_of_portfolio=record.cash_pct_of_portfolio,
            true_deployable_capital_usd=record.true_deployable_capital_usd,
            regt_excess_trailing_30d_usd=record.regt_excess_trailing_30d_usd,
            regt_excess_trailing_90d_usd=record.regt_excess_trailing_90d_usd,
            regt_excess_lifetime_usd=record.regt_excess_lifetime_usd,
        )
        assert rehydrated == record
        # Distinct settlement dates preserved.
        assert tuple(p.settlement_date for p in rehydrated.unsettled_proceeds) == tuple(
            p.settlement_date for p in proceeds
        )

    def test_record_to_row_writes_id_current(self) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
            cash_ledger_record_to_row,
        )

        record = _make_full_cash_ledger()
        row = cash_ledger_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        assert row.id == "current"

    def test_record_to_row_serializes_last_updated_at_as_utc_iso(self) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
            cash_ledger_record_to_row,
        )

        record = _make_full_cash_ledger()
        row = cash_ledger_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        assert row.last_updated_at == "2026-05-07T14:30:00Z"

    def test_record_to_row_rejects_naive_last_updated_at(self) -> None:
        from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
            cash_ledger_record_to_row,
        )

        record = _make_full_cash_ledger()
        naive = datetime(2026, 5, 7, 14, 30, 0)  # noqa: DTZ001 — exercises the codec's tz-aware guard
        with pytest.raises(ValueError, match="last_updated_at"):
            cash_ledger_record_to_row(record, last_updated_at=naive)


# Sanity-check the singleton CHECK is exercised at the SQL level.
def test_check_constraint_targets_id_current(engine: Engine) -> None:
    """Direct SQL with id != 'current' must fail at commit."""
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO cash_ledger (id, current_cash_usd, settled_cash_usd, "
                "reserved_capital_usd, available_buying_power_usd, margin_held_usd, "
                "unsettled_proceeds_json, last_updated_at) "
                "VALUES ('not_current', 0, 0, 0, 0, 0, '[]', '2026-05-07T14:30:00Z')"
            )
        )
