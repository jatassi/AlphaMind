"""Tests for ``alphamind.scheduler.control.sse_progress_bridge`` (ALP-812).

``PipelineSSEProgressBridge`` is the dual sink the orchestrator wires per
invocation: every ``ProgressEmitter`` Protocol callback forwards to an inner
emitter AND, for the 9 schema agents, fires the matching SSE schema event.
These tests drive the bridge against a **real** ``SSEEventEmitter`` (read back
through a ``subscribe()``-d queue) plus an in-process recording
``ProgressEmitter`` as the ``inner`` collaborator — no internal collaborator is
patched. The bridge's ``datetime.now(UTC)`` for ``started_at`` is asserted for
timezone-awareness rather than frozen (the value is not behaviorally load-bearing).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from alphamind.scheduler.control import sse_progress_bridge as bridge_mod
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.control.sse_progress_bridge import (
    DEFAULT_LATENCY_BUDGET_SECONDS,
    PipelineSSEProgressBridge,
    make_latency_budget_lookup,
)

_INVOCATION_ID = "inv-20260507-1"


class _RecordingProgressEmitter:
    """In-process ``ProgressEmitter`` double recording every forwarded call."""

    def __init__(self) -> None:
        self.phase_starts: list[str] = []
        self.phase_dones: list[tuple[str, dict[str, Any]]] = []
        self.agent_requests: list[dict[str, Any]] = []
        self.agent_retryings: list[dict[str, Any]] = []
        self.agent_responses: list[dict[str, Any]] = []

    def phase_start(self, phase: str) -> None:
        self.phase_starts.append(phase)

    def phase_done(self, phase: str, **fields: Any) -> None:
        self.phase_dones.append((phase, fields))

    def agent_request(self, *, phase: str, agent: str, model: str) -> None:
        self.agent_requests.append({"phase": phase, "agent": agent, "model": model})

    def agent_retrying(
        self, *, phase: str, agent: str, model: str, attempt: int, reason: str
    ) -> None:
        self.agent_retryings.append(
            {"phase": phase, "agent": agent, "model": model, "attempt": attempt, "reason": reason}
        )

    def agent_response(self, *, phase: str, agent: str, model: str, **fields: Any) -> None:
        self.agent_responses.append({"phase": phase, "agent": agent, "model": model, **fields})


def _deployment(budget: float | None) -> SimpleNamespace:
    return SimpleNamespace(latency_budget_seconds=budget)


def _make_bridge(
    *,
    emitter: SSEEventEmitter,
    inner: _RecordingProgressEmitter,
    agents_config: dict[str, Any] | None = None,
) -> PipelineSSEProgressBridge:
    return PipelineSSEProgressBridge(
        emitter=emitter,
        invocation_id=_INVOCATION_ID,
        latency_budget_lookup=make_latency_budget_lookup(agents_config or {}),
        inner=inner,
    )


# ---------------------------------------------------------------------------
# Module helpers.
# ---------------------------------------------------------------------------


class TestToSchemaAgentName:
    def test_renames_the_three_sector_researchers(self) -> None:
        assert bridge_mod._to_schema_agent_name("tech_semis_researcher") == (
            "domain_researcher_tech_semis"
        )
        assert bridge_mod._to_schema_agent_name("financials_researcher") == (
            "domain_researcher_financials"
        )
        assert bridge_mod._to_schema_agent_name("energy_researcher") == "domain_researcher_energy"

    @pytest.mark.parametrize(
        "agent",
        [
            "qualitative_researcher",
            "adaptive_researcher",
            "synthesizer",
            "analyst",
            "strategist",
            "portfolio_manager",
        ],
    )
    def test_passes_other_agents_through_unchanged(self, agent: str) -> None:
        assert bridge_mod._to_schema_agent_name(agent) == agent


class TestMakeLatencyBudgetLookup:
    def test_returns_configured_budget_for_known_agent(self) -> None:
        lookup = make_latency_budget_lookup({"analyst": _deployment(123.0)})
        assert lookup("analyst") == 123.0

    @pytest.mark.parametrize(
        "agents_config",
        [
            pytest.param({}, id="unknown-agent"),
            pytest.param({"analyst": _deployment(None)}, id="budget-is-none"),
            pytest.param({"analyst": _deployment(0.0)}, id="zero-budget"),
            pytest.param({"analyst": _deployment(-5.0)}, id="negative-budget"),
        ],
    )
    def test_falls_back_to_default(self, agents_config: dict[str, Any]) -> None:
        lookup = make_latency_budget_lookup(agents_config)
        assert lookup("analyst") == DEFAULT_LATENCY_BUDGET_SECONDS == 60.0


# ---------------------------------------------------------------------------
# agent_request → agent_started.
# ---------------------------------------------------------------------------


class TestAgentRequest:
    async def test_in_schema_emits_started_with_translated_name_and_internal_keyed_budget(
        self,
    ) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        # Budget keyed by the INTERNAL name; the schema name is not a config key,
        # so a non-default value proves the lookup used the pre-translation name.
        bridge = _make_bridge(
            emitter=emitter,
            inner=inner,
            agents_config={"tech_semis_researcher": _deployment(90.0)},
        )

        async with emitter.subscribe() as queue:
            bridge.agent_request(phase="phase2", agent="tech_semis_researcher", model="claude-opus")
            record = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert queue.empty()

        assert record.name == "agent_started"
        assert record.data["invocation_id"] == _INVOCATION_ID
        assert record.data["agent_name"] == "domain_researcher_tech_semis"
        assert record.data["latency_budget_seconds"] == 90.0
        started_at = datetime.fromisoformat(record.data["started_at"])
        assert started_at.tzinfo is not None
        assert started_at.utcoffset() == timedelta(0)
        # Forwarded to inner with the original, untranslated agent name.
        assert inner.agent_requests == [
            {"phase": "phase2", "agent": "tech_semis_researcher", "model": "claude-opus"}
        ]

    async def test_out_of_schema_does_not_emit_but_forwards(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.agent_request(
                phase="phase1", agent="debug_probe_researcher", model="claude-haiku"
            )
            assert queue.empty()

        assert inner.agent_requests == [
            {"phase": "phase1", "agent": "debug_probe_researcher", "model": "claude-haiku"}
        ]

    async def test_emit_failure_is_swallowed_and_still_forwards(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _RaisingEmitter(SSEEventEmitter):
            def emit(self, event: Any) -> None:
                raise RuntimeError("emit boom")

        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=_RaisingEmitter(), inner=inner)

        with caplog.at_level(
            logging.ERROR, logger="alphamind.scheduler.control.sse_progress_bridge"
        ):
            # Must not raise even though emit() does.
            bridge.agent_request(phase="phase2", agent="analyst", model="claude-opus")

        assert "SSE agent_started emit failed" in caplog.text
        assert inner.agent_requests == [
            {"phase": "phase2", "agent": "analyst", "model": "claude-opus"}
        ]


# ---------------------------------------------------------------------------
# agent_response → agent_succeeded.
# ---------------------------------------------------------------------------


class TestAgentResponse:
    async def test_in_schema_emits_succeeded_with_clamping_and_none_when_zero(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.agent_response(
                phase="phase2",
                agent="strategist",
                model="claude-opus",
                duration_s=-2.5,  # clamps to 0.0
                input_tokens=-10,  # clamps to 0
                cache_read_tokens=0,  # → None
                cache_write_tokens=0,  # → None
                output_tokens=200,
                tool_calls=3,
                stop_reason="end_turn",
            )
            record = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert queue.empty()

        assert record.name == "agent_succeeded"
        assert record.data["invocation_id"] == _INVOCATION_ID
        assert record.data["agent_name"] == "strategist"
        assert record.data["duration_seconds"] == 0.0
        assert record.data["tokens_used"] == {
            "input": 0,
            "output": 200,
            "cache_read": None,
            "cache_creation": None,
        }

    async def test_in_schema_emits_nonzero_cache_tokens(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.agent_response(
                phase="phase2",
                agent="analyst",
                model="claude-opus",
                duration_s=12.5,
                input_tokens=1000,
                cache_read_tokens=300,
                cache_write_tokens=100,
                output_tokens=250,
                tool_calls=0,
                stop_reason="end_turn",
            )
            record = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert record.data["duration_seconds"] == 12.5
        assert record.data["tokens_used"] == {
            "input": 1000,
            "output": 250,
            "cache_read": 300,
            "cache_creation": 100,
        }

    async def test_out_of_schema_does_not_emit_but_forwards(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.agent_response(
                phase="phase1",
                agent="debug_probe_researcher",
                model="claude-haiku",
                duration_s=1.0,
                input_tokens=10,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=5,
                tool_calls=0,
                stop_reason="end_turn",
            )
            assert queue.empty()

        assert len(inner.agent_responses) == 1
        assert inner.agent_responses[0]["agent"] == "debug_probe_researcher"


# ---------------------------------------------------------------------------
# Inner-only pass-through signals.
# ---------------------------------------------------------------------------


class TestInnerOnlySignals:
    async def test_agent_retrying_forwards_only_even_for_in_schema_agent(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.agent_retrying(
                phase="phase2",
                agent="synthesizer",
                model="claude-opus",
                attempt=1,
                reason="empty_response",
            )
            assert queue.empty()

        assert inner.agent_retryings == [
            {
                "phase": "phase2",
                "agent": "synthesizer",
                "model": "claude-opus",
                "attempt": 1,
                "reason": "empty_response",
            }
        ]

    async def test_phase_start_and_done_forward_only(self) -> None:
        emitter = SSEEventEmitter()
        inner = _RecordingProgressEmitter()
        bridge = _make_bridge(emitter=emitter, inner=inner)

        async with emitter.subscribe() as queue:
            bridge.phase_start("phase1")
            bridge.phase_done("phase1", fills_processed=4)
            assert queue.empty()

        assert inner.phase_starts == ["phase1"]
        assert inner.phase_dones == [("phase1", {"fills_processed": 4})]
