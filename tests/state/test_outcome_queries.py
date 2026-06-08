"""Window-scoped resolved-thesis read helper (ALP-885 / story 06c).

``read_resolved_theses_in_window`` returns only RESOLVED theses whose
``resolution_timestamp`` falls in ``[start, end)``, decoded with their component
rows, and excludes ACTIVE theses and theses resolved outside the window.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.repository.outcome_queries import (
    read_invocation_conditioning,
    read_resolved_theses_in_window,
    read_resolved_thesis_pnl_by_position,
)
from alphamind.state.tables.theses_codec import record_to_rows
from tests.feedback_loop.metrics._outcome_fixtures import make_resolved_thesis_record
from tests.state._fk_substrate import (
    stub_invocation_row,
    stub_position_row,
    stub_process_lifetime_row,
    stub_thesis_row,
)

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_RES_IN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # inside the window
_RES_OUT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # before the window


def _seed_resolved(sess: object, thesis_id: str, position_id: str, resolved_at: datetime) -> None:
    sess.add(stub_position_row(position_id))  # type: ignore[attr-defined]
    sess.flush()  # type: ignore[attr-defined]
    record = make_resolved_thesis_record(thesis_id, position_id, resolution_timestamp=resolved_at)
    thesis_row, comp_rows = record_to_rows(record)
    sess.add(thesis_row)  # type: ignore[attr-defined]
    for crow in comp_rows:
        sess.add(crow)  # type: ignore[attr-defined]


@pytest.fixture()
async def seeded_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with in/out-of-window resolved theses + one ACTIVE."""
    db_path = tmp_path / "outcome_queries_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        _seed_resolved(sess, "thes-in", "pos-in", _RES_IN)
        _seed_resolved(sess, "thes-out", "pos-out", _RES_OUT)
        # An ACTIVE thesis must never be returned, even resolved-in-window time.
        sess.add(stub_position_row("pos-active"))
        sess.flush()
        sess.add(
            stub_thesis_row("thes-active", "pos-active", status=ThesisRecordStatus.ACTIVE.value)
        )
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadResolvedThesesInWindow:
    async def test_in_window_resolved_thesis_returned(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert {t.thesis_id for t in theses} == {"thes-in"}

    async def test_components_decoded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        (thesis,) = theses
        assert len(thesis.components) == 3
        assert thesis.resolution_pnl_usd == 100.0

    async def test_out_of_window_excluded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert all(t.thesis_id != "thes-out" for t in theses)

    async def test_active_thesis_excluded(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(seeded_session, _WINDOW_START, _WINDOW_END)
        assert all(t.thesis_id != "thes-active" for t in theses)

    async def test_empty_window_returns_empty_tuple(self, seeded_session: AsyncSession) -> None:
        theses = await read_resolved_theses_in_window(
            seeded_session,
            datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2030, 2, 1, tzinfo=UTC),
        )
        assert theses == ()


# ---------------------------------------------------------------------------
# ALP-928 — read_resolved_thesis_pnl_by_position helper
# ---------------------------------------------------------------------------


def _seed_resolved_pnl(
    sess: object,
    thesis_id: str,
    position_id: str,
    resolved_at: datetime,
    resolution_pnl_usd: float,
) -> None:
    sess.add(stub_position_row(position_id))  # type: ignore[attr-defined]
    sess.flush()  # type: ignore[attr-defined]
    record = make_resolved_thesis_record(
        thesis_id,
        position_id,
        resolution_timestamp=resolved_at,
        resolution_pnl_usd=resolution_pnl_usd,
    )
    thesis_row, comp_rows = record_to_rows(record)
    sess.add(thesis_row)  # type: ignore[attr-defined]
    for crow in comp_rows:
        sess.add(crow)  # type: ignore[attr-defined]


@pytest.fixture()
async def pnl_by_position_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """SQLite seeded with resolved theses on distinct positions (one out-of-window)
    plus an ACTIVE thesis whose position must never resolve a P/L."""
    db_path = tmp_path / "pnl_by_position_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        _seed_resolved_pnl(sess, "thes-a", "pos-a", _RES_IN, 42.0)
        # Resolved well outside the analytics window — still keyed by position.
        _seed_resolved_pnl(sess, "thes-b", "pos-b", _RES_OUT, -17.0)
        sess.add(stub_position_row("pos-active"))
        sess.flush()
        sess.add(
            stub_thesis_row("thes-active", "pos-active", status=ThesisRecordStatus.ACTIVE.value)
        )
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadResolvedThesisPnlByPosition:
    async def test_maps_position_to_resolution_pnl_regardless_of_window(
        self, pnl_by_position_session: AsyncSession
    ) -> None:
        # Both positions resolve a P/L even though pos-b resolved outside any
        # analytics window — the modified-form thesis's own resolution is keyed
        # by position, not clipped to a window.
        result = await read_resolved_thesis_pnl_by_position(
            pnl_by_position_session, ["pos-a", "pos-b"]
        )
        assert result == {"pos-a": 42.0, "pos-b": -17.0}

    async def test_active_thesis_position_absent(
        self, pnl_by_position_session: AsyncSession
    ) -> None:
        result = await read_resolved_thesis_pnl_by_position(
            pnl_by_position_session, ["pos-a", "pos-active"]
        )
        assert "pos-active" not in result
        assert result == {"pos-a": 42.0}

    async def test_unknown_position_absent(self, pnl_by_position_session: AsyncSession) -> None:
        result = await read_resolved_thesis_pnl_by_position(
            pnl_by_position_session, ["pos-a", "pos-missing"]
        )
        assert "pos-missing" not in result
        assert result == {"pos-a": 42.0}

    async def test_empty_positions_returns_empty_dict(
        self, pnl_by_position_session: AsyncSession
    ) -> None:
        result = await read_resolved_thesis_pnl_by_position(pnl_by_position_session, [])
        assert result == {}


# ---------------------------------------------------------------------------
# ALP-930 (A)/(B) — loud-on-null guard + deterministic latest-resolution winner
# ---------------------------------------------------------------------------


def _seed_resolved_pnl_at(
    sess: object,
    thesis_id: str,
    position_id: str,
    resolved_at: datetime,
    resolution_pnl_usd: float,
    *,
    seed_position: bool = True,
) -> None:
    """Seed one RESOLVED thesis on *position_id* with a controlled P/L + timestamp.

    Unlike ``_seed_resolved_pnl`` it can attach a second resolved thesis to an
    already-seeded position (``seed_position=False``) — the multi-RESOLVED-per-position
    case (B) guards against.
    """
    if seed_position:
        sess.add(stub_position_row(position_id))  # type: ignore[attr-defined]
        sess.flush()  # type: ignore[attr-defined]
    record = make_resolved_thesis_record(
        thesis_id,
        position_id,
        resolution_timestamp=resolved_at,
        resolution_pnl_usd=resolution_pnl_usd,
    )
    thesis_row, comp_rows = record_to_rows(record)
    sess.add(thesis_row)  # type: ignore[attr-defined]
    for crow in comp_rows:
        sess.add(crow)  # type: ignore[attr-defined]


class TestReadResolvedThesisPnlByPositionGuards:
    async def test_resolved_thesis_with_null_pnl_raises(self, tmp_path: Path) -> None:
        """A RESOLVED thesis whose ``narrative_json`` carries ``resolution_pnl_usd:
        null`` is inconsistent persisted state and raises loudly (not a silent skip)."""
        db_path = tmp_path / "null_pnl.db"
        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(stub_position_row("pos-null"))
            sess.flush()
            # A valid resolved record's rows, with the parent P/L blanked to null —
            # the only way to persist the inconsistent state the typed record forbids.
            record = make_resolved_thesis_record(
                "thes-null", "pos-null", resolution_timestamp=_RES_IN
            )
            thesis_row, comp_rows = record_to_rows(record)
            payload = json.loads(thesis_row.narrative_json)
            payload["resolution_pnl_usd"] = None
            thesis_row.narrative_json = json.dumps(payload)
            sess.add(thesis_row)
            for crow in comp_rows:
                sess.add(crow)
            sess.commit()
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            async with factory() as sess_async:
                with pytest.raises(ValueError, match="null resolution_pnl_usd"):
                    await read_resolved_thesis_pnl_by_position(sess_async, ["pos-null"])
        finally:
            await async_engine.dispose()

    async def test_latest_resolution_wins_per_position(self, tmp_path: Path) -> None:
        """Two RESOLVED theses on one position → the later-resolved thesis's P/L wins.

        Schema permits two RESOLVED theses per position (no UNIQUE on position_id;
        cancel → reopen → re-resolve), so the read orders by resolution_timestamp and
        the latest resolution is the position's live realized outcome.
        """
        db_path = tmp_path / "multi_resolved.db"
        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        earlier = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
        later = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
        with make_session_factory(sync_engine)() as sess:
            # Seed the later-resolved thesis first so scan order ≠ resolution order;
            # only an explicit ORDER BY makes the latest win deterministically.
            _seed_resolved_pnl_at(sess, "thes-late", "pos-multi", later, 99.0)
            _seed_resolved_pnl_at(
                sess, "thes-early", "pos-multi", earlier, 11.0, seed_position=False
            )
            sess.commit()
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        try:
            async with factory() as sess_async:
                result = await read_resolved_thesis_pnl_by_position(sess_async, ["pos-multi"])
            assert result == {"pos-multi": 99.0}
        finally:
            await async_engine.dispose()


# ---------------------------------------------------------------------------
# ALP-919 — read_invocation_conditioning helper
# ---------------------------------------------------------------------------

_INV_NORMAL = "inv-norm-1"
_INV_ELEVATED = "inv-elev-1"


@pytest.fixture()
async def conditioning_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with two invocation rows (different regime/trigger_source)."""
    from alphamind.persistence.session import make_session_factory

    db_path = tmp_path / "inv_cond_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    plt = stub_process_lifetime_row()
    with make_session_factory(sync_engine)() as sess:
        sess.add(plt)
        sess.flush()
        inv_normal = stub_invocation_row(_INV_NORMAL)
        inv_normal.active_regime = "normal"
        inv_normal.trigger_source = "market_open"
        sess.add(inv_normal)
        inv_elevated = stub_invocation_row(_INV_ELEVATED)
        inv_elevated.active_regime = "elevated"
        inv_elevated.trigger_source = "continuous_monitor"
        sess.add(inv_elevated)
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadInvocationConditioning:
    async def test_returns_regime_and_trigger_source(
        self, conditioning_session: AsyncSession
    ) -> None:
        result = await read_invocation_conditioning(
            conditioning_session, [_INV_NORMAL, _INV_ELEVATED]
        )
        assert result[_INV_NORMAL].regime == "normal"
        assert result[_INV_NORMAL].time_of_day == "market_open"
        assert result[_INV_ELEVATED].regime == "elevated"
        assert result[_INV_ELEVATED].time_of_day == "continuous_monitor"

    async def test_unknown_invocation_id_absent_from_result(
        self, conditioning_session: AsyncSession
    ) -> None:
        result = await read_invocation_conditioning(
            conditioning_session, [_INV_NORMAL, "inv-does-not-exist"]
        )
        assert "inv-does-not-exist" not in result
        assert _INV_NORMAL in result

    async def test_empty_ids_returns_empty_dict(self, conditioning_session: AsyncSession) -> None:
        result = await read_invocation_conditioning(conditioning_session, [])
        assert result == {}
