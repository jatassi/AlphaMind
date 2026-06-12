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

    The environment can briefly deny the read (an antivirus scan or SMB layer
    holding the file); the probe must treat any OSError as an unreadable beat
    and re-probe next tick rather than crash the watchdog process. A directory
    at the heartbeat path raises an OSError subclass on both platforms,
    standing in for that transient denial.
    """
    probe = FileHeartbeatProbe(path=tmp_path)  # a directory: read_text -> OSError

    assert probe.age(now=100.0) is None


def test_beat_lands_in_place_while_reader_holds_the_file_open(tmp_path: Path) -> None:
    """A beat must land in the file a concurrent probe holds open (ALP-951).

    The watchdog probe reads the heartbeat from a separate process; its open
    handle must never make ``beat()`` fail (Windows share modes deny rename
    over an open destination) and the fresh beat must be visible through that
    same handle (an in-place write, not a directory-entry swap).
    """
    clock = {"now": 100.0}
    path = tmp_path / "monitor.heartbeat"
    sink = FileHeartbeatSink(path=path, clock=lambda: clock["now"])

    sink.beat()
    clock["now"] = 123.0
    with path.open("r", encoding="utf-8") as held:
        sink.beat()  # must not raise while ``held`` keeps the file open
        held.seek(0)
        assert held.read() == repr(123.0)


def test_beat_writes_only_the_heartbeat_file(tmp_path: Path) -> None:
    """Beats leave exactly the heartbeat file — no ``.tmp`` sibling (ALP-951)."""
    clock = {"now": 100.0}
    path = tmp_path / "monitor.heartbeat"
    sink = FileHeartbeatSink(path=path, clock=lambda: clock["now"])

    sink.beat()
    clock["now"] = 200.0
    sink.beat()

    assert [p.name for p in tmp_path.iterdir()] == ["monitor.heartbeat"]
    assert path.read_text(encoding="utf-8") == repr(200.0)


def test_probe_empty_heartbeat_is_none(tmp_path: Path) -> None:
    """An empty file — a probe read inside the writer's truncate-write window — is None."""
    path = tmp_path / "monitor.heartbeat"
    path.write_text("", encoding="utf-8")
    probe = FileHeartbeatProbe(path=path)

    assert probe.age(now=100.0) is None
