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

The write is atomic (write-temp + ``os.replace``) so a watchdog reading
concurrently never observes a half-written or empty file.
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
    """Writes the supervised process's latest beat timestamp to a file (atomically)."""

    def __init__(self, *, path: Path, clock: Callable[[], float] = time.time) -> None:
        # ``clock`` is the injected wall-clock port (default ``time.time``); tests
        # substitute a deterministic float source. It MUST be wall-clock, never
        # ``time.monotonic`` — a monotonic clock is per-process and meaningless to
        # the separate watchdog process reading this beat across the boundary.
        self._path = path
        self._now = clock

    def beat(self) -> None:
        """Record the current timestamp as the latest heartbeat.

        Atomic: writes to a sibling ``*.tmp`` then ``os.replace`` onto the real
        path, so a concurrent :class:`FileHeartbeatProbe` read never sees a
        torn or empty file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(repr(self._now()), encoding="utf-8")
        tmp.replace(self._path)


class FileHeartbeatProbe:
    """Reads the supervised process's heartbeat file to compute its age (read-only)."""

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def age(self, *, now: float) -> float | None:
        """Return seconds since the last beat, or ``None`` if never beaten.

        ``None`` means the heartbeat file does not exist yet (the supervised
        process has not started / not yet written one) — the watchdog treats
        that as startup grace, not a wedge. A malformed file (truncated
        mid-write despite the atomic replace, or hand-corrupted) also reads as
        ``None`` so a single bad read never trips a false restart.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            last_beat = float(raw)
        except ValueError:
            return None
        return now - last_beat
