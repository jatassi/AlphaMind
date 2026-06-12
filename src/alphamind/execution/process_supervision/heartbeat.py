"""Non-DB file heartbeat between a supervised process and its watchdog.

The supervised process's liveness signal is a **file**, never a row in the
shared DB: the heartbeat must stay readable while the process (and any DB
connection it pins) is wedged, and ADR-0004 additionally requires the safety
core to write nothing to the DB. The process beats every tick
(:class:`FileHeartbeatSink`); the out-of-process watchdog reads the file
(:class:`FileHeartbeatProbe`) to decide whether the process has wedged. A file
sink (not a DB row, not a pipe) is the simplest seam that crosses the process
boundary and survives the writer's death.

Cross-process timing: the timestamp is a **wall-clock epoch** (``time.time()``),
not ``time.monotonic()`` — a monotonic clock is per-process and meaningless to a
separate watchdog process. Both processes read the same wall clock, so age is
comparable across the boundary.

The beat is a single in-place write to the heartbeat path. Renaming onto the
path is not an option: Windows denies it while the watchdog probe holds the
file open for its concurrent read (ALP-951). The liveness contract is
per-write freshness with read-side tolerance — a probe read landing inside
the writer's truncate-write window sees an empty file and reports ``None``,
which the watchdog treats as one missed probe, never a restart trigger.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class HeartbeatSink(Protocol):
    """Structural Protocol for the per-iteration liveness beat a process writes.

    The seam a supervised loop depends on (``MonitorSupervisor``'s injected
    heartbeat, the safety core's shell loop): one ``beat()`` per iteration.
    Production binds :class:`FileHeartbeatSink`; tests bind a fake that records
    calls.
    """

    def beat(self) -> None: ...


class FileHeartbeatSink:
    """Writes the supervised process's latest beat timestamp to a file (in place)."""

    def __init__(self, *, path: Path, clock: Callable[[], float] = time.time) -> None:
        # ``clock`` is the injected wall-clock port (default ``time.time``); tests
        # substitute a deterministic float source. It MUST be wall-clock, never
        # ``time.monotonic`` — a monotonic clock is per-process and meaningless to
        # the separate watchdog process reading this beat across the boundary.
        self._path = path
        self._now = clock
        # Created once here, not per beat — ``beat()`` runs on the supervised
        # loop's per-iteration path.
        path.parent.mkdir(parents=True, exist_ok=True)

    def beat(self) -> None:
        """Record the current timestamp as the latest heartbeat.

        A single in-place write to the heartbeat path — never a rename onto it,
        which Windows denies (``PermissionError``) while the watchdog's
        :class:`FileHeartbeatProbe` holds the file open for its concurrent read
        (ALP-951). A probe read landing inside the truncate-write window sees an
        empty file, which :meth:`FileHeartbeatProbe.age` reports as ``None``. The
        timestamp itself lands in one small buffered ``write``, which concurrent
        reads observe all-or-nothing on both platforms — empty is the only
        intermediate state a probe can see, never a torn numeric prefix (which
        could parse as an ancient epoch and trip a false restart).
        """
        self._path.write_text(repr(self._now()), encoding="utf-8")


class FileHeartbeatProbe:
    """Reads the supervised process's heartbeat file to compute its age (read-only)."""

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def age(self, *, now: float) -> float | None:
        """Return seconds since the last beat, or ``None`` if never beaten.

        ``None`` means the heartbeat file does not exist yet (the supervised
        process has not started / not yet written one) — the watchdog treats
        that as startup grace, not a wedge. A transiently unreadable file
        (e.g. an antivirus scan holding it), an empty one (a read landing
        inside the writer's in-place truncate-write window), or a malformed
        one (hand-corrupted) also reads as ``None`` so a single bad read never
        trips a false restart — and never crashes the watchdog, which must
        outlive any single probe failure to keep supervising.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            last_beat = float(raw)
        except ValueError:
            return None
        return now - last_beat
