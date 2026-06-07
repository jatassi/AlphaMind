"""``read_unresolved_optional_pending_rollbacks`` — the retrospective follow-up pull
(ALP-890 / story 07c).

The retrospective ingestion surfaces every ``optional_pending_retrospective`` validation
outcome that the operator has *not* yet resolved with a ``decision_type='follow_up'``
retrospective decision. "Resolved" is keyed on the follow-up decision's
``item_identifier`` (``follow_up.rollback_<artifact_slug>`` per the
``feedback-retrospective`` SKILL), NOT on ``linked_validation_id`` — a *rejected*
follow-up resolves the outcome but spawns no validation, so it carries a null
``linked_validation_id``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.retrospective.records import (
    DecisionId,
    DecisionType,
    ReportId,
    RetrospectiveDecisionRecord,
    RetrospectiveReportRecord,
)
from alphamind.feedback_loop.retrospective.records import (
    Verdict as DecisionVerdict,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    OutcomeId,
    RollbackStatus,
    ValidationId,
    ValidationOutcomeRecord,
    ValidationRecord,
)
from alphamind.feedback_loop.validation.records import (
    Verdict as OutcomeVerdict,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.retrospective_queries import (
    insert_retrospective_decision,
    insert_retrospective_report,
)
from alphamind.state.repository.validation_queries import (
    insert_validation,
    insert_validation_outcome,
    read_unresolved_optional_pending_rollbacks,
    rollback_followup_identifier,
)


def _validation(validation_id: str, edited_artifact: str) -> ValidationRecord:
    return ValidationRecord(
        validation_id=ValidationId(validation_id),
        registered_at=datetime(2026, 6, 1, tzinfo=UTC),
        registered_by_session_id=None,
        edited_artifact=edited_artifact,
        pre_edit_version="abc1234",
        post_edit_version="def5678",
        registered_regime="normal",
        registered_model_id="claude-sonnet-4-5",
        watched_metric_ids=(MetricId("win_rate"),),
        window_length_days=7,
        expected_direction=ExpectedDirection.IMPROVED,
        expected_magnitude="5% relative",
        success_criterion="win_rate up 5%",
        failure_criterion="win_rate down 3%",
        evaluation_due_at=datetime(2026, 6, 8, tzinfo=UTC),
        superseded_at=None,
        superseded_reason=None,
    )


def _outcome(
    outcome_id: str,
    validation_id: str,
    rollback_status: RollbackStatus,
) -> ValidationOutcomeRecord:
    return ValidationOutcomeRecord(
        outcome_id=OutcomeId(outcome_id),
        validation_id=ValidationId(validation_id),
        evaluated_at=datetime(2026, 6, 10, tzinfo=UTC),
        evaluated_by_session_id=None,
        verdict=OutcomeVerdict.DEGRADED,
        posterior_summary={"delta": -0.02},
        confounder_notes="regime straddle",
        narrative="degraded with confounder",
        rollback_status=rollback_status,
    )


def _report(report_id: str) -> RetrospectiveReportRecord:
    return RetrospectiveReportRecord(
        report_id=ReportId(report_id),
        window_start=datetime(2026, 1, 1, tzinfo=UTC),
        window_end=datetime(2026, 4, 1, tzinfo=UTC),
        generated_at=datetime(2026, 4, 2, tzinfo=UTC),
        generated_by_session_id=None,
        report_file_ref=f"data/retrospective_reports/{report_id}/report.md",
    )


def _followup_decision(
    decision_id: str,
    report_id: str,
    item_identifier: str,
    verdict: DecisionVerdict,
    linked_validation_id: str | None,
) -> RetrospectiveDecisionRecord:
    return RetrospectiveDecisionRecord(
        decision_id=DecisionId(decision_id),
        report_id=ReportId(report_id),
        captured_at=datetime(2026, 4, 2, 1, tzinfo=UTC),
        decision_type=DecisionType.FOLLOW_UP,
        item_identifier=item_identifier,
        verdict=verdict,
        rationale="operator call",
        linked_validation_id=linked_validation_id,
    )


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    db_path = tmp_path / "rollback_pull.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as sess:
        yield sess
    engine.dispose()


class TestRollbackFollowupIdentifier:
    def test_identifier_is_prefixed_and_slugged(self) -> None:
        ident = rollback_followup_identifier("prompts/decision/strategist.md")
        assert ident == "follow_up.rollback_prompts_decision_strategist_md"

    def test_identifier_is_deterministic(self) -> None:
        artifact = "config/distillation/q1.yaml"
        assert rollback_followup_identifier(artifact) == rollback_followup_identifier(artifact)


class TestReadUnresolvedOptionalPendingRollbacks:
    def test_returns_only_optional_pending_outcomes(self, session: Session) -> None:
        insert_validation(session, _validation("v-opt", "prompts/decision/strategist.md"))
        insert_validation(session, _validation("v-mand", "prompts/analysis/analyst.md"))
        session.flush()
        insert_validation_outcome(
            session,
            _outcome("o-opt", "v-opt", RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE),
        )
        insert_validation_outcome(
            session,
            _outcome("o-mand", "v-mand", RollbackStatus.MANDATORY_CLEAN_FAILURE),
        )
        session.commit()

        pending = read_unresolved_optional_pending_rollbacks(session)

        assert {o.outcome_id for o in pending} == {OutcomeId("o-opt")}

    def test_excludes_outcome_resolved_by_accepted_followup(self, session: Session) -> None:
        artifact = "prompts/decision/strategist.md"
        insert_validation(session, _validation("v-opt", artifact))
        insert_validation(session, _validation("v-rollback-paired", artifact))
        insert_retrospective_report(session, _report("r-1"))
        session.flush()
        insert_validation_outcome(
            session,
            _outcome("o-opt", "v-opt", RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE),
        )
        insert_retrospective_decision(
            session,
            _followup_decision(
                "d-1",
                "r-1",
                rollback_followup_identifier(artifact),
                DecisionVerdict.ACCEPTED,
                linked_validation_id="v-rollback-paired",
            ),
        )
        session.commit()

        pending = read_unresolved_optional_pending_rollbacks(session)

        assert pending == ()

    def test_excludes_outcome_resolved_by_rejected_followup(self, session: Session) -> None:
        """A *rejected* follow-up resolves the outcome despite null linked_validation_id."""
        artifact = "prompts/decision/strategist.md"
        insert_validation(session, _validation("v-opt", artifact))
        insert_retrospective_report(session, _report("r-1"))
        session.flush()
        insert_validation_outcome(
            session,
            _outcome("o-opt", "v-opt", RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE),
        )
        insert_retrospective_decision(
            session,
            _followup_decision(
                "d-1",
                "r-1",
                rollback_followup_identifier(artifact),
                DecisionVerdict.REJECTED,
                linked_validation_id=None,
            ),
        )
        session.commit()

        pending = read_unresolved_optional_pending_rollbacks(session)

        assert pending == ()

    def test_unresolved_outcome_survives_unrelated_followup(self, session: Session) -> None:
        artifact = "prompts/decision/strategist.md"
        insert_validation(session, _validation("v-opt", artifact))
        insert_retrospective_report(session, _report("r-1"))
        session.flush()
        insert_validation_outcome(
            session,
            _outcome("o-opt", "v-opt", RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE),
        )
        insert_retrospective_decision(
            session,
            _followup_decision(
                "d-1",
                "r-1",
                rollback_followup_identifier("prompts/analysis/analyst.md"),
                DecisionVerdict.ACCEPTED,
                linked_validation_id=None,
            ),
        )
        session.commit()

        pending = read_unresolved_optional_pending_rollbacks(session)

        assert {o.outcome_id for o in pending} == {OutcomeId("o-opt")}

    def test_empty_when_no_outcomes(self, session: Session) -> None:
        assert read_unresolved_optional_pending_rollbacks(session) == ()
