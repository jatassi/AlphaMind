"""Tests for the non-DB file heartbeat (ALP-857 / ALP-941).

The heartbeat is a supervised process's liveness signal to its out-of-process
watchdog — written to a **file**, never the shared DB (it must stay readable
while the process is wedged; ADR-0004 additionally forbids safety-core DB
writes). ``FileHeartbeatSink.beat`` writes a wall-clock timestamp;
``FileHeartbeatProbe.age`` reads it back so the watchdog can detect staleness
without sharing the supervised process's loop or its DB session.
"""

from __future__ import annotations

from pathlib import Path

from alphamind.execution.process_supervision.heartbeat import (
    FileHeartbeatProbe,
    FileHeartbeatSink,
)


def test_beat_then_probe_round_trips_age(tmp_path: Path) -> None:
    """A beat at t0, probed at t0+5, reports ~5s of age."""
    clock = {"now": 100.0}
    path = tmp_path / "safety_core.heartbeat"
    sink = FileHeartbeatSink(path=path, clock=lambda: clock["now"])
    probe = FileHeartbeatProbe(path=path)

    sink.beat()
    clock["now"] = 105.0

    assert probe.age(now=105.0) == 5.0


def test_probe_missing_heartbeat_is_none(tmp_path: Path) -> None:
    """No heartbeat file yet (core never started) → age is None, not a crash."""
    probe = FileHeartbeatProbe(path=tmp_path / "absent.heartbeat")

    assert probe.age(now=100.0) is None


def test_probe_unreadable_heartbeat_is_none_not_a_crash(tmp_path: Path) -> None:
    """A transient OS-level read failure reads as None — the watchdog survives.

    Windows can briefly deny the read while the writer's os.replace holds the
    file; the probe must treat any OSError as an unreadable beat and re-probe
    next tick rather than crash the watchdog process. A directory at the
    heartbeat path raises an OSError subclass on both platforms, standing in
    for that transient denial.
    """
    probe = FileHeartbeatProbe(path=tmp_path)  # a directory: read_text -> OSError

    assert probe.age(now=100.0) is None


def test_beat_overwrites_previous(tmp_path: Path) -> None:
    """A later beat resets the timer — age is measured from the most recent beat."""
    clock = {"now": 100.0}
    path = tmp_path / "safety_core.heartbeat"
    sink = FileHeartbeatSink(path=path, clock=lambda: clock["now"])
    probe = FileHeartbeatProbe(path=path)

    sink.beat()
    clock["now"] = 200.0
    sink.beat()

    assert probe.age(now=205.0) == 5.0
