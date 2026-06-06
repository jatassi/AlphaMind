"""Tests for the PM-decision replay queue iterator (ALP-564, story 08 §2).

``iter_pending_replay_proposals`` yields ``(entry, detail, replay_kind)`` for
PM_DECISION activity-log rows that are *due*: verdict in
{REJECT, APPROVE_WITH_MODIFICATION}, evaluation horizon elapsed by ``as_of``,
and (when given) ``entry.timestamp >= since``. The behaviors under test:

* verdict filter — APPROVE / OVERRIDE entries are skipped;
* horizon filter — an entry whose replay window ends after ``as_of`` is skipped;
* since filter — an entry before ``since`` is skipped;
* replay-kind mapping — REJECT → REJECTION, APPROVE_WITH_MODIFICATION →
  MODIFICATION_ORIGINAL_FORM.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import alphamind.state.invocation_context  # noqa: F401 — break circular import seam
from alphamind.execution.counterfactual_replay_engine.enums import ReplayKind
from alphamind.execution.counterfactual_replay_engine.queue import (
    iter_pending_replay_proposals,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.types import PMVerdict
from tests.execution.counterfactual_replay_engine._fixtures import (
    REPLAY_CONFIG,
    add_pm_decision_row,
    analyst_equity_recommendation_json,
    pm_decision_entry,
    seed_invocation,
)

_INVOCATION = "inv-2026-06-01T14:00:00Z-aaaa"
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
# Analyst market proposal window = proposal_ts + 24h (time_expectation_hours).
_HORIZON_ELAPSED_AS_OF = _PROPOSAL_TS + timedelta(hours=25)
_HORIZON_PENDING_AS_OF = _PROPOSAL_TS + timedelta(hours=10)


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


def _collect(session: Session, *, as_of: datetime, since: datetime | None = None) -> list:
    return list(
        iter_pending_replay_proposals(session, as_of=as_of, since=since, config=REPLAY_CONFIG)
    )


class TestQueueVerdictFilter:
    def test_reject_entry_yielded_as_rejection(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        results = _collect(session, as_of=_HORIZON_ELAPSED_AS_OF)
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
        results = _collect(session, as_of=_HORIZON_ELAPSED_AS_OF)
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
        assert _collect(session, as_of=_HORIZON_ELAPSED_AS_OF) == []


class TestQueueHorizonFilter:
    def test_entry_whose_horizon_not_elapsed_is_skipped(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        # as_of is only 10h after the proposal; the 24h window has not elapsed.
        assert _collect(session, as_of=_HORIZON_PENDING_AS_OF) == []


class TestQueueSinceFilter:
    def test_entry_before_since_is_skipped(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        since = _PROPOSAL_TS + timedelta(hours=1)
        assert _collect(session, as_of=_HORIZON_ELAPSED_AS_OF, since=since) == []

    def test_entry_at_or_after_since_is_yielded(self, session: Session) -> None:
        _seed_analyst(
            session, entry_id="e1", envelope_id="env-1", verdict=PMVerdict.REJECT, ts=_PROPOSAL_TS
        )
        session.flush()
        since = _PROPOSAL_TS - timedelta(hours=1)
        results = _collect(session, as_of=_HORIZON_ELAPSED_AS_OF, since=since)
        assert len(results) == 1
