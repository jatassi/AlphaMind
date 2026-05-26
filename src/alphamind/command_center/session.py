"""``ProcessSession`` — frozen handle for one command-center-process lifetime.

Mirrors :class:`alphamind.scheduler.session.PipelineSession` shape: a
small frozen Pydantic model the supervisor + its tasks consume to know
the process-lifetime FK + the start timestamp. The ``mode`` field is
intentionally absent — the command center has no paper/live mode
distinction; it serves whichever DB it's pointed at.

The process_lifetime_id FK target is recorded via
:func:`alphamind.state.process_lifetime.record_process_lifetime` at
daemon startup; the value flows into the supervisor + the
:func:`operator_invocation` helper so every operator-action row
participates in the same process-lifetime grouping the pipeline /
monitor processes use.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class ProcessSession(BaseModel):
    """Per-process handle threaded through the command-center supervisor.

    Carries the FK to ``process_lifetimes`` so per-operator-action
    invocation rows can stamp ``process_lifetime_id``, plus the process
    start timestamp for log-line provenance.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    process_lifetime_id: str
    started_at: datetime


def new_session(*, process_lifetime_id: str) -> ProcessSession:
    """Build a fresh :class:`ProcessSession` stamped with the current UTC time."""
    return ProcessSession(
        process_lifetime_id=process_lifetime_id,
        started_at=datetime.now(UTC),
    )
