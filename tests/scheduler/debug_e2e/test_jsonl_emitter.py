"""Tests for ``scheduler/debug_e2e/jsonl_emitter.py`` (story ALP-499 / 02c).

Verifies the public contract called out by the user story:

* ``JsonlProgressEmitter(*, path)`` constructs cleanly.
* ``JsonlProgressEmitter`` is a structural instance of ``ProgressEmitter``
  (``isinstance`` check — the Protocol carries ``@runtime_checkable``).
* Each emitted event is exactly one JSON line containing an ``event``
  discriminator, an ISO-8601 UTC ``timestamp``, and the caller-supplied
  fields.
* Parent directories are created on first write; subsequent writes append.
* Writes are synchronous (``flush()`` + ``os.fsync()`` per write) — a
  second handle opened after each emit and read inside the writer's
  surrounding scope sees the line on disk before the next call.
* Emitting one of each event type and re-reading produces the JSONL
  records in the order they were emitted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphamind.scheduler.debug_e2e.jsonl_emitter import JsonlProgressEmitter
from alphamind.scheduler.progress import ProgressEmitter


def test_emitter_satisfies_progress_emitter_protocol(tmp_path: Path) -> None:
    """``JsonlProgressEmitter`` is a structural instance of the Protocol.

    Acceptance criterion: verified via ``isinstance`` against the
    ``@runtime_checkable`` Protocol from story 01b.
    """
    emitter = JsonlProgressEmitter(path=tmp_path / "progress.jsonl")
    assert isinstance(emitter, ProgressEmitter)


def test_phase_start_writes_one_jsonl_record_with_event_and_timestamp(
    tmp_path: Path,
) -> None:
    """A single ``phase_start`` emits exactly one JSON line.

    The line must contain ``event="phase_start"``, the caller-supplied
    ``phase``, and an ISO-8601 UTC ``timestamp``.
    """
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_start("phase1")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "phase_start"
    assert record["phase"] == "phase1"
    # ISO-8601 UTC timestamp — round-trips via ``datetime.fromisoformat``
    # and preserves UTC.
    parsed = datetime.fromisoformat(record["timestamp"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)


def test_phase_done_includes_caller_supplied_fields(tmp_path: Path) -> None:
    """``phase_done(phase, **fields)`` carries every supplied field."""
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_done("phase1", elapsed_s=1.5, status="ok")

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["event"] == "phase_done"
    assert record["phase"] == "phase1"
    assert record["elapsed_s"] == 1.5
    assert record["status"] == "ok"


def test_agent_request_emits_event_with_kwargs(tmp_path: Path) -> None:
    """``agent_request(**fields)`` carries the Protocol's kwargs."""
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.agent_request(
        phase="synthesizer",
        agent="synthesizer",
        model="claude-opus-4-7",
    )

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["event"] == "agent_request"
    assert record["phase"] == "synthesizer"
    assert record["agent"] == "synthesizer"
    assert record["model"] == "claude-opus-4-7"


def test_agent_response_emits_full_field_set(tmp_path: Path) -> None:
    """``agent_response`` carries the 5-field response set per ALP-493 (B)."""
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.agent_response(
        phase="analyst",
        agent="analyst",
        model="claude-opus-4-7",
        duration_s=42.7,
        input_tokens=12_345,
        output_tokens=6_789,
        tool_calls=3,
        stop_reason="end_turn",
    )

    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["event"] == "agent_response"
    assert record["phase"] == "analyst"
    assert record["agent"] == "analyst"
    assert record["model"] == "claude-opus-4-7"
    assert record["duration_s"] == 42.7
    assert record["input_tokens"] == 12_345
    assert record["output_tokens"] == 6_789
    assert record["tool_calls"] == 3
    assert record["stop_reason"] == "end_turn"


