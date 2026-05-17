"""Tests for the ``scheduler/progress.py`` Protocol + NoOp default.

Verifies the public contract called out by story ALP-495:

* ``NoOpProgressEmitter`` satisfies the ``ProgressEmitter`` Protocol
  (``isinstance`` check — the Protocol carries ``@runtime_checkable``).
* Every Protocol method is callable with the field shapes documented in
  the design doc, including the kwargs-only ``agent_request`` /
  ``agent_response`` signatures and the 5-field ``agent_response`` set
  fixed by parent issue ALP-493 § Pre-resolved (B):
  ``duration_s``, ``input_tokens``, ``output_tokens``, ``tool_calls``,
  ``stop_reason``.
"""

from __future__ import annotations

from typing import Any

from alphamind.scheduler.progress import NoOpProgressEmitter, ProgressEmitter


def test_noop_emitter_satisfies_protocol() -> None:
    """``NoOpProgressEmitter`` is a structural instance of the Protocol.

    Verified by ``isinstance`` against ``ProgressEmitter`` — requires
    ``@runtime_checkable`` on the Protocol.
    """
    emitter = NoOpProgressEmitter()
    assert isinstance(emitter, ProgressEmitter)


def test_noop_phase_start_accepts_phase_without_raising() -> None:
    """``phase_start(phase: str)`` is callable and returns nothing."""
    emitter = NoOpProgressEmitter()
    emitter.phase_start("phase1")


def test_noop_phase_done_accepts_phase_and_arbitrary_fields() -> None:
    """``phase_done(phase, **fields)`` swallows arbitrary keyword fields."""
    emitter = NoOpProgressEmitter()
    emitter.phase_done("phase1", elapsed_s=1.23, status="ok")


def test_noop_agent_request_accepts_kwargs_only_fields() -> None:
    """``agent_request(*, phase, agent, model)`` is invoked kwargs-only."""
    emitter = NoOpProgressEmitter()
    emitter.agent_request(
        phase="synthesizer",
        agent="synthesizer",
        model="claude-opus-4-7",
    )


def test_noop_agent_response_accepts_all_five_response_fields() -> None:
    """``agent_response`` carries the 5-field response set per ALP-493 (B).

    The Protocol fixes the field shape; the no-op implementation
    accepts via ``**fields: Any``.
    """
    emitter = NoOpProgressEmitter()
    emitter.agent_response(
        phase="synthesizer",
        agent="synthesizer",
        model="claude-opus-4-7",
        duration_s=42.7,
        input_tokens=12_345,
        output_tokens=6_789,
        tool_calls=3,
        stop_reason="end_turn",
    )


def test_noop_agent_response_accepts_none_stop_reason() -> None:
    """``stop_reason`` is ``str | None``; ``None`` must round-trip cleanly."""
    emitter = NoOpProgressEmitter()
    emitter.agent_response(
        phase="analyst",
        agent="analyst",
        model="claude-opus-4-7",
        duration_s=0.0,
        input_tokens=0,
        output_tokens=0,
        tool_calls=0,
        stop_reason=None,
    )


def test_protocol_declared_methods_are_present() -> None:
    """The Protocol exposes exactly the four declared method names.

    Guards against silent renames; the orchestrator (story 02a) threads
    these names through every emit site.
    """
    declared = {"phase_start", "phase_done", "agent_request", "agent_response"}
    available = {
        name
        for name in dir(ProgressEmitter)
        if not name.startswith("_") and callable(getattr(ProgressEmitter, name))
    }
    # Every declared method is present; allow extras the typing machinery
    # may attach (e.g. ``mro``-style hooks should not break this test).
    assert declared.issubset(available)


def test_recording_emitter_substitute_also_satisfies_protocol() -> None:
    """A minimal in-test substitute structurally satisfies the Protocol.

    Demonstrates the testing seam called out by the design doc § 5 —
    fakes over mocks. Story 02a will use this shape to assert on the
    event stream.
    """

    class _RecordingEmitter:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def phase_start(self, phase: str) -> None:
            self.events.append(("phase_start", {"phase": phase}))

        def phase_done(self, phase: str, **fields: Any) -> None:
            self.events.append(("phase_done", {"phase": phase, **fields}))

        def agent_request(self, **fields: Any) -> None:
            self.events.append(("agent_request", fields))

        def agent_response(self, **fields: Any) -> None:
            self.events.append(("agent_response", fields))

    recording = _RecordingEmitter()
    assert isinstance(recording, ProgressEmitter)
