"""Bridge :class:`ProgressEmitter` Protocol callbacks into SSE schema events (ALP-720).

The orchestrator and analysis/decision pipelines invoke the
:class:`alphamind._kernel.progress.ProgressEmitter` Protocol via
``progress.agent_request(phase, agent, model)`` and
``progress.agent_response(phase, agent, model, duration_s, ...)``.  The
SSE event vocabulary defined in
``docs/design/pipeline-control-and-events-schema.md`` carries
``agent_started`` and ``agent_succeeded`` events with the same
operational semantics but a different field shape (invocation_id,
latency_budget_seconds, tokens_used).

:class:`PipelineSSEProgressBridge` wraps the per-process
:class:`SSEEventEmitter` and an inner :class:`ProgressEmitter` so each
Protocol callback fires the matching SSE schema event AND the inner
emitter's debug-time JSONL write.  Constructed per-invocation so the
``invocation_id`` carried on every schema event is the current one.

Agent-name guard: the schema's ``_AgentName`` literal pins the closed
set of 9 names.  If a caller passes an ``agent`` string outside that
set (debug-only agents, internal helpers, future names not yet in the
schema), the bridge logs once and skips the SSE emit rather than
raising a Pydantic validation error and crashing the agent call site.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.control.models import (
    AgentStartedEvent,
    AgentSucceededEvent,
    TokensUsed,
)

__all__ = [
    "DEFAULT_LATENCY_BUDGET_SECONDS",
    "PipelineSSEProgressBridge",
]

log = logging.getLogger(__name__)


# The schema's _AgentName literal enumerates exactly these 9 names.
# Other agent strings (debug researcher names, future agents) flow
# through the bridge as no-op SSE emits.
_SCHEMA_AGENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "domain_researcher_tech_semis",
        "domain_researcher_financials",
        "domain_researcher_energy",
        "qualitative_researcher",
        "adaptive_researcher",
        "synthesizer",
        "analyst",
        "strategist",
        "portfolio_manager",
    }
)


# Internal :class:`alphamind.config.models.agents.AgentName` values differ
# from the schema's ``_AgentName`` vocabulary for the three domain researchers
# — internal naming is ``<sector>_researcher`` while the schema follows
# ``domain_researcher_<sector>``. This map normalizes at the bridge so the
# 6 agents whose internal + schema names agree pass through unchanged.
_INTERNAL_TO_SCHEMA_AGENT_NAME: Final[dict[str, str]] = {
    "tech_semis_researcher": "domain_researcher_tech_semis",
    "financials_researcher": "domain_researcher_financials",
    "energy_researcher": "domain_researcher_energy",
}


def _to_schema_agent_name(internal_name: str) -> str:
    """Translate the harness's internal agent name to the schema's enum value.

    Returns the internal name unchanged when no translation is needed (the
    6 agents whose names already agree). The caller checks membership
    against :data:`_SCHEMA_AGENT_NAMES` after translation.
    """
    return _INTERNAL_TO_SCHEMA_AGENT_NAME.get(internal_name, internal_name)


# Defensive default for latency_budget_seconds when the lookup misses.
# The schema requires exclusiveMinimum=0; 60s mirrors the harness's
# fallback timeout in src/alphamind/analysis/_sdk_subprocess.py.
DEFAULT_LATENCY_BUDGET_SECONDS: Final[float] = 60.0


def make_latency_budget_lookup(
    agents_config: Mapping[str, Any],
) -> Callable[[str], float]:
    """Build a lookup from ``resolved.agents.agents`` to latency budget seconds.

    The resolved config carries an ``AgentDeployment`` per agent name; each
    deployment has ``latency_budget_seconds``.  The returned closure looks
    up the agent by name and returns the configured budget, falling back to
    :data:`DEFAULT_LATENCY_BUDGET_SECONDS` for unknown agents.
    """

    def _lookup(agent_name: str) -> float:
        deployment = agents_config.get(agent_name)
        if deployment is None:
            return DEFAULT_LATENCY_BUDGET_SECONDS
        budget = getattr(deployment, "latency_budget_seconds", None)
        if not isinstance(budget, int | float) or budget <= 0:
            return DEFAULT_LATENCY_BUDGET_SECONDS
        return float(budget)

    return _lookup


class PipelineSSEProgressBridge:
    """Dual emitter: ProgressEmitter Protocol + SSE schema events.

    ``phase_start`` / ``phase_done`` pass through to the inner emitter
    only — the orchestrator emits schema-shaped ``phase_transition``
    events explicitly at phase boundaries via :meth:`emit_phase_transition`
    because the Protocol's phase taxonomy
    (``phase1`` / ``snapshot_assembly`` / ``phase2``) does not 1:1 with
    the schema's 5-phase taxonomy (``collect`` / ``distill`` / ``analyze``
    / ``decide`` / ``execute``).
    """

    def __init__(
        self,
        *,
        emitter: SSEEventEmitter,
        invocation_id: str,
        latency_budget_lookup: Callable[[str], float],
        inner: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    ) -> None:
        self._emitter = emitter
        self._invocation_id = invocation_id
        self._latency_budget_lookup = latency_budget_lookup
        self._inner = inner

    # ------------------------------------------------------------------
    # ProgressEmitter Protocol — pass through to inner; SSE emit on agent_*.
    # ------------------------------------------------------------------

    def phase_start(self, phase: str) -> None:
        self._inner.phase_start(phase)

    def phase_done(self, phase: str, **fields: Any) -> None:
        self._inner.phase_done(phase, **fields)

    def agent_request(self, *, phase: str, agent: str, model: str) -> None:
        schema_agent = _to_schema_agent_name(agent)
        if schema_agent in _SCHEMA_AGENT_NAMES:
            try:
                self._emitter.emit(
                    AgentStartedEvent(
                        invocation_id=self._invocation_id,
                        agent_name=schema_agent,  # type: ignore[arg-type]
                        started_at=datetime.now(UTC),
                        # Lookup uses the harness's internal name (the config
                        # key); only the SSE event renames.
                        latency_budget_seconds=self._latency_budget_lookup(agent),
                    )
                )
            except Exception:
                # The agent call site is hot path — log + continue so
                # an SSE emit bug never breaks a live invocation.
                log.exception("SSE agent_started emit failed agent=%s", agent)
        self._inner.agent_request(phase=phase, agent=agent, model=model)

    def agent_retrying(
        self, *, phase: str, agent: str, model: str, attempt: int, reason: str
    ) -> None:
        # Forward to the inner emitter only (the debug-e2e JSONL captures it),
        # mirroring the phase_start / phase_done pass-through. A schema-level
        # ``AgentRetryingEvent`` exists, but emitting it for this signal is
        # deferred: the synthesizer's ``reason="empty_response"`` is outside the
        # event's ``_FailureMode`` vocabulary and its two-retry budget exceeds
        # the event's documented attempt cap, so wiring the SSE emit needs a
        # coordinated schema + frontend change tracked separately (ALP-756
        # follow-up). The diagnostic ``errors.json`` is the prod-side record.
        self._inner.agent_retrying(
            phase=phase, agent=agent, model=model, attempt=attempt, reason=reason
        )

    def agent_response(  # noqa: PLR0913 - mirrors ProgressEmitter Protocol's 10-kwarg contract; cannot be reduced
        self,
        *,
        phase: str,
        agent: str,
        model: str,
        duration_s: float,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        tool_calls: int,
        stop_reason: str | None,
    ) -> None:
        schema_agent = _to_schema_agent_name(agent)
        if schema_agent in _SCHEMA_AGENT_NAMES:
            try:
                self._emitter.emit(
                    AgentSucceededEvent(
                        invocation_id=self._invocation_id,
                        agent_name=schema_agent,  # type: ignore[arg-type]
                        duration_seconds=max(0.0, duration_s),
                        tokens_used=TokensUsed(
                            input=max(0, input_tokens),
                            output=max(0, output_tokens),
                            cache_read=(max(0, cache_read_tokens) if cache_read_tokens else None),
                            cache_creation=(
                                max(0, cache_write_tokens) if cache_write_tokens else None
                            ),
                        ),
                    )
                )
            except Exception:
                log.exception("SSE agent_succeeded emit failed agent=%s", agent)
        self._inner.agent_response(
            phase=phase,
            agent=agent,
            model=model,
            duration_s=duration_s,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
        )
