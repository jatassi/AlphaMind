"""Transport-agnostic connected-but-silent stream detection (ALP-828 / ALP-819).

A long-lived websocket stream can flap at the transport layer (e.g.
``WinError 121``) and have the ``websockets`` library reconnect internally
*without raising or delivering frames* — the stream is "connected but silent".
Nothing surfaces the wedge, so a naive consumer parks forever on its next read
(the 11h ALP-768 hang).

The kernel here is the reusable heart of the ALP-819 fix, extracted from the
fill stream so the underlying-price stream (story 03) drives the same logic
rather than rebuilding it. It is deliberately transport-agnostic: it knows
nothing of ``TradeUpdate``, alpaca-py, quotes, or queues. The caller pumps one
:meth:`StreamActivityMonitor.on_slice` per poll tick and signals
:meth:`StreamActivityMonitor.record_activity` whenever real data arrives (a
dequeued fill frame; a handled quote). The monitor:

* beats the supervisor's stall watchdog on every slice (per-slice liveness);
* tracks the time since the last activity off an injected monotonic clock;
* raises :class:`StreamStalledError` when the market is open (``is_rth``) and
  the silence exceeds ``frame_timeout``, so the caller can force a
  budget-neutral reconnect;
* resets its activity clock off-hours, so the closed-market gap is not charged
  against the first RTH window (which would force a spurious reconnect at the
  open).

``frame_timeout`` (proactive in-task reconnect on quote/frame silence) is a
distinct knob from the supervisor's watchdog stall-timeout (task-liveness
backstop → ``os._exit`` → NSSM restart); they are not conflated here.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


class StreamStalledError(Exception):
    """A connected stream stopped delivering activity during RTH (ALP-819).

    The canonical cross-stream name (renamed from ``FillStreamStalledError``):
    both the fill stream and the underlying-price stream raise it to signal a
    connected-but-silent wedge. The consumer treats it as a budget-neutral
    signal to tear down and rebuild the stream.
    """


@dataclass(frozen=True, slots=True)
class _StalenessDecision:
    """The functional core's verdict for one silent poll slice.

    ``reset_clock`` asks the shell to treat *now* as the last-activity instant
    (the off-hours reset); ``stalled`` asks it to raise.
    """

    stalled: bool
    reset_clock: bool


def evaluate_staleness(
    *,
    elapsed_seconds: float,
    frame_timeout: float | None,
    is_rth: bool | None,
) -> _StalenessDecision:
    """Decide whether a silent slice is a stall (pure; functional core).

    No clock, no I/O — the imperative :class:`StreamActivityMonitor` reads the
    clock and applies the verdict. With ``frame_timeout`` / ``is_rth`` unset
    the staleness branch is inert (an ordinary drain). Off-hours, the clock is
    reset so a quiet closed market never trips and the gap is not charged
    against the first RTH window. During RTH, silence past ``frame_timeout``
    stalls.
    """
    if frame_timeout is None or is_rth is None:
        return _StalenessDecision(stalled=False, reset_clock=False)
    if not is_rth:
        return _StalenessDecision(stalled=False, reset_clock=True)
    return _StalenessDecision(stalled=elapsed_seconds > frame_timeout, reset_clock=False)


class StreamActivityMonitor:
    """Per-slice heartbeat + RTH staleness watch a stream consumer pumps.

    Transport-agnostic imperative shell around :func:`evaluate_staleness`. The
    caller owns the ``await`` (a queue drain, an alpaca-py ``_run_forever`` on a
    sibling task) and drives this monitor: :meth:`record_activity` on each real
    frame/quote, :meth:`on_slice` once per silent poll tick. :attr:`poll_interval`
    is the slice width the caller should use as its read timeout.

    All four staleness knobs are optional. With ``frame_timeout`` / ``is_rth``
    unset the monitor only beats — the staleness branch is inert.
    """

    def __init__(
        self,
        *,
        frame_timeout: float | None,
        is_rth: Callable[[], bool] | None,
        beat: Callable[[], None],
        poll_interval: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frame_timeout = frame_timeout
        self._is_rth = is_rth
        self._beat = beat
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._last_activity_at = monotonic()

    @property
    def poll_interval(self) -> float:
        """The slice width the caller uses as its per-read timeout."""
        return self._poll_interval

    def record_activity(self) -> None:
        """Signal that real data arrived this slice (a frame / a quote)."""
        self._last_activity_at = self._monotonic()

    def on_slice(self) -> None:
        """Process one silent poll slice: beat, then evaluate staleness.

        Beats the watchdog (per-slice liveness, fired on every slice), then
        consults the functional core. Resets the activity clock off-hours and
        raises :class:`StreamStalledError` when the market is open and the
        silence has exceeded ``frame_timeout``.
        """
        self._beat()
        now = self._monotonic()
        decision = evaluate_staleness(
            elapsed_seconds=now - self._last_activity_at,
            frame_timeout=self._frame_timeout,
            is_rth=self._is_rth() if self._is_rth is not None else None,
        )
        if decision.reset_clock:
            self._last_activity_at = now
        if decision.stalled:
            timeout = self._frame_timeout or 0.0
            msg = f"no stream activity for >{timeout:.0f}s during RTH"
            raise StreamStalledError(msg)
