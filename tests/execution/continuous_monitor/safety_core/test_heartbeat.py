"""Tests for the safety core's non-DB file heartbeat (ALP-857).

The heartbeat is the safety core's liveness signal to its out-of-process
watchdog — written to a **file**, never the shared DB (ADR-0004: the safety
core writes nothing to the DB). ``FileHeartbeatSink.beat`` writes a wall-clock
timestamp; ``FileHeartbeatProbe.age`` reads it back so the watchdog can detect
staleness without sharing the core's loop or its DB session.
"""

from __future__ import annotations

from pathlib import Path

from alphamind.execution.continuous_monitor.safety_core.heartbeat import (
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
