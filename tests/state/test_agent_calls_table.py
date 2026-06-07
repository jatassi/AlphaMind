"""Tests for the agent_calls table + codec + repository helpers (ALP-873).

Covers:
* ``AgentCallRecord`` + ``AgentCallsRow`` — column shape, CHECK constraints,
  FK to ``invocations``.
* Codec round-trip: ``encode_agent_call`` → ``decode_agent_call`` equals original
  for all-populated and minimally-populated records.
* ``insert_agent_call`` + ``read_agent_calls_for_invocation`` — insert then read
  back from the same invocation; different invocation not returned.
* ``read_agent_calls_in_window`` — returns only calls whose owning invocation's
  ``start_at`` falls in the window (AC: window filter).
* ``read_agent_calls_for_agent`` — filters by agent_name within the window.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.repository.agent_calls_queries import (
    insert_agent_call,
    read_agent_calls_for_agent,
    read_agent_calls_for_invocation,
    read_agent_calls_in_window,
)
from alphamind.state.tables.agent_calls import (
    AgentCallErrorClass,
    AgentCallRecord,
    AgentCallsRow,
)
from alphamind.state.tables.agent_calls_codec import decode_agent_call, encode_agent_call
from tests.state._fk_substrate import (
    stub_invocation_row,
    stub_process_lifetime_row,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TS_EARLY = "2026-01-01T09:00:00+00:00"
_TS_MID = "2026-06-01T09:00:00+00:00"
_TS_LATE = "2026-12-01T09:00:00+00:00"

_DT_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_DT_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)  # _TS_MID is inside; _TS_EARLY is outside

_INV_EARLY = "inv-early"
_INV_MID = "inv-mid"
_PLT = "plt-agent-tests"


# ---------------------------------------------------------------------------
# Fixtures — sync (codec + shape tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Fixtures — async (repository helper tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def async_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """On-disk SQLite with both invocations seeded as FK targets."""
    db_path = tmp_path / "agent_calls_test.db"

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)

    with make_session_factory(sync_engine)() as sess:
        # Insert process_lifetime first (FK dependency) and flush before invocations.
        sess.add(stub_process_lifetime_row(_PLT))
        sess.flush()
        # Two invocations at different timestamps for window-filter tests.
        early_row = stub_invocation_row(_INV_EARLY, process_lifetime_id=_PLT)
        early_row.start_at = _TS_EARLY
        mid_row = stub_invocation_row(_INV_MID, process_lifetime_id=_PLT)
        mid_row.start_at = _TS_MID
        sess.add(early_row)
        sess.add(mid_row)
        sess.commit()

    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


@pytest.fixture()
async def async_session(
    async_db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[AsyncSession]:
    _, factory = async_db
    async with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_record(
    agent_call_id: str = "call-1",
    invocation_id: str = _INV_MID,
    agent_name: str = "analyst",
    attempt_number: int = 1,
    *,
    error_class: AgentCallErrorClass | None = None,
    error_message: str | None = None,
    output_schema_ref: str | None = "schema/analyst.json",
    tools_definition_ref: str | None = None,
    output_artifact_ref: str | None = "provenance/inv-mid/agent_calls/call-1/output.json",
    success: bool = True,
) -> AgentCallRecord:
    return AgentCallRecord(
        agent_call_id=agent_call_id,
        invocation_id=invocation_id,
        agent_name=agent_name,
        attempt_number=attempt_number,
        model_id="claude-opus-4-8",
        prompt_path="prompts/analysis/analyst.md",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json='{"temperature": 1.0, "max_tokens": 4096}',
        output_schema_ref=output_schema_ref,
        tools_definition_ref=tools_definition_ref,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=50,
        wall_clock_ms=3200,
        stop_reason="end_turn",
        success=success,
        error_class=error_class,
        error_message=error_message,
        output_artifact_ref=output_artifact_ref,
    )


def _make_failed_record(
    agent_call_id: str = "call-fail-1",
    invocation_id: str = _INV_MID,
    agent_name: str = "analyst",
) -> AgentCallRecord:
    return _make_record(
        agent_call_id=agent_call_id,
        invocation_id=invocation_id,
        agent_name=agent_name,
        success=False,
        error_class=AgentCallErrorClass.malformed_output,
        error_message="Response did not match the JSON schema.",
        output_artifact_ref=None,
    )


# ---------------------------------------------------------------------------
# Cycle 1 — Codec round-trip (no DB required)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_full_record_round_trips(self) -> None:
        record = _make_record()
        assert decode_agent_call(encode_agent_call(record)) == record

    def test_minimal_record_round_trips(self) -> None:
        """All nullable optionals set to None."""
        record = _make_record(
            output_schema_ref=None,
            tools_definition_ref=None,
            output_artifact_ref=None,
            success=True,
            error_class=None,
            error_message=None,
        )
        assert decode_agent_call(encode_agent_call(record)) == record

    def test_failed_record_with_error_class_round_trips(self) -> None:
        record = _make_failed_record()
        assert decode_agent_call(encode_agent_call(record)) == record

    def test_all_error_class_members_round_trip(self) -> None:
        for member in AgentCallErrorClass:
            record = _make_failed_record(
                agent_call_id=f"call-{member.value}",
                invocation_id=_INV_MID,
                agent_name="analyst",
            )
            record = AgentCallRecord(**{**encode_agent_call(record), "error_class": member})
            assert decode_agent_call(encode_agent_call(record)) == record

    def test_decode_accepts_orm_row(self, session: Session) -> None:
        """``decode_agent_call`` works with an ORM row (attribute access), not just dicts."""
        # Seed the FK targets in dependency order (PLT → invocation).
        session.add(stub_process_lifetime_row())
        session.flush()
        session.add(stub_invocation_row(_INV_MID))
        session.flush()

        record = _make_record()
        row = AgentCallsRow(**encode_agent_call(record))
        session.add(row)
        session.flush()
        session.expire(row)

        from sqlalchemy import select

        fetched = session.execute(
            select(AgentCallsRow).where(AgentCallsRow.agent_call_id == record.agent_call_id)
        ).scalar_one()
        assert decode_agent_call(fetched) == record


# ---------------------------------------------------------------------------
# Cycle 2 — insert_agent_call + read_agent_calls_for_invocation
# ---------------------------------------------------------------------------


class TestInsertAndRead:
    async def test_insert_then_read_for_invocation(self, async_session: AsyncSession) -> None:
        record = _make_record()
        await insert_agent_call(async_session, record)
        await async_session.flush()

        loaded = await read_agent_calls_for_invocation(async_session, _INV_MID)
        assert len(loaded) == 1
        assert loaded[0] == record

    async def test_two_calls_same_invocation_both_returned(
        self, async_session: AsyncSession
    ) -> None:
        r1 = _make_record(agent_call_id="call-a", attempt_number=1)
        r2 = _make_record(agent_call_id="call-b", attempt_number=2)
        await insert_agent_call(async_session, r1)
        await insert_agent_call(async_session, r2)
        await async_session.flush()

        loaded = await read_agent_calls_for_invocation(async_session, _INV_MID)
        assert len(loaded) == 2
        assert {r.agent_call_id for r in loaded} == {"call-a", "call-b"}

    async def test_different_invocation_not_returned(self, async_session: AsyncSession) -> None:
        r_mid = _make_record(agent_call_id="call-mid", invocation_id=_INV_MID)
        r_early = _make_record(agent_call_id="call-early", invocation_id=_INV_EARLY)
        await insert_agent_call(async_session, r_mid)
        await insert_agent_call(async_session, r_early)
        await async_session.flush()

        loaded = await read_agent_calls_for_invocation(async_session, _INV_MID)
        assert len(loaded) == 1
        assert loaded[0].agent_call_id == "call-mid"

    async def test_empty_invocation_returns_empty_tuple(self, async_session: AsyncSession) -> None:
        loaded = await read_agent_calls_for_invocation(async_session, "inv-nonexistent")
        assert loaded == ()


# ---------------------------------------------------------------------------
# Cycle 3 — read_agent_calls_in_window (AC: window filter)
# ---------------------------------------------------------------------------


class TestWindowFilter:
    async def test_call_inside_window_returned(self, async_session: AsyncSession) -> None:
        # _INV_MID has start_at inside _DT_WINDOW_START / _DT_WINDOW_END
        record = _make_record(agent_call_id="call-in", invocation_id=_INV_MID)
        await insert_agent_call(async_session, record)
        await async_session.flush()

        loaded = await read_agent_calls_in_window(async_session, _DT_WINDOW_START, _DT_WINDOW_END)
        assert len(loaded) == 1
        assert loaded[0].agent_call_id == "call-in"

    async def test_call_outside_window_not_returned(self, async_session: AsyncSession) -> None:
        # _INV_EARLY has start_at well before the window.
        record = _make_record(agent_call_id="call-out", invocation_id=_INV_EARLY)
        await insert_agent_call(async_session, record)
        await async_session.flush()

        loaded = await read_agent_calls_in_window(async_session, _DT_WINDOW_START, _DT_WINDOW_END)
        assert loaded == ()

    async def test_window_filter_excludes_outside_keeps_inside(
        self, async_session: AsyncSession
    ) -> None:
        """Both invocations present; only the one inside the window is returned."""
        r_in = _make_record(agent_call_id="call-in2", invocation_id=_INV_MID)
        r_out = _make_record(agent_call_id="call-out2", invocation_id=_INV_EARLY)
        await insert_agent_call(async_session, r_in)
        await insert_agent_call(async_session, r_out)
        await async_session.flush()

        loaded = await read_agent_calls_in_window(async_session, _DT_WINDOW_START, _DT_WINDOW_END)
        assert len(loaded) == 1
        assert loaded[0].agent_call_id == "call-in2"

    async def test_empty_window_returns_empty_tuple(self, async_session: AsyncSession) -> None:
        loaded = await read_agent_calls_in_window(
            async_session,
            datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2031, 1, 1, tzinfo=UTC),
        )
        assert loaded == ()


# ---------------------------------------------------------------------------
# Cycle 4 — read_agent_calls_for_agent
# ---------------------------------------------------------------------------


class TestAgentNameFilter:
    async def test_filter_by_agent_name_in_window(self, async_session: AsyncSession) -> None:
        analyst = _make_record(
            agent_call_id="call-analyst", invocation_id=_INV_MID, agent_name="analyst"
        )
        strategist = _make_record(
            agent_call_id="call-strategist",
            invocation_id=_INV_MID,
            agent_name="strategist",
        )
        await insert_agent_call(async_session, analyst)
        await insert_agent_call(async_session, strategist)
        await async_session.flush()

        loaded = await read_agent_calls_for_agent(
            async_session, "analyst", _DT_WINDOW_START, _DT_WINDOW_END
        )
        assert len(loaded) == 1
        assert loaded[0].agent_name == "analyst"
        assert loaded[0].agent_call_id == "call-analyst"

    async def test_agent_outside_window_not_returned(self, async_session: AsyncSession) -> None:
        record = _make_record(
            agent_call_id="call-early-analyst",
            invocation_id=_INV_EARLY,
            agent_name="analyst",
        )
        await insert_agent_call(async_session, record)
        await async_session.flush()

        loaded = await read_agent_calls_for_agent(
            async_session, "analyst", _DT_WINDOW_START, _DT_WINDOW_END
        )
        assert loaded == ()

    async def test_unknown_agent_name_returns_empty(self, async_session: AsyncSession) -> None:
        record = _make_record(agent_call_id="call-pm", agent_name="portfolio_manager")
        await insert_agent_call(async_session, record)
        await async_session.flush()

        loaded = await read_agent_calls_for_agent(
            async_session, "no_such_agent", _DT_WINDOW_START, _DT_WINDOW_END
        )
        assert loaded == ()
