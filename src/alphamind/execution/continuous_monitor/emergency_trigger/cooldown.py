"""In-memory cooldown tracker for the emergency-invocation trigger (story 04b).

A pure tracker: deterministic given ``(now, trigger_type)`` and the prior
record sequence. Non-margin triggers are suppressed for
``cooldown_minutes`` after a prior emit; ``margin_call`` triggers always
pass (the broker's deadline is non-negotiable per
``docs/design/06-risk-guardrails/breach-behavior.md`` § Emergency invocation
trigger).

The tracker is reset across monitor restarts — a restart implies the prior
emergency context is gone, so the next trigger fires immediately. Pipeline
scheduler ALP-447's emergency receiver re-checks the cooldown against the
authoritative ``invocations`` table, so a re-fired request inside the
window is harmless: the receiver coalesces it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

_MARGIN_CALL_TRIGGER = "margin_call"


class CooldownTracker:
    """Tracks the last non-margin emergency emit's timestamp.

    The tracker exposes two operations:

    * :meth:`may_fire` — would this trigger be allowed to emit right now?
    * :meth:`record_fire` — note that an emit has occurred.

    The third method, :meth:`remaining_seconds`, is the writer-side
    observation surfaced into ``EmergencyInvocationRequestedDetail`` for
    the receiver's bookkeeping.

    Margin-call triggers always return ``True`` from :meth:`may_fire` and
    ``0`` from :meth:`remaining_seconds` regardless of the prior emit; their
    :meth:`record_fire` calls still advance the window so subsequent
    non-margin triggers see the new anchor.
    """

    def __init__(self, *, cooldown_minutes: int) -> None:
        if cooldown_minutes < 1:
            msg = f"cooldown_minutes must be >= 1; got {cooldown_minutes}"
            raise ValueError(msg)
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._last_fire_at: datetime | None = None

    def may_fire(self, *, now: datetime, trigger_type: str) -> bool:
        """Return ``True`` when this trigger is allowed to emit at *now*.

        ``margin_call`` always passes; other triggers pass only when no
        prior emit exists or the elapsed time since the prior emit is at
        least ``cooldown_minutes``.
        """
        if trigger_type == _MARGIN_CALL_TRIGGER:
            return True
        if self._last_fire_at is None:
            return True
        return (now - self._last_fire_at) >= self._cooldown

    def record_fire(self, *, now: datetime, trigger_type: str) -> None:
        """Note that an emit occurred at *now*.

        ``trigger_type`` is accepted for symmetry with :meth:`may_fire` but
        does not affect anchor placement — every emit advances the cooldown
        window so the next emit (margin or non-margin) sees a fresh anchor.
        """
        del trigger_type  # kept on signature for symmetry; future audit may key on it.
        self._last_fire_at = now

    def remaining_seconds(self, *, now: datetime, trigger_type: str) -> int:
        """Return integer seconds until the cooldown lifts, ``0`` when satisfied.

        ``margin_call`` always returns ``0`` (cooldown bypassed). Other
        trigger types return the positive remaining seconds when inside the
        window, or ``0`` when no prior emit exists or the window has
        elapsed.
        """
        if trigger_type == _MARGIN_CALL_TRIGGER:
            return 0
        if self._last_fire_at is None:
            return 0
        elapsed = now - self._last_fire_at
        if elapsed >= self._cooldown:
            return 0
        return int((self._cooldown - elapsed).total_seconds())


__all__ = ["CooldownTracker"]
