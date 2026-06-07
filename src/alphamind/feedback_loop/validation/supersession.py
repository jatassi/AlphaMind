"""Mid-window supersession detector (ALP-892 / story 08b).

A registered validation is a contract over a specific conditioning context. When
that context shifts materially during the post-edit window the contract is
structurally broken — the post-edit data is no longer comparable to the pre-edit
baseline — so the system auto-marks the validation ``superseded`` per
``docs/design/feedback-loop.md`` § Mid-window supersession.

:func:`detect_supersessions` is the importable seam (the CLI ``detect-supersessions``
subcommand and a later scheduler wiring both drive it). For each active validation —
pending, non-superseded, unevaluated — it inspects the post-edit window
``[registered_at, evaluation_due_at)`` for, in documented order:

1. ``regime_transition`` — an invocation whose ``active_regime`` differs from
   ``registered_regime``,
2. ``model_version_change`` — an ``agent_calls.model_id`` differing from
   ``registered_model_id``,
3. ``concurrent_edit_on_watched_artifact`` — a commit to the validation's
   ``edited_artifact`` git path landing in the window.

Only the first-firing trigger is recorded; supersession is permanent.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from alphamind.feedback_loop.validation.records import SupersededReason
from alphamind.state.repository.validation_queries import (
    mark_validation_superseded,
    read_pending_validations,
)
from alphamind.state.tables.agent_calls import AgentCallsRow
from alphamind.state.tables.invocations import InvocationRow

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from sqlalchemy.orm import Session

    from alphamind.feedback_loop.validation.records import ValidationRecord

# Length of the ``YYYY-MM-DDTHH:MM:SS`` second-precision prefix shared by every
# ISO-8601 timestamp the codebase writes, regardless of its suffix. Matches
# ``agent_calls_queries._SECOND_PREFIX_LEN`` — ``invocations.start_at`` is written
# by two paths at differing sub-second precision, so a faithful range filter
# compares second prefixes, not the raw column.
_SECOND_PREFIX_LEN = 19


def _second_prefix(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _post_window_regimes(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> tuple[str, ...]:
    """Distinct ``active_regime`` labels for invocations in ``[start, end)``."""
    start_at_prefix = func.substr(InvocationRow.start_at, 1, _SECOND_PREFIX_LEN)
    stmt = (
        select(InvocationRow.active_regime)
        .where(
            start_at_prefix >= _second_prefix(start),
            start_at_prefix < _second_prefix(end),
        )
        .distinct()
    )
    return tuple(session.execute(stmt).scalars().all())


def detect_supersessions(
    session: Session,
    *,
    repo_root: Path | None = None,
) -> int:
    """Mark each active validation crossed by a conditioning shift ``superseded``.

    Iterates the pending (non-superseded, unevaluated) validations, evaluates the
    three triggers in documented order over the post-edit window, and writes the
    first-firing trigger via ``mark_validation_superseded``. Returns the count
    marked. Mutations are queued on *session*; the caller owns the commit.

    ``repo_root`` overrides the git working tree the concurrent-edit trigger walks
    (default: the process CWD) so tests can point at a throwaway repo.
    """
    marked = 0
    for validation in read_pending_validations(session):
        reason = _first_firing_trigger(session, validation, repo_root=repo_root)
        if reason is not None:
            mark_validation_superseded(
                session,
                validation.validation_id,
                reason,
                datetime.now(UTC),
            )
            marked += 1
    return marked


def _first_firing_trigger(
    session: Session,
    validation: ValidationRecord,
    *,
    repo_root: Path | None,
) -> SupersededReason | None:
    """The first trigger that fired over *validation*'s post-edit window, or ``None``."""
    start = validation.registered_at
    end = validation.evaluation_due_at

    regimes = _post_window_regimes(session, start=start, end=end)
    if any(regime != validation.registered_regime for regime in regimes):
        return SupersededReason.REGIME_TRANSITION

    models = _post_window_models(session, start=start, end=end)
    if any(model != validation.registered_model_id for model in models):
        return SupersededReason.MODEL_VERSION_CHANGE

    if _artifact_committed_in_window(
        validation.edited_artifact, start=start, end=end, repo_root=repo_root
    ):
        return SupersededReason.CONCURRENT_EDIT_ON_WATCHED_ARTIFACT

    return None


def _post_window_models(
    session: Session,
    *,
    start: datetime,
    end: datetime,
) -> tuple[str, ...]:
    """Distinct ``agent_calls.model_id`` values for invocations in ``[start, end)``."""
    start_at_prefix = func.substr(InvocationRow.start_at, 1, _SECOND_PREFIX_LEN)
    stmt = (
        select(AgentCallsRow.model_id)
        .join(InvocationRow, AgentCallsRow.invocation_id == InvocationRow.invocation_id)
        .where(
            start_at_prefix >= _second_prefix(start),
            start_at_prefix < _second_prefix(end),
        )
        .distinct()
    )
    return tuple(session.execute(stmt).scalars().all())


def _artifact_committed_in_window(
    edited_artifact: str,
    *,
    start: datetime,
    end: datetime,
    repo_root: Path | None,
) -> bool:
    """Whether a commit touching *edited_artifact* landed in ``[start, end)``.

    Walks the artifact's git log over the window via ``git log --since/--until``
    restricted to the artifact path. ``[start, end)`` — ``--until`` is exclusive
    by trimming one second, matching the half-open window semantics the rest of
    the detector uses. A git failure (not a repo, path never tracked) reads as
    "no commit", so the trigger fails closed to *not superseded* rather than
    raising.
    """
    completed = _git_log_commits(edited_artifact, start=start, end=end, repo_root=repo_root)
    return bool(completed)


def _git_log_commits(
    edited_artifact: str,
    *,
    start: datetime,
    end: datetime,
    repo_root: Path | None,
) -> Iterable[str]:
    """Commit hashes touching *edited_artifact* in ``[start, end)`` (empty on failure)."""
    # ``git log --until`` is inclusive; trim one second to make the upper bound
    # exclusive, matching the half-open ``[start, end)`` SQL filters above.
    until = end.astimezone(UTC) - timedelta(seconds=1)
    args = [
        "git",
        "log",
        "--format=%H",
        f"--since={start.astimezone(UTC).isoformat()}",
        f"--until={until.isoformat()}",
        "--",
        edited_artifact,
    ]
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, path is a column value
            args,
            cwd=None if repo_root is None else str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(line for line in completed.stdout.splitlines() if line)


__all__ = ["detect_supersessions"]
