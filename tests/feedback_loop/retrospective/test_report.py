"""``save_report`` + ``capture_decision`` — retrospective persistence (ALP-890 / 07c).

``save_report`` writes a ``retrospective_reports`` row and the long-form markdown to
``{data_root}/retrospective_reports/{report_id}/report.md``. ``capture_decision``
writes a ``retrospective_decisions`` row. Both round-trip through the 02c repository
helpers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.dataset import WindowDataset
from alphamind.feedback_loop.retrospective.records import (
    DecisionType,
    RetrospectiveDecisionRecord,
    RetrospectiveReportRecord,
    Verdict,
)
from alphamind.feedback_loop.retrospective.report import (
    capture_decision,
    save_report,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    ValidationId,
    ValidationRecord,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.repository.retrospective_queries import (
    read_decisions_for_report,
    read_retrospective_report,
)
from alphamind.state.repository.validation_queries import insert_validation

_WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 4, 1, tzinfo=UTC)
_NOW = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)


def _window() -> WindowDataset:
    return WindowDataset(
        start=_WINDOW_START,
        end=_WINDOW_END,
        agent_calls=(),
        pm_decision_log=(),
        validations=(),
    )


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    db_path = tmp_path / "report.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as sess:
        yield sess
    engine.dispose()


class TestSaveReport:
    def test_writes_row_and_markdown_file(self, session: Session, tmp_path: Path) -> None:
        markdown = "# Quarterly retrospective\n\nsome findings\n"
        record = save_report(
            session,
            _window(),
            markdown,
            data_root=tmp_path,
            now=lambda: _NOW,
        )
        session.commit()

        report_md = tmp_path / "retrospective_reports" / record.report_id / "report.md"
        assert report_md.read_text(encoding="utf-8") == markdown

        stored = read_retrospective_report(session, record.report_id)
        assert stored is not None
        assert stored.window_start == _WINDOW_START
        assert stored.window_end == _WINDOW_END
        assert stored.generated_at == _NOW

    def test_report_file_ref_is_canonical_relative_path(
        self, session: Session, tmp_path: Path
    ) -> None:
        record = save_report(
            session, _window(), "# r\n", data_root=tmp_path, now=lambda: _NOW
        )
        assert (
            record.report_file_ref
            == f"data/retrospective_reports/{record.report_id}/report.md"
        )

    def test_session_id_is_nullable(self, session: Session, tmp_path: Path) -> None:
        record = save_report(
            session, _window(), "# r\n", data_root=tmp_path, now=lambda: _NOW
        )
        assert record.generated_by_session_id is None

    def test_session_id_persists_when_supplied(
        self, session: Session, tmp_path: Path
    ) -> None:
        record = save_report(
            session,
            _window(),
            "# r\n",
            session_id="review-session-7",
            data_root=tmp_path,
            now=lambda: _NOW,
        )
        session.commit()
        stored = read_retrospective_report(session, record.report_id)
        assert stored is not None
        assert stored.generated_by_session_id == "review-session-7"

    def test_report_ids_are_unique(self, session: Session, tmp_path: Path) -> None:
        r1 = save_report(session, _window(), "# a\n", data_root=tmp_path, now=lambda: _NOW)
        r2 = save_report(session, _window(), "# b\n", data_root=tmp_path, now=lambda: _NOW)
        assert r1.report_id != r2.report_id

    def test_returns_record_type(self, session: Session, tmp_path: Path) -> None:
        record = save_report(
            session, _window(), "# r\n", data_root=tmp_path, now=lambda: _NOW
        )
        assert isinstance(record, RetrospectiveReportRecord)


class TestCaptureDecision:
    def _saved_report(self, session: Session, tmp_path: Path) -> str:
        record = save_report(
            session, _window(), "# r\n", data_root=tmp_path, now=lambda: _NOW
        )
        session.flush()
        return record.report_id

    def test_writes_decision_row(self, session: Session, tmp_path: Path) -> None:
        report_id = self._saved_report(session, tmp_path)
        decision = capture_decision(
            session,
            report_id,
            item_identifier="candidate.synth_drop_rate",
            decision_type=DecisionType.PROMOTION_CANDIDATE,
            verdict=Verdict.ACCEPTED,
            rationale="worth instrumenting",
            now=lambda: _NOW,
        )
        session.commit()

        stored = read_decisions_for_report(session, report_id)
        assert len(stored) == 1
        assert stored[0].decision_id == decision.decision_id
        assert stored[0].decision_type == DecisionType.PROMOTION_CANDIDATE
        assert stored[0].verdict == Verdict.ACCEPTED
        assert stored[0].item_identifier == "candidate.synth_drop_rate"
        assert stored[0].linked_validation_id is None

    def test_links_validation_when_supplied(self, session: Session, tmp_path: Path) -> None:
        insert_validation(
            session,
            ValidationRecord(
                validation_id=ValidationId("v-paired"),
                registered_at=_NOW,
                registered_by_session_id=None,
                edited_artifact="prompts/decision/strategist.md",
                pre_edit_version="a",
                post_edit_version="b",
                registered_regime="normal",
                registered_model_id="claude-sonnet-4-5",
                watched_metric_ids=(MetricId("win_rate"),),
                window_length_days=7,
                expected_direction=ExpectedDirection.IMPROVED,
                expected_magnitude="5%",
                success_criterion="up",
                failure_criterion="down",
                evaluation_due_at=_NOW,
                superseded_at=None,
                superseded_reason=None,
            ),
        )
        report_id = self._saved_report(session, tmp_path)
        capture_decision(
            session,
            report_id,
            item_identifier="follow_up.rollback_prompts_decision_strategist_md",
            decision_type=DecisionType.FOLLOW_UP,
            verdict=Verdict.ACCEPTED,
            rationale="roll back",
            linked_validation_id="v-paired",
            now=lambda: _NOW,
        )
        session.commit()

        stored = read_decisions_for_report(session, report_id)
        assert stored[0].linked_validation_id == "v-paired"

    def test_returns_record_type(self, session: Session, tmp_path: Path) -> None:
        report_id = self._saved_report(session, tmp_path)
        decision = capture_decision(
            session,
            report_id,
            item_identifier="x",
            decision_type=DecisionType.FOLLOW_UP,
            verdict=Verdict.REJECTED,
            rationale="no",
            now=lambda: _NOW,
        )
        assert isinstance(decision, RetrospectiveDecisionRecord)
