"""Retrospective report-save + decision-capture persistence (ALP-890 / story 07c).

The imperative shell behind the ``/feedback-retrospective`` skill's Phase 3 (report
rendering) and Phase 4 (decision capture):

* :func:`save_report` writes the ``retrospective_reports`` metadata row and the
  long-form markdown to ``data/retrospective_reports/{report_id}/report.md``
  (``state-persistence.md § Retrospective reports``). The record is metadata; the
  filesystem carries the content.
* :func:`capture_decision` writes one ``retrospective_decisions`` row per walkthrough
  decision (promotion-candidate or follow-up, accepted or rejected, optionally
  linking a spawned validation).

Both delegate the row encode/insert to the 02c repository helpers; this module owns
only the ID minting, the clock read, and (for the report) the file write.

``feedback_loop`` is read-only over *trading* state — these tables are the feedback
loop's own ledgers, not live trading-state records, so writing them respects the
read-only-over-trading-state contract.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from alphamind.feedback_loop.retrospective.records import (
    DecisionId,
    DecisionType,
    ReportId,
    RetrospectiveDecisionRecord,
    RetrospectiveReportRecord,
    Verdict,
)
from alphamind.state.repository.retrospective_queries import (
    insert_retrospective_decision,
    insert_retrospective_report,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session

    from alphamind.feedback_loop.dataset import WindowDataset

#: Filesystem subtree (under the data root) for persisted report markdown. The
#: canonical ``report_file_ref`` stored on the record is always rooted at ``data/``
#: per ``state-persistence.md § Retrospective reports``; the on-disk *data_root* is
#: injectable so tests write under a temp directory without diverging the ref.
_REPORTS_SUBDIR = "retrospective_reports"
_REPORT_FILENAME = "report.md"
_CANONICAL_DATA_ROOT = "data"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def save_report(
    session: Session,
    window: WindowDataset,
    markdown: str,
    *,
    session_id: str | None = None,
    data_root: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> RetrospectiveReportRecord:
    """Persist a retrospective report: the metadata row + the markdown file.

    Mints a fresh ``report_id``, writes *markdown* to
    ``{data_root}/retrospective_reports/{report_id}/report.md`` (creating parents),
    and queues a :class:`RetrospectiveReportRecord` for insert. The window bounds
    come from *window* (the Phase-1 :class:`WindowDataset`); *session_id* is the
    optional review-session under which Phase 3 rendered (nullable — the session
    surface is deferred, ALP-686). The caller owns the commit boundary.

    *data_root* defaults to the repo-relative ``data/`` directory so the on-disk
    location matches the canonical ``report_file_ref`` stored on the record; tests
    inject a temp directory. The returned record's ``report_file_ref`` is always the
    canonical ``data/``-rooted path regardless of *data_root*.
    """
    resolved_root = Path(_CANONICAL_DATA_ROOT) if data_root is None else data_root
    report_id = ReportId(f"retro-{uuid.uuid4().hex}")
    report_dir = resolved_root / _REPORTS_SUBDIR / report_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / _REPORT_FILENAME).write_text(markdown, encoding="utf-8")

    record = RetrospectiveReportRecord(
        report_id=report_id,
        window_start=window.start,
        window_end=window.end,
        generated_at=now(),
        generated_by_session_id=session_id,
        report_file_ref=(
            f"{_CANONICAL_DATA_ROOT}/{_REPORTS_SUBDIR}/{report_id}/{_REPORT_FILENAME}"
        ),
    )
    insert_retrospective_report(session, record)
    return record


def capture_decision(
    session: Session,
    report_id: ReportId,
    *,
    item_identifier: str,
    decision_type: DecisionType,
    verdict: Verdict,
    rationale: str,
    linked_validation_id: str | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> RetrospectiveDecisionRecord:
    """Persist one retrospective walkthrough decision.

    Mints a fresh ``decision_id`` and queues a :class:`RetrospectiveDecisionRecord`
    for insert against *report_id*. *linked_validation_id* is set only when the
    decision spawned a validation registration (nullable otherwise). The caller owns
    the commit boundary.
    """
    record = RetrospectiveDecisionRecord(
        decision_id=DecisionId(f"retrodec-{uuid.uuid4().hex}"),
        report_id=report_id,
        captured_at=now(),
        decision_type=decision_type,
        item_identifier=item_identifier,
        verdict=verdict,
        rationale=rationale,
        linked_validation_id=linked_validation_id,
    )
    insert_retrospective_decision(session, record)
    return record


__all__ = ["capture_decision", "save_report"]
