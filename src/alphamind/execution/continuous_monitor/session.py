"""``MonitorSession`` — frozen handle for one monitor-process lifetime (story 01).

A ``MonitorSession`` is threaded through every task the supervisor registers:
the fill-stream consumer, the underlying-price stream, the greeks-refresh
loop, the breach-evaluation loop, the engine-envelope cascade dispatcher, the
emergency-invocation trigger, and the options bracket-stop firing path. The
session carries the per-process identity used in activity-log provenance and
log lines.

Session-id shape ``mon-YYYYMMDDTHHMMSSZ-XXXXXXXX`` mirrors the collector's
``coll-...`` and pipeline scheduler's ``plt-...`` conventions; the 8-hex random
suffix is taken from :func:`secrets.token_hex` so two sessions started within
the same second cannot collide.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, get_args

MonitorMode = Literal["paper", "live"]


@dataclass(frozen=True, slots=True)
class MonitorSession:
    """Per-process handle threaded through the supervisor and its tasks."""

    session_id: str
    started_at: datetime
    mode: MonitorMode

    def __post_init__(self) -> None:
        if self.mode not in get_args(MonitorMode):
            msg = f"mode must be one of {get_args(MonitorMode)}, got {self.mode!r}"
            raise ValueError(msg)


def new_session(*, mode: MonitorMode) -> MonitorSession:
    """Build a fresh ``MonitorSession`` stamped with the current UTC time.

    The session_id format ``mon-YYYYMMDDTHHMMSSZ-XXXXXXXX`` packs a sortable
    UTC timestamp with an 8-hex-character random suffix so concurrent starts
    produce distinct IDs and ``grep`` over ``monitor.log`` can find every
    line emitted by one process.
    """
    started_at = datetime.now(UTC)
    suffix = secrets.token_hex(4)
    session_id = f"mon-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
    return MonitorSession(session_id=session_id, started_at=started_at, mode=mode)
