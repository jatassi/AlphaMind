"""Tests for the PM-decision replay queue iterator (ALP-564, story 08 §2).

``iter_pending_replay_proposals`` yields ``(entry, detail, replay_kind)`` for
PM_DECISION activity-log rows whose verdict is replayable, in ascending
``entry_at`` order, without hydrating the proposal body (the not-yet-due horizon
gate lives in the driver, which hydrates inside its protected body — see the
queue module docstring). The behaviors under test:

* verdict filter — APPROVE / OVERRIDE entries are skipped;
* since filter — an entry before ``since`` is skipped, including the boundary
  rendered in the activity-log's stored ``Z``-suffixed format;
* replay-kind mapping — REJECT → REJECTION, APPROVE_WITH_MODIFICATION →
  MODIFICATION_ORIGINAL_FORM.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.execution.counterfactual_replay_engine.enums import ReplayKind
from alphamind.execution.counterfactual_replay_engine.queue import (
    iter_pending_replay_proposals,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import PMVerdict
from alphamind.state.tables.activity_log import ActivityLogRow
from tests.execution.counterfactual_replay_engine._fixtures import (
    add_pm_decision_row,
    analyst_equity_recommendation_json,
    pm_decision_entry,
    seed_invocation,
)

_INVOCATION = "inv-2026-06-01T14:00:00Z-aaaa"
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)


@pytest.fixture()
def session() -> Iterator[Session]:
    import alphamind.state.tables  # noqa: F401 — register all tables

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    factory = make_session_factory(eng)
    with factory() as sess:
        seed_invocation(sess, _INVOCATION)
        yield sess
    eng.dispose()


def _seed_analyst(
    session: Session,
    *,
    entry_id: str,
    envelope_id: str,
    verdict: PMVerdict,
    ts: datetime,
) -> None:
    entry = pm_decision_entry(
        entry_id=entry_id,
        invocation_id=_INVOCATION,
        timestamp=ts,
        envelope_id=envelope_id,
        verdict=verdict,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=analyst_equity_recommendation_json(),
    )
    add_pm_decision_row(session, entry)


def _collect(
    session: Session, *, since: datetime | None = None
) -> list[tuple[ActivityLogEntry, PMDecisionDetail, ReplayKind]]:
    return list(iter_pending_replay_proposals(session, since=since))


class TestQueueVerdictFilter:
    def test_reject_entry_yielded_as_rejection(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        results = _collect(session)
        assert len(results) == 1
        _entry, detail, kind = results[0]
        assert detail.envelope_id == "env-1"
        assert kind is ReplayKind.REJECTION

    def test_approve_with_modification_yielded_as_modification(self, session: Session) -> None:
        _seed_analyst(
            session,
            entry_id="e1",
            envelope_id="env-1",
            verdict=PMVerdict.APPROVE_WITH_MODIFICATION,
            ts=_PROPOSAL_TS,
        )
        session.flush()
        results = _collect(session)
        assert len(results) == 1
        assert results[0][2] is ReplayKind.MODIFICATION_ORIGINAL_FORM

    def test_approve_and_override_are_skipped(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.APPROVE, ts=_PROPOSAL_TS
        )
        _seed_analyst(
            session,
            entry_id="e2",
            envelope_id="env-2",
            verdict=PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION,
            ts=_PROPOSAL_TS,
        )
        session.flush()
        assert _collect(session) == []


class TestQueueDoesNotHydrate:
    def test_malformed_proposal_body_does_not_raise_in_queue(self, session: Session) -> None:
        # A REJECT entry whose originating-proposal body is un-hydratable. The
        # queue must NOT hydrate (it would raise ProposalHydrationError and escape
        # the driver's per-proposal try/except); it yields the entry verbatim for
        # the driver to hydrate inside its protected body.
        entry = pm_decision_entry(
            entry_id="e1",
            invocation_id=_INVOCATION,
            timestamp=_PROPOSAL_TS,
            envelope_id="env-bad",
            verdict=PMVerdict.REJECT,
            source_provenance_json={"source_provenance": "pm_analyst"},
            originating_proposal_json={"not": "a valid recommendation"},
        )
        add_pm_decision_row(session, entry)
        session.flush()

        results = _collect(session)

        assert len(results) == 1
        assert results[0][0].detail.envelope_id == "env-bad"


class TestQueueSinceFilter:
    def test_entry_before_since_is_skipped(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        since = _PROPOSAL_TS + timedelta(hours=1)
        assert _collect(session, since=since) == []

    def test_entry_at_or_after_since_is_yielded(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        since = _PROPOSAL_TS - timedelta(hours=1)
        results = _collect(session, since=since)
        assert len(results) == 1

    def test_since_boundary_matches_stored_z_suffixed_format(self, session: Session) -> None:
        # The activity-log writer stores entry_at as isoformat() with +00:00
        # rewritten to "Z". The since bound is rendered the same way, so a since
        # set EXACTLY to the stored timestamp is inclusive (entry_at >= since).
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        # Confirm the stored format is the Z-suffixed one this boundary must match.
        stored_entry_at = session.execute(
            select(ActivityLogRow.entry_at).where(ActivityLogRow.entry_id == "e1")
        ).scalar_one()
        assert stored_entry_at == "2026-06-01T14:00:00Z"
        assert len(_collect(session, since=_PROPOSAL_TS)) == 1

    def test_naive_since_is_coerced_to_utc(self, session: Session) -> None:
        # A naive since (no tzinfo) is assumed UTC; without coercion its
        # isoformat() lacks the +00:00 offset and would not be rewritten to "Z",
        # mis-ordering the lexical comparison against the stored "...Z" text.
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        naive_before = datetime(2026, 6, 1, 13, 0, 0)  # noqa: DTZ001 — testing naive coercion
        assert len(_collect(session, since=naive_before)) == 1
        naive_after = datetime(2026, 6, 1, 15, 0, 0)  # noqa: DTZ001
        assert _collect(session, since=naive_after) == []
