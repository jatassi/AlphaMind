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

Also exposes :class:`RecordingProgressEmitter` as a reusable test fake
(per story ALP-497 § AC) so the integration test in
``tests/scheduler/test_orchestrator_progress.py`` and any future
emitter-aware test can assert on the recorded event stream without
re-deriving the substitute shape.
"""

from __future__ import annotations

from typing import Any

from alphamind.scheduler.progress import NoOpProgressEmitter, ProgressEmitter


class RecordingProgressEmitter:
    """Test fake — records every progress event as a ``(kind, fields)`` tuple.

    Structurally satisfies :class:`ProgressEmitter` (the Protocol is
    ``@runtime_checkable``); tests can pass an instance wherever
    ``ProgressEmitter`` is expected and assert on ``self.events`` to
    verify the emit stream. Story ALP-497 § AC pins this shape so the
    orchestrator-progress integration test (and follow-on tests) can
    import a single canonical substitute.

    ``phase_start`` events carry only the phase name; ``phase_done``
    events carry the phase name plus any caller-supplied ``**fields``.
    ``agent_request`` / ``agent_response`` events carry every kwarg the
    real call site supplies — the recording does not enforce the
    Protocol's named-only contract so tests can assert on whatever
    field set the real emit site uses.
    """

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


def test_recording_emitter_satisfies_protocol() -> None:
    """The module-level :class:`RecordingProgressEmitter` satisfies the Protocol.

    Demonstrates the testing seam called out by the design doc § 5 —
    fakes over mocks. The integration test in
    ``test_orchestrator_progress.py`` uses this same class to assert on
    the orchestrator's emitted event stream.
    """
    recording = RecordingProgressEmitter()
    assert isinstance(recording, ProgressEmitter)


def test_recording_emitter_records_each_event_kind() -> None:
    """All four event kinds land in ``events`` with the expected payload shape."""
    emitter = RecordingProgressEmitter()
    emitter.phase_start("phase1")
    emitter.phase_done("phase1", fills_processed=3)
    emitter.agent_request(phase="analyst", agent="analyst", model="claude-opus-4-7")
    emitter.agent_response(
        phase="analyst",
        agent="analyst",
        model="claude-opus-4-7",
        duration_s=4.2,
        input_tokens=100,
        output_tokens=50,
        tool_calls=2,
        stop_reason="end_turn",
    )
    kinds = [evt[0] for evt in emitter.events]
    assert kinds == ["phase_start", "phase_done", "agent_request", "agent_response"]
    assert emitter.events[0][1] == {"phase": "phase1"}
    assert emitter.events[1][1] == {"phase": "phase1", "fills_processed": 3}
    assert emitter.events[2][1]["agent"] == "analyst"
    assert emitter.events[3][1]["stop_reason"] == "end_turn"
