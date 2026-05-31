"""``PipelineSession`` — frozen handle for one pipeline-process lifetime.

A ``PipelineSession`` is passed to every task the supervisor registers; it
carries the FK to ``process_lifetimes`` (so per-invocation rows can stamp
``process_lifetime_id``), the process start timestamp, and the active
trading mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

PipelineMode = Literal["paper", "live"]


class PipelineSession(BaseModel):
    """Per-process handle threaded through the supervisor and its tasks."""

    model_config = ConfigDict(frozen=True, strict=True)

    process_lifetime_id: str
    started_at: datetime
    mode: PipelineMode


def new_session(
    *,
    process_lifetime_id: str,
    mode: PipelineMode,
) -> PipelineSession:
    """Build a fresh ``PipelineSession`` stamped with the current UTC time."""
    return PipelineSession(
        process_lifetime_id=process_lifetime_id,
        started_at=datetime.now(UTC),
        mode=mode,
    )