def test_parent_directory_created_on_first_write(tmp_path: Path) -> None:
    """Acceptance criterion: parent directory created on first write."""
    path = tmp_path / "deeply" / "nested" / "dir" / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)
    assert not path.parent.exists()

    emitter.phase_start("phase1")

    assert path.parent.is_dir()
    assert path.is_file()


def test_subsequent_writes_append_rather_than_truncate(tmp_path: Path) -> None:
    """Acceptance criterion: subsequent writes append."""
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_start("phase1")
    emitter.phase_start("phase2")
    emitter.phase_start("phase3")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    phases = [json.loads(line)["phase"] for line in lines]
    assert phases == ["phase1", "phase2", "phase3"]


def test_each_write_is_synchronously_flushed_and_fsynced(tmp_path: Path) -> None:
    """Acceptance criterion: each write is synchronously visible.

    Open a second handle after each emit and read the bytes back —
    asserting the event is on disk before the next call. The writer's
    surrounding scope has already exited per-emit (no buffering across
    calls).
    """
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_start("phase1")
    with path.open("r", encoding="utf-8") as second_handle:
        first_seen = second_handle.read()
    assert json.loads(first_seen.strip())["phase"] == "phase1"

    emitter.phase_done("phase1", elapsed_s=1.0)
    with path.open("r", encoding="utf-8") as second_handle:
        second_seen = second_handle.read()
    second_lines = second_seen.splitlines()
    assert len(second_lines) == 2
    assert json.loads(second_lines[1])["event"] == "phase_done"


def test_emit_one_of_each_event_type_roundtrips_in_order(tmp_path: Path) -> None:
    """Acceptance criterion: re-read JSONL matches emitted sequence.

    Emits one event of each declared Protocol method in order and
    asserts the on-disk file's parsed JSONL records match.
    """
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_start("phase1")
    emitter.phase_done("phase1", elapsed_s=0.5)
    emitter.agent_request(
        phase="synthesizer",
        agent="synthesizer",
        model="claude-opus-4-7",
    )
    emitter.agent_response(
        phase="synthesizer",
        agent="synthesizer",
        model="claude-opus-4-7",
        duration_s=3.0,
        input_tokens=100,
        output_tokens=50,
        tool_calls=0,
        stop_reason="end_turn",
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    records = [json.loads(line) for line in lines]
    assert [r["event"] for r in records] == [
        "phase_start",
        "phase_done",
        "agent_request",
        "agent_response",
    ]
    # Each record carries an ISO-8601 timestamp.
    for record in records:
        parsed = datetime.fromisoformat(record["timestamp"])
        assert parsed.tzinfo is not None


def test_non_json_serializable_field_raises_type_error(tmp_path: Path) -> None:
    """Non-JSON-serializable values raise ``TypeError`` — no silent drop.

    Story scope: "Non-JSON-serializable values raise TypeError at write
    time — no silent drop."
    """
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    with pytest.raises(TypeError):
        emitter.phase_done("phase1", payload=object())


def test_raw_bytes_contain_only_lf_line_endings(tmp_path: Path) -> None:
    """Raw on-disk bytes use ``\\n`` exclusively — no Windows CRLF translation.

    Default text-mode file opening on Windows translates ``\\n`` writes
    into ``\\r\\n`` bytes. The downstream consumers (``json.loads``)
    tolerate ``\\r\\n``, but the JSONL spec mandates ``\\n``-only and
    byte-identical output across platforms matters for archive
    digests, diff hygiene, and any future binary-aware parser.
    """
    path = tmp_path / "progress.jsonl"
    emitter = JsonlProgressEmitter(path=path)

    emitter.phase_start("phase1")
    emitter.phase_done("phase1", elapsed_s=1.0)
    emitter.agent_request(phase="x", agent="x", model="m")

    raw_bytes = path.read_bytes()
    assert b"\r\n" not in raw_bytes, f"raw bytes contained CRLF — got {raw_bytes!r}"
    assert raw_bytes.count(b"\n") == 3
