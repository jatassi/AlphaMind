"""Monotonic per-session trigger-id generator (ALP-438).

The continuous monitor's cascade dispatcher requests a fresh trigger ID from
this generator per immediate-action breach event; the same trigger ID is
reused as the *initial* trigger ID across the envelopes of one cascade chain
(cascade orchestrators bump the trigger ID internally for follow-up envelopes
so the dispatcher only needs to seed each chain).

Per-session monotonic invariant: ids are gapless, strictly increasing within
one ``MonitorSession``. A monitor restart starts a fresh session id, which
makes envelope ids ``MON.{session}.{trigger}`` globally distinct across
restarts even though the trigger counter restarts at 1.
"""

from __future__ import annotations


class TriggerIdGenerator:
    """Monotonically-increasing trigger ID generator scoped to a MonitorSession.

    In-memory only — a monitor restart starts at ``start`` (default ``1``), and
    the fresh session id keeps the resulting envelope ids globally distinct.
    """

    __slots__ = ("_next", "_session_id")

    def __init__(self, *, session_id: str, start: int = 1) -> None:
        if not session_id:
            msg = "session_id must be a non-empty string"
            raise ValueError(msg)
        if start < 1:
            msg = f"start must be >= 1; got {start}"
            raise ValueError(msg)
        self._session_id = session_id
        self._next = start

    @property
    def session_id(self) -> str:
        """The session this generator is scoped to."""
        return self._session_id

    def next(self) -> int:
        """Return the next trigger id and advance the internal counter."""
        value = self._next
        self._next += 1
        return value
