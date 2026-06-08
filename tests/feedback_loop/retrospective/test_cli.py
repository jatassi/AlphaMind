"""Retrospective CLI — ingest / save-report / capture-decision (ALP-890 / 07c).

The headless surface ``/feedback-retrospective`` drives. Each subcommand runs against
a temp-file SQLite DB (``--db-path``) and a temp filesystem (``--data-root``); the
tests exercise the full round-trip (DB rows + the markdown file) through ``main``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop.retrospective.cli import main
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
    read_decisions_for_report,
    read_retrospective_report,
)
from alphamind.state.repository.validation_queries import (
    insert_validation,
    insert_validation_outcome,
)

_NOW = datetime(2026, 4, 2, tzinfo=UTC)


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "retro_cli.db"
    engine = make_engine(str(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    yield path


def _seed_optional_pending(path: Path) -> None:
    engine: Engine = make_engine(str(path))
    sess: Session
    with make_session_factory(engine)() as sess:
        insert_validation(
            sess,
            ValidationRecord(
                validation_id=ValidationId("v-opt"),
                registered_at=datetime(2026, 2, 1, tzinfo=UTC),
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
                evaluation_due_at=datetime(2026, 2, 8, tzinfo=UTC),
                superseded_at=None,
                superseded_reason=None,
            ),
        )
        sess.flush()
        insert_validation_outcome(
            sess,
            ValidationOutcomeRecord(
                outcome_id=OutcomeId("o-opt"),
                validation_id=ValidationId("v-opt"),
                evaluated_at=datetime(2026, 2, 15, tzinfo=UTC),
                evaluated_by_session_id=None,
                verdict=OutcomeVerdict.DEGRADED,
                posterior_summary={"delta": -0.01},
                confounder_notes="regime straddle",
                narrative="degraded with confounder",
                rollback_status=RollbackStatus.OPTIONAL_PENDING_RETROSPECTIVE,
            ),
        )
        sess.commit()
    engine.dispose()


class TestIngest:
    def test_ingest_reports_pending_rollback_count(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_optional_pending(db_path)
        exit_code = main(
            [
                "ingest",
                "--db-path",
                str(db_path),
                "--start",
                "2026-01-01T00:00:00+00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "pending_rollbacks: 1" in out

    def test_ingest_emits_canonical_identifier_per_pending_rollback(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_optional_pending(db_path)
        exit_code = main(
            [
                "ingest",
                "--db-path",
                str(db_path),
                "--start",
                "2026-01-01T00:00:00+00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # The skill copies this identifier verbatim into capture-decision.
        assert "follow_up.rollback_prompts/decision/strategist.md" in out
        assert "prompts/decision/strategist.md" in out

    def test_ingest_rejects_naive_datetime(
        self, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "ingest",
                "--db-path",
                str(db_path),
                "--start",
                "2026-01-01T00:00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
            ]
        )
        assert exit_code == 1


class TestSaveReport:
    def test_save_report_writes_row_and_file(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        markdown_src = tmp_path / "draft.md"
        markdown_src.write_text("# Quarterly retrospective\n", encoding="utf-8")
        data_root = tmp_path / "data"

        exit_code = main(
            [
                "save-report",
                "--db-path",
                str(db_path),
                "--data-root",
                str(data_root),
                "--start",
                "2026-01-01T00:00:00+00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
                "--markdown-file",
                str(markdown_src),
            ]
        )
        assert exit_code == 0
        report_id = capsys.readouterr().out.strip()
        assert report_id.startswith("retro-")

        report_md = data_root / "retrospective_reports" / report_id / "report.md"
        assert report_md.read_text(encoding="utf-8") == "# Quarterly retrospective\n"

        engine = make_engine(str(db_path))
        with make_session_factory(engine)() as sess:
            from alphamind.feedback_loop.retrospective.records import ReportId

            stored = read_retrospective_report(sess, ReportId(report_id))
        engine.dispose()
        assert stored is not None
        assert stored.report_file_ref.endswith(f"{report_id}/report.md")


class TestCaptureDecision:
    def test_capture_decision_writes_row(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        markdown_src = tmp_path / "draft.md"
        markdown_src.write_text("# r\n", encoding="utf-8")
        main(
            [
                "save-report",
                "--db-path",
                str(db_path),
                "--data-root",
                str(tmp_path / "data"),
                "--start",
                "2026-01-01T00:00:00+00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
                "--markdown-file",
                str(markdown_src),
            ]
        )
        report_id = capsys.readouterr().out.strip()

        exit_code = main(
            [
                "capture-decision",
                "--db-path",
                str(db_path),
                "--report-id",
                report_id,
                "--item-identifier",
                "candidate.synth_drop_rate",
                "--decision-type",
                "promotion_candidate",
                "--verdict",
                "accepted",
                "--rationale",
                "worth instrumenting",
            ]
        )
        assert exit_code == 0
        decision_id = capsys.readouterr().out.strip()
        assert decision_id.startswith("retrodec-")

        from alphamind.feedback_loop.retrospective.records import ReportId

        engine = make_engine(str(db_path))
        with make_session_factory(engine)() as sess:
            stored = read_decisions_for_report(sess, ReportId(report_id))
        engine.dispose()
        assert len(stored) == 1
        assert stored[0].item_identifier == "candidate.synth_drop_rate"
        assert stored[0].decision_id == decision_id

    def test_capture_decision_links_validation(
        self, db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_optional_pending(db_path)
        markdown_src = tmp_path / "draft.md"
        markdown_src.write_text("# r\n", encoding="utf-8")
        main(
            [
                "save-report",
                "--db-path",
                str(db_path),
                "--data-root",
                str(tmp_path / "data"),
                "--start",
                "2026-01-01T00:00:00+00:00",
                "--end",
                "2026-04-01T00:00:00+00:00",
                "--markdown-file",
                str(markdown_src),
            ]
        )
        report_id = capsys.readouterr().out.strip()

        exit_code = main(
            [
                "capture-decision",
                "--db-path",
                str(db_path),
                "--report-id",
                report_id,
                "--item-identifier",
                "follow_up.rollback_prompts_decision_strategist_md",
                "--decision-type",
                "follow_up",
                "--verdict",
                "accepted",
                "--rationale",
                "roll back",
                "--linked-validation-id",
                "v-opt",
            ]
        )
        assert exit_code == 0

        from alphamind.feedback_loop.retrospective.records import ReportId

        engine = make_engine(str(db_path))
        with make_session_factory(engine)() as sess:
            stored = read_decisions_for_report(sess, ReportId(report_id))
        engine.dispose()
        assert stored[0].linked_validation_id == "v-opt"
