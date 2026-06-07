"""Window-scoped activity-event read helper (ALP-884 / story 06b).

``read_activity_events_in_window`` returns activity-log entries of the requested
event types whose ``entry_at`` falls in ``[start, end)`` — the strictly-bounded slice
the feedback-loop execution-process metrics count, distinct from the ``PM_DECISION``-only
sliding window.
"""

from __future__ import annotations

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
from alphamind.portfolio_state.events.risk_guardrail import GuardrailRejectionDetail
from alphamind.portfolio_state.events.types import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.repository.activity_log_queries import read_activity_events_in_window
from tests.state._fk_substrate import stub_invocation_row, stub_process_lifetime_row

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_IN = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)  # inside the window
_OUT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # before the window

_SEQ = [0]


def _rejection_entry(*, timestamp: datetime) -> ActivityLogEntry:
    _SEQ[0] += 1
    return ActivityLogEntry(
        entry_id=f"ent-{_SEQ[0]}",
        invocation_id="inv-1",
        timestamp=timestamp,
        event_type=EventType.GUARDRAIL_REJECTION,
        event_group=EventGroup.RISK_AND_GUARDRAIL,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=GuardrailRejectionDetail(
            command_summary="OPEN AAPL",
            blocking_rule_ids=("position_level_max_loss",),
            current_limit_values_json={},
            headroom_json={},
            suggested_modification=None,
        ),
    )


def _risk_limit_entry(*, timestamp: datetime) -> ActivityLogEntry:
    """A different event type (``RISK_LIMIT_APPROACHED``) the metric does not request."""
    from alphamind.portfolio_state.events.risk_guardrail import RiskLimitApproachedDetail

    _SEQ[0] += 1
    return ActivityLogEntry(
        entry_id=f"ent-{_SEQ[0]}",
        invocation_id="inv-1",
        timestamp=timestamp,
        event_type=EventType.RISK_LIMIT_APPROACHED,
        event_group=EventGroup.RISK_AND_GUARDRAIL,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER,
        detail=RiskLimitApproachedDetail(
            metric_id="gross_exposure",
            current_value=0.9,
            threshold_value=0.8,
            limit_value=1.0,
        ),
    )


@pytest.fixture()
async def seeded_session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    """On-disk SQLite seeded with in/out-of-window rejections + an off-type entry."""
    db_path = tmp_path / "activity_window_test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        sess.add(stub_process_lifetime_row())
        sess.flush()
        sess.add(stub_invocation_row("inv-1"))
        sess.flush()
        sess.add(activity_log_entry_to_row(_rejection_entry(timestamp=_IN)))
        sess.add(activity_log_entry_to_row(_rejection_entry(timestamp=_OUT)))
        sess.add(activity_log_entry_to_row(_risk_limit_entry(timestamp=_IN)))
        sess.commit()
    sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory: async_sessionmaker[AsyncSession] = make_async_session_factory(async_engine)
    async with factory() as sess_async:
        yield sess_async
    await async_engine.dispose()


class TestReadActivityEventsInWindow:
    async def test_in_window_requested_type_returned(self, seeded_session: AsyncSession) -> None:
        entries = await read_activity_events_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END, (EventType.GUARDRAIL_REJECTION,)
        )
        assert len(entries) == 1
        assert entries[0].event_type == EventType.GUARDRAIL_REJECTION
        assert entries[0].timestamp == _IN

    async def test_out_of_window_excluded(self, seeded_session: AsyncSession) -> None:
        entries = await read_activity_events_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END, (EventType.GUARDRAIL_REJECTION,)
        )
        assert all(e.timestamp != _OUT for e in entries)

    async def test_unrequested_event_type_excluded(self, seeded_session: AsyncSession) -> None:
        entries = await read_activity_events_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END, (EventType.GUARDRAIL_REJECTION,)
        )
        assert all(e.event_type == EventType.GUARDRAIL_REJECTION for e in entries)

    async def test_empty_event_types_returns_empty(self, seeded_session: AsyncSession) -> None:
        entries = await read_activity_events_in_window(
            seeded_session, _WINDOW_START, _WINDOW_END, ()
        )
        assert entries == ()
