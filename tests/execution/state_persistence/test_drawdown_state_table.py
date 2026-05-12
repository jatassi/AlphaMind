"""Schema-level + round-trip tests for ``drawdown_state`` (story 04e / ALP-362).

Singleton-row table: a single row with ``id = "current"`` enforced by a
CHECK constraint. The persisted column set mirrors the running-state
subset of :class:`alphamind.portfolio_state.aggregates.DrawdownState`.
Read-time computed fields (zones, tier, intraday drawdown) are not
stored — they are derived from the running fields plus current snapshot
context at delivery time.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.regime import RiskZone
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.aggregates import DrawdownState


@pytest.fixture()
def engine() -> Iterator[Engine]:
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


def _make_drawdown_state(
    drawdown_by_source_pct: dict[str, float] | None = None,
) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=4.25,
        equity_high_water_mark_usd=1_050_000.0,
        drawdown_duration_hours=18.5,
        lifetime_max_drawdown_pct=8.10,
        intraday_drawdown_pct=1.20,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.WARNING,
        cumulative_tier=None,
        drawdown_by_source_pct=drawdown_by_source_pct if drawdown_by_source_pct is not None else {},
    )


def _row_kwargs(
    *,
    id_: str = "current",
    drawdown_by_source_json: str = "{}",
) -> dict[str, object]:
    return {
        "id": id_,
        "equity_high_water_mark_usd": 1_050_000.0,
        "current_drawdown_pct": 4.25,
        "drawdown_duration_hours": 18.5,
        "lifetime_max_drawdown_pct": 8.10,
        "drawdown_by_source_json": drawdown_by_source_json,
        "last_updated_at": "2026-05-07T14:30:00Z",
    }


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestDrawdownStateTableShape:
    def test_table_has_seven_columns(self, engine: Engine) -> None:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("drawdown_state")}
        expected = {
            "id",
            "equity_high_water_mark_usd",
            "current_drawdown_pct",
            "drawdown_duration_hours",
            "lifetime_max_drawdown_pct",
            "drawdown_by_source_json",
            "last_updated_at",
        }
        assert cols == expected
        pk = insp.get_pk_constraint("drawdown_state")
        assert pk["constrained_columns"] == ["id"]


# ---------------------------------------------------------------------------
# Singleton CHECK constraint
# ---------------------------------------------------------------------------


class TestDrawdownStateSingletonInvariant:
    def test_inserting_id_current_is_accepted(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state import (
            DrawdownStateRow,
        )

        session.add(DrawdownStateRow(**_row_kwargs()))
        session.commit()
        readback = session.get(DrawdownStateRow, "current")
        assert readback is not None

    def test_inserting_non_current_id_is_rejected(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state import (
            DrawdownStateRow,
        )

        session.add(DrawdownStateRow(**_row_kwargs(id_="other")))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec
# ---------------------------------------------------------------------------


class TestDrawdownStateRoundTrip:
    def test_round_trip_with_empty_drawdown_by_source(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state import (
            DrawdownStateRow,
        )
        from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
            drawdown_state_record_from_row,
            drawdown_state_record_to_row,
        )

        record = _make_drawdown_state()
        row = drawdown_state_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        session.add(row)
        session.commit()

        readback = session.get(DrawdownStateRow, "current")
        assert readback is not None
        rehydrated = drawdown_state_record_from_row(
            readback,
            intraday_drawdown_pct=record.intraday_drawdown_pct,
            daily_zone=record.daily_zone,
            cumulative_zone=record.cumulative_zone,
            cumulative_tier=record.cumulative_tier,
        )
        assert rehydrated == record
        # Empty default preserved.
        assert rehydrated.drawdown_by_source_pct == {}

    def test_round_trip_with_populated_drawdown_by_source(self, session: Session) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state import (
            DrawdownStateRow,
        )
        from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
            drawdown_state_record_from_row,
            drawdown_state_record_to_row,
        )

        contributions = {
            "pos-001": 1.25,
            "pos-002": 0.75,
            "pos-003": 2.25,
        }
        record = _make_drawdown_state(drawdown_by_source_pct=contributions)
        row = drawdown_state_record_to_row(record, last_updated_at=_LAST_UPDATED_AT)
        session.add(row)
        session.commit()

        readback = session.get(DrawdownStateRow, "current")
        assert readback is not None
        rehydrated = drawdown_state_record_from_row(
            readback,
            intraday_drawdown_pct=record.intraday_drawdown_pct,
            daily_zone=record.daily_zone,
            cumulative_zone=record.cumulative_zone,
            cumulative_tier=record.cumulative_tier,
        )
        assert rehydrated == record
        assert rehydrated.drawdown_by_source_pct == contributions

    def test_record_to_row_writes_id_current(self) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
            drawdown_state_record_to_row,
        )

        row = drawdown_state_record_to_row(
            _make_drawdown_state(),
            last_updated_at=_LAST_UPDATED_AT,
        )
        assert row.id == "current"

    def test_record_to_row_serializes_last_updated_at_as_utc_iso(self) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
            drawdown_state_record_to_row,
        )

        row = drawdown_state_record_to_row(
            _make_drawdown_state(),
            last_updated_at=_LAST_UPDATED_AT,
        )
        assert row.last_updated_at == "2026-05-07T14:30:00Z"

    def test_record_to_row_rejects_naive_last_updated_at(self) -> None:
        from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
            drawdown_state_record_to_row,
        )

        naive = datetime(2026, 5, 7, 14, 30, 0)  # noqa: DTZ001 — exercises the codec's tz-aware guard
        with pytest.raises(ValueError, match="last_updated_at"):
            drawdown_state_record_to_row(_make_drawdown_state(), last_updated_at=naive)


# Sanity-check the singleton CHECK is exercised at the SQL level.
def test_check_constraint_targets_id_current(engine: Engine) -> None:
    """Direct SQL with id != 'current' must fail at commit."""
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO drawdown_state (id, equity_high_water_mark_usd, "
                "current_drawdown_pct, drawdown_duration_hours, "
                "lifetime_max_drawdown_pct, drawdown_by_source_json, "
                "last_updated_at) "
                "VALUES ('not_current', 0, 0, 0, 0, '{}', '2026-05-07T14:30:00Z')"
            )
        )
