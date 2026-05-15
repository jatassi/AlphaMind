"""SQLAlchemy mapping for the ``process_lifetimes`` table (story 02b).

One row per long-running process start (pipeline / monitor). Captures
immutable runtime provenance — code version, dependency set, SDK versions —
so the feedback loop can isolate behavior shifts caused by code or
dependency changes from those caused by prompt or config changes.

Append-only; the row is INSERT-ed once at process start. Referenced by
``invocations.process_lifetime_id`` with ``ON DELETE RESTRICT``.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Process role vocabulary mirrored in ``state-persistence.md`` § Process
# lifetimes. The CHECK constraint defends a future direct-SQL writer (e.g.
# a backfill script) against drifting from the typed-record vocabulary.
_PROCESS_ROLES = ("pipeline", "monitor")


class ProcessLifetimeRow(Base):
    """Forward-only per-process-start runtime provenance row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Process
    lifetimes. The 14 columns are immutable once written.
    """

    __tablename__ = "process_lifetimes"

    process_lifetime_id: Mapped[str] = mapped_column(Text, primary_key=True)
    process_role: Mapped[str] = mapped_column(Text, nullable=False)
    process_start_at: Mapped[str] = mapped_column(Text, nullable=False)
    process_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    git_sha: Mapped[str] = mapped_column(Text, nullable=False)
    git_branch: Mapped[str] = mapped_column(Text, nullable=False)
    git_dirty: Mapped[int] = mapped_column(Integer, nullable=False)
    python_version: Mapped[str] = mapped_column(Text, nullable=False)
    pip_freeze_hash: Mapped[str] = mapped_column(Text, nullable=False)
    pip_freeze_snapshot_path: Mapped[str] = mapped_column(Text, nullable=False)
    anthropic_sdk_version: Mapped[str] = mapped_column(Text, nullable=False)
    claude_agent_sdk_version: Mapped[str] = mapped_column(Text, nullable=False)
    os_release: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"process_role IN ({', '.join(repr(r) for r in _PROCESS_ROLES)})",
            name="ck_process_lifetimes_process_role",
        ),
        CheckConstraint(
            "git_dirty IN (0, 1)",
            name="ck_process_lifetimes_git_dirty",
        ),
    )
