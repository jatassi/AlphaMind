"""Targeted LLM component-evaluator — ALP-898.

The design's "targeted LLM evaluation (minimal tokens)" mode for thesis-
component resolution (``docs/design/05-execution-layer/thesis-model.md``
§ Three consumption modes). Given one ambiguous/qualitative component — the
programmatic assessor (story 04c) left it ``INCONCLUSIVE`` — this drives a
focused, minimal-context SDK call carrying *only* that component's narrative,
its key assumptions, the instrument reference, and a focused slice of market
data — never the full thesis, sibling legs, or bracket parameters. It returns
a schema-validated :class:`ThesisComponentOutcome` plus resolution notes.

Consumer of the shared analysis harness core
(:mod:`alphamind.analysis._harness_core`): the call rides ``invoke_sdk`` so the
``agent_calls`` capture (story 04a) records one row per evaluation, and the
diagnostic archive is preserved — exactly like the four analysis harnesses,
without re-implementing the SDK driver loop.

On-demand only. Invoked by the resolver (story 04e), *not* fired per invocation:
it is **not** a roster agent — it has no entry in the ``AgentName`` enum or
``config/agents.yaml`` and no ``run_types`` budget slot. The resolver supplies a
:class:`BaseAgentConfig` pointing at the minimal-eval prompt on demand; that
value object is not roster enrolment (the roster is the closed-enum-keyed
``AgentsConfig.agents`` dict). The agent name is a literal string — the
``agent_calls.agent_name`` column is a free ``Text`` field, not enum-constrained.

Per ``feedback_prompt_output_format_compat``: the prompt drives
``output_format={"type": "json_schema", ...}`` and carries no "begin with `{`"
prefill directive (which would hang the model in JSON-schema mode).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._harness_core import (
    DiagState,
    MalformedOutputFailure,
    _load_prompt,
    _render_raw_response,
    capture_agent_call,
    invoke_sdk,
)
from alphamind.config.models.agents import BaseAgentConfig
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentOutcome,
)

__all__ = [
    "ComponentLLMOutcome",
    "evaluate_component_llm",
]

# Non-roster agent identity. A literal string (not an ``AgentName`` member) so
# the ``agent_calls`` row lands without enrolling this helper in the scheduled
# agent roster. The ``agent_name`` column is free text — see module docstring.
_EVALUATOR_AGENT_NAME = "thesis_component_evaluator"

# Solo, single-turn structured call: no MCP tool loop, so no init-stall watchdog
# is needed and ``max_turns`` is small. The component + market data arrive in the
# user message; nothing is fetched via tools.
_MAX_TURNS = 1


# ---------------------------------------------------------------------------
# Boundary output model (Pydantic — the json_schema contract the SDK enforces)
# ---------------------------------------------------------------------------


class _ComponentEvaluation(BaseModel):
    """The structured payload the API enforces and surfaces on ResultMessage.

    Pydantic only because this is the ``output_format`` trust boundary; the
    parsed value is converted once into the frozen :class:`ComponentLLMOutcome`
    the caller consumes. ``outcome`` reuses 04c's :class:`ThesisComponentOutcome`
    ``StrEnum`` directly, so its closed vocabulary becomes an enum constraint in
    the generated JSON schema (the API rejects any out-of-set value).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ThesisComponentOutcome
    notes: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Result value object (frozen dataclass — the internal contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ComponentLLMOutcome:
    """The evaluator's verdict for one component: outcome + resolution notes."""

    outcome: ThesisComponentOutcome
    notes: str


# ---------------------------------------------------------------------------
# Functional core: focused minimal-context assembly
# ---------------------------------------------------------------------------


def _build_focused_context(component: ThesisComponent, market_data: str) -> str:
    """Assemble the minimal user-message context for one component (pure).

    Carries *only* the single component's identity, narrative, and key
    assumptions plus the focused market-data slice — never the wider thesis,
    sibling legs, or bracket parameters (thesis-model.md § Three consumption
    modes). ``market_data`` is a pre-rendered focused slice owned by the
    resolver (it decides what is "relevant"), keeping this evaluator a pure
    consumer of text rather than coupling it to a market-data record shape that
    does not yet exist.
    """
    assumptions = (
        "\n".join(f"- {ka.text}" for ka in component.key_assumptions)
        if component.key_assumptions
        else "(none recorded)"
    )
    return (
        "Evaluate this single thesis component against the market data below.\n\n"
        f"Component type: {component.component_type.value}\n"
        f"Instrument: {component.instrument_reference}\n\n"
        "Narrative:\n"
        f"{component.narrative}\n\n"
        "Key assumptions:\n"
        f"{assumptions}\n\n"
        "Relevant market data:\n"
        f"{market_data}\n"
    )


# ---------------------------------------------------------------------------
# SDK options builder
# ---------------------------------------------------------------------------


def _build_sdk_options(agent_config: BaseAgentConfig, *, prompt_text: str) -> Any:
    """Build :class:`ClaudeAgentOptions` for the focused evaluation call.

    Pins the autonomous-agent contract (``setting_sources=[]``, ``tools=[]``,
    ``strict-mcp-config``) and registers no MCP servers — the call is a single
    structured judgement over the in-context component + data, with no tool
    loop. ``output_format`` flips on JSON-Schema mode so the API enforces the
    ``_ComponentEvaluation`` shape; the dict surfaces on
    ``ResultMessage.structured_output``.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        system_prompt=prompt_text,
        model=agent_config.model,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        max_turns=_MAX_TURNS,
        setting_sources=[],
        extra_args={"strict-mcp-config": None},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(agent_config.output_token_budget)},
        output_format={"type": "json_schema", "schema": _ComponentEvaluation.model_json_schema()},
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def evaluate_component_llm(  # noqa: PLR0913 — leading domain args + the standard harness kwargs the resolver threads through every agent call
    component: ThesisComponent,
    market_data: str,
    *,
    agent_config: BaseAgentConfig,
    invocation_id: str,
    as_of: datetime | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = _EVALUATOR_AGENT_NAME,
    telemetry_session: AsyncSession | None = None,
    provenance_root: Path | None = None,
) -> ComponentLLMOutcome:
    """Evaluate one ambiguous/qualitative thesis *component* with a focused call.

    Drives a single, minimal-context SDK call via the shared harness core —
    only *component*'s narrative + key assumptions + instrument + the focused
    *market_data* slice are sent (not the full thesis) — and returns a
    schema-validated :class:`ComponentLLMOutcome`. The call rides ``invoke_sdk``,
    so when *telemetry_session* and *provenance_root* are wired one
    ``agent_calls`` row (story 04a) is captured for the evaluation.

    ``sdk_query_fn`` defaults to ``claude_agent_sdk.query``; tests inject a stub
    so no test touches the Anthropic API. ``archive_root=None`` skips diagnostic
    writes. Raises :class:`MalformedOutputFailure` when the SDK fails to populate
    the structured output or it does not satisfy the ``_ComponentEvaluation``
    contract; ``SDKFailure`` / ``TimeoutFailure`` / ``ContextOverflowFailure``
    propagate from the underlying SDK call.
    """
    if sdk_query_fn is None:
        from claude_agent_sdk import query as _real_query

        sdk_query_fn = _real_query

    prompt_text = await _load_prompt(agent_config.prompt)
    user_message = _build_focused_context(component, market_data)
    options = _build_sdk_options(agent_config, prompt_text=prompt_text)
    output_schema = _ComponentEvaluation.model_json_schema()

    diag = DiagState(
        agent_name=_EVALUATOR_AGENT_NAME,
        invocation_id=invocation_id,
        prompt_text=prompt_text,
        user_message=user_message,
        model=str(agent_config.model),
        archive_root=archive_root,
        as_of=as_of,
        response_filename="response.md",
        prompt_path=agent_config.prompt,
        output_schema=output_schema,
        sampling_params={"max_tokens": agent_config.output_token_budget},
    )
    wall_start = time.monotonic()

    async with capture_agent_call(
        diag, telemetry_session=telemetry_session, provenance_root=provenance_root
    ):
        outcome = await invoke_sdk(
            sdk_query_fn=sdk_query_fn,
            prompt=user_message,
            options=options,
            diag=diag,
            budget_seconds=float(agent_config.latency_budget_seconds),
            init_stall_timeout_seconds=None,
            wall_start=wall_start,
            agent_name=_EVALUATOR_AGENT_NAME,
            invocation_id=invocation_id,
            on_cli_result_error="sdk_failure",
            progress=progress,
            phase=phase,
        )

        payload = outcome.structured_output
        diag.response_initial = _render_raw_response(payload, outcome.response_text)
        diag.tokens_used = outcome.tokens_used
        diag.output_payload = payload

        evaluation = _parse_evaluation(
            payload, invocation_id=invocation_id, diag=diag, wall_start=wall_start
        )

        diag.write(
            success=True,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=outcome.stop_reason,
        )
        return ComponentLLMOutcome(outcome=evaluation.outcome, notes=evaluation.notes)


def _parse_evaluation(
    payload: dict[str, Any] | None,
    *,
    invocation_id: str,
    diag: DiagState,
    wall_start: float,
) -> _ComponentEvaluation:
    """Coerce the SDK's structured output into :class:`_ComponentEvaluation`.

    ``None`` (the SDK left ``structured_output`` unpopulated) and a payload that
    violates the contract both surface as :class:`MalformedOutputFailure`, the
    fail-closed signal the resolver acts on. A failed-outcome ``diag.write`` is
    stamped before raising so the same ``agent_calls`` capture seam records the
    failed evaluation (matching the four analysis harnesses' failure paths).
    """
    if payload is None:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=diag.last_stop_reason,
        )
        raise MalformedOutputFailure(
            "SDK did not populate structured_output for the component evaluation",
            agent_name=_EVALUATOR_AGENT_NAME,
            invocation_id=invocation_id,
        )
    try:
        return _ComponentEvaluation.model_validate(payload)
    except ValidationError as exc:
        diag.write(
            success=False,
            wall_clock_seconds=time.monotonic() - wall_start,
            stop_reason=diag.last_stop_reason,
        )
        raise MalformedOutputFailure(
            f"Component evaluation output failed schema validation: {exc}",
            agent_name=_EVALUATOR_AGENT_NAME,
            invocation_id=invocation_id,
            cause=exc,
        ) from exc
