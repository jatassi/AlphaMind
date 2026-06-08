"""Tests for the retrospective_reports + retrospective_decisions tables,
codecs, and repository helpers (ALP-875 / story 02c).

Covers:
* ``RetrospectiveReportRecord`` + ``RetrospectiveReportsRow`` round-trip codec.
* ``RetrospectiveDecisionRecord`` + ``RetrospectiveDecisionsRow`` round-trip
  codec, including nullable ``linked_validation_id``.
* Nullable columns survive a real INSERT → SELECT.
* CHECK constraints fire for out-of-vocabulary enum values.
* FK constraint: ``retrospective_decisions.report_id`` → ``retrospective_reports``.
* FK constraint: ``retrospective_decisions.linked_validation_id`` →
  ``validations`` (nullable, tested both null and non-null).
* ``read_decisions_for_report`` returns only that report's decisions.
* ``read_unresolved_followup_decisions`` returns follow-up decisions with no
  linked validation (unresolved predicate).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.retrospective.records import (
    DecisionId,
    DecisionType,
    ReportId,
    RetrospectiveDecisionRecord,
    RetrospectiveReportRecord,
    Verdict,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.retrospective_queries import (
    insert_retrospective_decision,
    insert_retrospective_report,
    read_decisions_for_report,
    read_retrospective_report,
    read_unresolved_followup_decisions,
)
from alphamind.state.tables.retrospective_decisions import RetrospectiveDecisionsRow

# ---------------------------------------------------------------------------
# Timestamps / IDs used throughout
# ---------------------------------------------------------------------------

_TS_WIN_START = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
_TS_WIN_END = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)
_TS_GENERATED = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
_TS_CAPTURED = datetime(2026, 6, 2, 9, 0, 0, tzinfo=UTC)
_TS_CAPTURED2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=UTC)
_TS_CAPTURED3 = datetime(2026, 6, 2, 11, 0, 0, tzinfo=UTC)

_REPORT_ID_1 = ReportId("rpt-001")
_REPORT_ID_2 = ReportId("rpt-002")
_DEC_ID_1 = DecisionId("dec-001")
_DEC_ID_2 = DecisionId("dec-002")
_DEC_ID_3 = DecisionId("dec-003")
_DEC_ID_4 = DecisionId("dec-004")

_VALIDATION_ID = "val-001"
_FILE_REF_1 = "data/retrospective_reports/rpt-001/report.md"
_FILE_REF_2 = "data/retrospective_reports/rpt-002/report.md"


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_report(
    report_id: ReportId = _REPORT_ID_1,
    *,
    generated_by_session_id: str | None = None,
    file_ref: str = _FILE_REF_1,
) -> RetrospectiveReportRecord:
    return RetrospectiveReportRecord(
        report_id=report_id,
        window_start=_TS_WIN_START,
        window_end=_TS_WIN_END,
        generated_at=_TS_GENERATED,
        generated_by_session_id=generated_by_session_id,
        report_file_ref=file_ref,
    )


def _make_decision(
    decision_id: DecisionId = _DEC_ID_1,
    report_id: ReportId = _REPORT_ID_1,
    *,
    decision_type: DecisionType = DecisionType.PROMOTION_CANDIDATE,
    verdict: Verdict = Verdict.ACCEPTED,
    linked_validation_id: str | None = None,
    captured_at: datetime = _TS_CAPTURED,
) -> RetrospectiveDecisionRecord:
    return RetrospectiveDecisionRecord(
        decision_id=decision_id,
        report_id=report_id,
        captured_at=captured_at,
        decision_type=decision_type,
        item_identifier="candidate-A",
        verdict=verdict,
        rationale="Strong evidence of improvement",
        linked_validation_id=linked_validation_id,
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
# Helper: seed a validations row (FK target for linked_validation_id)
# ---------------------------------------------------------------------------


def _seed_validation(session: Session, validation_id: str = _VALIDATION_ID) -> None:
    """Insert a minimal validations row so the FK resolves."""
    from alphamind.state.tables.validations import ValidationsRow

    row = ValidationsRow(
        validation_id=validation_id,
        registered_at="2026-05-01T00:00:00Z",
        registered_by_session_id=None,
        edited_artifact="prompts/decision/analyst.md",
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime="normal",
        registered_model_id="claude-sonnet-4-5",
        watched_metric_ids_json='["win_rate_30d"]',
        window_length_days=7,
        expected_direction="improved",
        expected_magnitude="small",
        success_criterion="win rate improves",
        failure_criterion="win rate degrades",
        evaluation_due_at="2026-05-08T00:00:00Z",
        superseded_at=None,
        superseded_reason=None,
    )
    session.add(row)
    session.flush()


# ---------------------------------------------------------------------------
# Slice 1 — RetrospectiveReportRecord codec round-trip
# ---------------------------------------------------------------------------


def test_report_codec_round_trip_no_session_id(session: Session) -> None:
    """Codec round-trips a report with nullable generated_by_session_id=None."""
    record = _make_report(generated_by_session_id=None)
    insert_retrospective_report(session, record)
    session.flush()

    result = read_retrospective_report(session, _REPORT_ID_1)
    assert result is not None
    assert result.report_id == _REPORT_ID_1
    assert result.window_start == _TS_WIN_START
    assert result.window_end == _TS_WIN_END
    assert result.generated_at == _TS_GENERATED
    assert result.generated_by_session_id is None
    assert result.report_file_ref == _FILE_REF_1


def test_report_codec_round_trip_with_session_id(session: Session) -> None:
    """Codec round-trips a report with generated_by_session_id set."""
    record = _make_report(generated_by_session_id="session-abc")
    insert_retrospective_report(session, record)
    session.flush()

    result = read_retrospective_report(session, _REPORT_ID_1)
    assert result is not None
    assert result.generated_by_session_id == "session-abc"


def test_read_retrospective_report_missing_returns_none(session: Session) -> None:
    """read_retrospective_report returns None for a non-existent ID."""
    result = read_retrospective_report(session, ReportId("does-not-exist"))
    assert result is None


# ---------------------------------------------------------------------------
# Slice 2 — RetrospectiveDecisionRecord codec round-trip
# ---------------------------------------------------------------------------


def test_decision_codec_round_trip_no_linked_validation(session: Session) -> None:
    """Codec round-trips a promotion_candidate decision with no linked validation."""
    insert_retrospective_report(session, _make_report())
    session.flush()

    record = _make_decision(linked_validation_id=None)
    insert_retrospective_decision(session, record)
    session.flush()

    rows = session.query(RetrospectiveDecisionsRow).all()
    assert len(rows) == 1
    decoded = _decode_decision(rows[0])
    assert decoded.decision_id == _DEC_ID_1
    assert decoded.report_id == _REPORT_ID_1
    assert decoded.decision_type == DecisionType.PROMOTION_CANDIDATE
    assert decoded.verdict == Verdict.ACCEPTED
    assert decoded.linked_validation_id is None
    assert decoded.captured_at == _TS_CAPTURED


def test_decision_codec_round_trip_with_linked_validation(session: Session) -> None:
    """Codec round-trips a follow-up decision with a non-null linked_validation_id."""
    _seed_validation(session)
    insert_retrospective_report(session, _make_report())
    session.flush()

    record = _make_decision(
        decision_type=DecisionType.FOLLOW_UP,
        verdict=Verdict.REJECTED,
        linked_validation_id=_VALIDATION_ID,
    )
    insert_retrospective_decision(session, record)
    session.flush()

    rows = session.query(RetrospectiveDecisionsRow).all()
    assert len(rows) == 1
    decoded = _decode_decision(rows[0])
    assert decoded.decision_type == DecisionType.FOLLOW_UP
    assert decoded.verdict == Verdict.REJECTED
    assert decoded.linked_validation_id == _VALIDATION_ID


def _decode_decision(row: RetrospectiveDecisionsRow) -> RetrospectiveDecisionRecord:
    """Import codec inline to avoid cluttering module-level imports."""
    from alphamind.state.tables.retrospective_decisions_codec import row_to_record

    return row_to_record(row)


# ---------------------------------------------------------------------------
# Slice 3 — CHECK constraints enforce closed-set vocabularies
# ---------------------------------------------------------------------------


def test_decision_type_check_constraint(session: Session) -> None:
    """Invalid decision_type raises IntegrityError at flush."""
    insert_retrospective_report(session, _make_report())
    session.flush()

    bad_row = RetrospectiveDecisionsRow(
        decision_id="bad-dec",
        report_id=str(_REPORT_ID_1),
        captured_at="2026-06-02T09:00:00Z",
        decision_type="invalid_type",
        item_identifier="item-X",
        verdict="accepted",
        rationale="reason",
        linked_validation_id=None,
    )
    session.add(bad_row)
    with pytest.raises(IntegrityError):
        session.flush()


def test_verdict_check_constraint(session: Session) -> None:
    """Invalid verdict raises IntegrityError at flush."""
    insert_retrospective_report(session, _make_report())
    session.flush()

    bad_row = RetrospectiveDecisionsRow(
        decision_id="bad-dec-2",
        report_id=str(_REPORT_ID_1),
        captured_at="2026-06-02T09:00:00Z",
        decision_type="promotion_candidate",
        item_identifier="item-Y",
        verdict="maybe",
        rationale="reason",
        linked_validation_id=None,
    )
    session.add(bad_row)
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Slice 4 — FK: report_id → retrospective_reports
# ---------------------------------------------------------------------------


def test_decision_fk_report_id_enforced(session: Session) -> None:
    """Inserting a decision for a non-existent report_id raises IntegrityError."""
    bad_row = RetrospectiveDecisionsRow(
        decision_id="dec-orphan",
        report_id="rpt-does-not-exist",
        captured_at="2026-06-02T09:00:00Z",
        decision_type="promotion_candidate",
        item_identifier="item-Z",
        verdict="accepted",
        rationale="reason",
        linked_validation_id=None,
    )
    session.add(bad_row)
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Slice 5 — FK: linked_validation_id → validations (nullable)
# ---------------------------------------------------------------------------


def test_linked_validation_id_fk_enforced(session: Session) -> None:
    """Non-null linked_validation_id pointing at missing validation raises IntegrityError."""
    insert_retrospective_report(session, _make_report())
    session.flush()

    bad_row = RetrospectiveDecisionsRow(
        decision_id="dec-bad-val",
        report_id=str(_REPORT_ID_1),
        captured_at="2026-06-02T09:00:00Z",
        decision_type="follow_up",
        item_identifier="item-Q",
        verdict="accepted",
        rationale="reason",
        linked_validation_id="val-does-not-exist",
    )
    session.add(bad_row)
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------------------
# Slice 6 — read_decisions_for_report
# ---------------------------------------------------------------------------


def test_read_decisions_for_report_returns_only_that_report(session: Session) -> None:
    """read_decisions_for_report returns only decisions for the given report_id."""
    # Insert two reports
    insert_retrospective_report(session, _make_report(_REPORT_ID_1, file_ref=_FILE_REF_1))
    insert_retrospective_report(session, _make_report(_REPORT_ID_2, file_ref=_FILE_REF_2))
    session.flush()

    # Two decisions on report 1, one on report 2
    insert_retrospective_decision(
        session,
        _make_decision(_DEC_ID_1, _REPORT_ID_1, captured_at=_TS_CAPTURED),
    )
    insert_retrospective_decision(
        session,
        _make_decision(_DEC_ID_2, _REPORT_ID_1, captured_at=_TS_CAPTURED2),
    )
    insert_retrospective_decision(
        session,
        _make_decision(_DEC_ID_3, _REPORT_ID_2, captured_at=_TS_CAPTURED3),
    )
    session.flush()

    results = read_decisions_for_report(session, _REPORT_ID_1)
    assert len(results) == 2
    ids = {r.decision_id for r in results}
    assert ids == {_DEC_ID_1, _DEC_ID_2}
    # Confirm report 2's decision is absent
    assert all(r.report_id == _REPORT_ID_1 for r in results)


def test_read_decisions_for_report_empty_when_none(session: Session) -> None:
    """read_decisions_for_report returns empty tuple for a report with no decisions."""
    insert_retrospective_report(session, _make_report())
    session.flush()

    results = read_decisions_for_report(session, _REPORT_ID_1)
    assert results == ()


# ---------------------------------------------------------------------------
# Slice 7 — read_unresolved_followup_decisions
# ---------------------------------------------------------------------------


def test_read_unresolved_followup_decisions_returns_followup_without_linked_validation(
    session: Session,
) -> None:
    """read_unresolved_followup_decisions: follow-up with no linked_validation_id."""
    _seed_validation(session)
    insert_retrospective_report(session, _make_report())
    session.flush()

    # follow-up, no linked validation → unresolved
    insert_retrospective_decision(
        session,
        _make_decision(
            _DEC_ID_1,
            decision_type=DecisionType.FOLLOW_UP,
            linked_validation_id=None,
        ),
    )
    # follow-up, with linked validation → resolved (not returned)
    insert_retrospective_decision(
        session,
        _make_decision(
            _DEC_ID_2,
            decision_type=DecisionType.FOLLOW_UP,
            linked_validation_id=_VALIDATION_ID,
            captured_at=_TS_CAPTURED2,
        ),
    )
    # promotion_candidate, no linked validation → not a follow-up (not returned)
    insert_retrospective_decision(
        session,
        _make_decision(
            _DEC_ID_3,
            decision_type=DecisionType.PROMOTION_CANDIDATE,
            linked_validation_id=None,
            captured_at=_TS_CAPTURED3,
        ),
    )
    session.flush()

    results = read_unresolved_followup_decisions(session)
    assert len(results) == 1
    assert results[0].decision_id == _DEC_ID_1
    assert results[0].decision_type == DecisionType.FOLLOW_UP
    assert results[0].linked_validation_id is None


def test_read_unresolved_followup_decisions_empty_when_all_resolved(
    session: Session,
) -> None:
    """read_unresolved_followup_decisions returns empty when all follow-ups have validations."""
    _seed_validation(session)
    insert_retrospective_report(session, _make_report())
    session.flush()

    insert_retrospective_decision(
        session,
        _make_decision(
            _DEC_ID_1,
            decision_type=DecisionType.FOLLOW_UP,
            linked_validation_id=_VALIDATION_ID,
        ),
    )
    session.flush()

    results = read_unresolved_followup_decisions(session)
    assert results == ()
