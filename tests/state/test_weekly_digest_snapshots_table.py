"""Tests for weekly_digest_snapshots table, codec, and repository helpers (ALP-876).

Covers:
* ``WeeklyDigestSnapshotsRow`` — table exists with correct columns; uniqueness
  constraint on ``week_start`` enforced.
* ``WeeklyDigestSnapshotRecord`` — frozen dataclass; ``digest_json`` round-trips
  as JSON-text losslessly.
* ``insert_weekly_digest_snapshot`` — encodes + inserts; readable after flush.
* ``read_weekly_digest_snapshot(week_start)`` — returns the record for a given
  week boundary; returns ``None`` when absent.
* ``read_weekly_digest_snapshots_in_range(start, end)`` — returns all snapshots
  whose ``week_start`` falls within the closed interval [start, end], ordered
  by ``week_start``.
* Duplicate ``week_start`` raises ``IntegrityError`` (uniqueness constraint).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.digest_queries import (
    insert_weekly_digest_snapshot,
    read_weekly_digest_snapshot,
    read_weekly_digest_snapshots_in_range,
)
from alphamind.state.tables.weekly_digest_snapshots import WeeklyDigestSnapshotRecord

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_WEEK_START_A = date(2026, 6, 1)  # Monday
_WEEK_START_B = date(2026, 6, 8)  # Monday
_WEEK_START_C = date(2026, 6, 15)  # Monday

_WEEK_END_A = date(2026, 6, 7)
_WEEK_END_B = date(2026, 6, 14)
_WEEK_END_C = date(2026, 6, 21)

_SNAPSHOTTED_AT_A = "2026-06-08T08:00:00+00:00"
_SNAPSHOTTED_AT_B = "2026-06-15T08:00:00+00:00"
_SNAPSHOTTED_AT_C = "2026-06-22T08:00:00+00:00"

_DIGEST_PAYLOAD_A = {"sections": ["performance"], "trade_count": 5, "pnl_usd": 123.45}
_DIGEST_PAYLOAD_B = {"sections": ["performance", "risk"], "trade_count": 3, "pnl_usd": -22.10}
_DIGEST_PAYLOAD_C: dict[str, object] = {"sections": [], "trade_count": 0, "pnl_usd": 0.0}

_SCHEMA_VERSION = 1


def _make_record(
    snapshot_id: str,
    week_start: date,
    week_end: date,
    snapshotted_at: str,
    payload: dict[str, object],
    schema_version: int = _SCHEMA_VERSION,
) -> WeeklyDigestSnapshotRecord:
    return WeeklyDigestSnapshotRecord(
        snapshot_id=snapshot_id,
        week_start=week_start,
        week_end=week_end,
        snapshotted_at=snapshotted_at,
        digest_schema_version=schema_version,
        digest_json=json.dumps(payload),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWeeklyDigestSnapshotRecord:
    def test_is_frozen(self) -> None:
        record = _make_record(
            "id-1", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        with pytest.raises((AttributeError, TypeError)):
            record.snapshot_id = "mutated"  # type: ignore[misc]

    def test_digest_json_stores_as_str(self) -> None:
        payload: dict[str, object] = {"key": "value", "number": 42}
        record = _make_record("id-2", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, payload)
        assert isinstance(record.digest_json, str)
        assert json.loads(record.digest_json) == payload


class TestInsertWeeklyDigestSnapshot:
    def test_insert_then_read_round_trips_record(self, session: Session) -> None:
        record = _make_record(
            "snap-1", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        insert_weekly_digest_snapshot(session, record)
        session.flush()

        loaded = read_weekly_digest_snapshot(session, _WEEK_START_A)
        assert loaded is not None
        assert loaded == record

    def test_digest_json_round_trips_losslessly(self, session: Session) -> None:
        """A representative nested payload survives encode -> store -> decode unchanged."""
        payload: dict[str, object] = {
            "sections": ["performance", "risk", "attribution"],
            "metrics": {"sharpe": 1.23, "max_dd_pct": -4.5},
            "trade_count": 7,
            "pnl_usd": 456.78,
            "unicode_str": "élève",
        }
        record = _make_record("snap-2", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, payload)
        insert_weekly_digest_snapshot(session, record)
        session.flush()

        loaded = read_weekly_digest_snapshot(session, _WEEK_START_A)
        assert loaded is not None
        assert json.loads(loaded.digest_json) == payload


class TestReadWeeklyDigestSnapshot:
    def test_returns_none_when_absent(self, session: Session) -> None:
        result = read_weekly_digest_snapshot(session, _WEEK_START_A)
        assert result is None

    def test_returns_correct_snapshot_by_week_start(self, session: Session) -> None:
        rec_a = _make_record(
            "snap-3a", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        rec_b = _make_record(
            "snap-3b", _WEEK_START_B, _WEEK_END_B, _SNAPSHOTTED_AT_B, _DIGEST_PAYLOAD_B
        )
        insert_weekly_digest_snapshot(session, rec_a)
        insert_weekly_digest_snapshot(session, rec_b)
        session.flush()

        loaded = read_weekly_digest_snapshot(session, _WEEK_START_B)
        assert loaded is not None
        assert loaded.snapshot_id == "snap-3b"
        assert loaded.week_start == _WEEK_START_B


class TestReadWeeklyDigestSnapshotsInRange:
    def test_returns_empty_when_no_snapshots(self, session: Session) -> None:
        result = read_weekly_digest_snapshots_in_range(session, _WEEK_START_A, _WEEK_START_C)
        assert result == ()

    def test_returns_all_snapshots_in_closed_range(self, session: Session) -> None:
        rec_a = _make_record(
            "snap-4a", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        rec_b = _make_record(
            "snap-4b", _WEEK_START_B, _WEEK_END_B, _SNAPSHOTTED_AT_B, _DIGEST_PAYLOAD_B
        )
        rec_c = _make_record(
            "snap-4c", _WEEK_START_C, _WEEK_END_C, _SNAPSHOTTED_AT_C, _DIGEST_PAYLOAD_C
        )
        insert_weekly_digest_snapshot(session, rec_a)
        insert_weekly_digest_snapshot(session, rec_b)
        insert_weekly_digest_snapshot(session, rec_c)
        session.flush()

        result = read_weekly_digest_snapshots_in_range(session, _WEEK_START_A, _WEEK_START_C)
        assert len(result) == 3
        assert result[0].week_start == _WEEK_START_A
        assert result[1].week_start == _WEEK_START_B
        assert result[2].week_start == _WEEK_START_C

    def test_range_excludes_snapshots_outside_bounds(self, session: Session) -> None:
        rec_a = _make_record(
            "snap-5a", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        rec_b = _make_record(
            "snap-5b", _WEEK_START_B, _WEEK_END_B, _SNAPSHOTTED_AT_B, _DIGEST_PAYLOAD_B
        )
        rec_c = _make_record(
            "snap-5c", _WEEK_START_C, _WEEK_END_C, _SNAPSHOTTED_AT_C, _DIGEST_PAYLOAD_C
        )
        insert_weekly_digest_snapshot(session, rec_a)
        insert_weekly_digest_snapshot(session, rec_b)
        insert_weekly_digest_snapshot(session, rec_c)
        session.flush()

        result = read_weekly_digest_snapshots_in_range(session, _WEEK_START_B, _WEEK_START_B)
        assert len(result) == 1
        assert result[0].snapshot_id == "snap-5b"

    def test_results_ordered_by_week_start_ascending(self, session: Session) -> None:
        rec_c = _make_record(
            "snap-6c", _WEEK_START_C, _WEEK_END_C, _SNAPSHOTTED_AT_C, _DIGEST_PAYLOAD_C
        )
        rec_a = _make_record(
            "snap-6a", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        rec_b = _make_record(
            "snap-6b", _WEEK_START_B, _WEEK_END_B, _SNAPSHOTTED_AT_B, _DIGEST_PAYLOAD_B
        )
        insert_weekly_digest_snapshot(session, rec_c)
        insert_weekly_digest_snapshot(session, rec_a)
        insert_weekly_digest_snapshot(session, rec_b)
        session.flush()

        result = read_weekly_digest_snapshots_in_range(session, _WEEK_START_A, _WEEK_START_C)
        assert [r.week_start for r in result] == [_WEEK_START_A, _WEEK_START_B, _WEEK_START_C]


class TestWeekStartUniquenessConstraint:
    def test_duplicate_week_start_raises_integrity_error(self, session: Session) -> None:
        """Two snapshots for the same ``week_start`` violate the uniqueness constraint."""
        rec1 = _make_record(
            "snap-7a", _WEEK_START_A, _WEEK_END_A, _SNAPSHOTTED_AT_A, _DIGEST_PAYLOAD_A
        )
        rec2 = _make_record(
            "snap-7b",
            _WEEK_START_A,  # same week_start -- should be rejected
            _WEEK_END_A,
            _SNAPSHOTTED_AT_A,
            _DIGEST_PAYLOAD_B,
        )
        insert_weekly_digest_snapshot(session, rec1)
        session.flush()
        insert_weekly_digest_snapshot(session, rec2)
        with pytest.raises(IntegrityError):
            session.flush()
