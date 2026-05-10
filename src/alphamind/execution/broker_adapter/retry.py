"""Bounded-retry helper for Alpaca submissions (story 01 / ALP-378).

The submission retry window contract lives in
``state-persistence.md § Phase 2 write path``: each Alpaca submission attempt
runs inside a brief exponential-backoff loop bounded by
``ExecutionConfig.submission_retry_window_seconds``. On exhaustion the command
transaction rolls back and a ``command_abandoned`` activity-log entry surfaces
to the originating agent.

This helper handles only the retry mechanics; the rollback + activity-log
emission are owned by the Phase 2 write path. The helper distinguishes
**transient** failures (network errors, timeouts, 5xx) — which it retries —
from **permanent** rejections (4xx with rejection reasons per
``broker-adapter.md``) — which it re-raises so the caller can translate them
into a synchronous OMS rejection.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from alphamind.execution.broker_adapter.errors import is_transient


@dataclass(frozen=True)
class Submitted[T]:
    """A submission attempt that produced an Alpaca acknowledgment.

    ``payload`` is the callable's return value — typically an
    ``alpaca.trading.models.Order`` once order-translation stories ship.
    ``attempt_count`` is 1 for first-try success.
    """

    payload: T
    attempt_count: int


@dataclass(frozen=True)
class GatewaySubmissionFailed:
    """A submission attempt that exhausted the retry window without success.

    Phase 2 of the OMS write path translates this into a ``command_abandoned``
    activity-log entry per ``state-persistence.md``. ``last_error_class`` is a
    short identifier (the final exception's ``type(...).__name__``) suitable
    for log queries; the full exception chain is not preserved here — the
    caller logs at the boundary.
    """

    reason: str
    attempt_count: int
    last_error_class: str


type SubmissionOutcome[T] = Submitted[T] | GatewaySubmissionFailed


# Backoff schedule per the story: 0.5s, 1s, 2s, 4s, capped at half the
# remaining window. Tuned for Alpaca's brief transient envelope (single-digit
# seconds typical) inside the ~30s default window.
_BASE_BACKOFFS_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)


async def submit_with_retry[T](
    submit: Callable[[], Awaitable[T]],
    *,
    window_seconds: float,
    transient_classifier: Callable[[Exception], bool] | None = None,
) -> SubmissionOutcome[T]:
    """Run ``submit()`` with bounded exponential-backoff retry.

    * Total wall-clock budget: ``window_seconds`` (typically
      ``ExecutionConfig.submission_retry_window_seconds``).
    * Backoff schedule: 0.5s, 1s, 2s, 4s, then 4s indefinitely, each capped at
      half the remaining window so a sleep cannot consume the entire budget.
    * Permanent rejections (per ``transient_classifier`` returning ``False``)
      re-raise so the caller can translate them to a synchronous OMS rejection.
    * The default ``transient_classifier`` is
      :func:`alphamind.execution.broker_adapter.errors.is_transient`.

    ``BaseException`` subclasses (``asyncio.CancelledError``, ``KeyboardInterrupt``,
    ``SystemExit``) propagate unchanged — they signal external interruption and
    must never be retried as if they were transient broker failures.
    """
    classifier = transient_classifier or is_transient
    deadline = time.monotonic() + window_seconds
    attempt = 0
    last_exc: Exception | None = None

    while True:
        attempt += 1
        try:
            payload = await submit()
        except Exception as exc:
            if not classifier(exc):
                raise
            last_exc = exc
        else:
            return Submitted(payload=payload, attempt_count=attempt)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(_next_backoff(attempt, remaining))
        if time.monotonic() >= deadline:
            break

    # Window exhausted on a transient error; ``last_exc`` is set because
    # we only reach here after at least one classified-transient failure.
    assert last_exc is not None
    return GatewaySubmissionFailed(
        reason=f"submission retry window of {window_seconds:g}s exhausted",
        attempt_count=attempt,
        last_error_class=type(last_exc).__name__,
    )


def _next_backoff(attempt: int, remaining: float) -> float:
    """Compute the next sleep duration, capped at half the remaining window.

    ``attempt`` is the 1-based count of attempts already made; the first sleep
    (after attempt 1 fails) uses ``_BASE_BACKOFFS_SECONDS[0]``. Caller
    guarantees ``remaining > 0``.
    """
    idx = min(attempt - 1, len(_BASE_BACKOFFS_SECONDS) - 1)
    return min(_BASE_BACKOFFS_SECONDS[idx], remaining / 2)
