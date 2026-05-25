"""Per-invocation MCP wrapper around the guardrail validation tool — story 04.

Wraps :func:`validate_guardrail` as a Claude Agent SDK MCP tool the analyst,
strategist, and PM call during reasoning. The factory captures the initial
``ValidationToolState`` in a mutable cell the tool callback reads and writes;
each PASS result advances the cell via ``state.with_accepted_proposal(delta)``
so subsequent calls within the same invocation see cumulative-impact tracking.

The factory is per-invocation: each call to
:func:`build_validate_guardrail_mcp_server` returns a fresh SDK MCP server
whose handler closes over its own state cell. This isolates one decision-layer
invocation's validation surface from another's.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from pydantic import ValidationError

from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.state_delivery.validation_tool import (
    BatchValidationResult,
    ValidationRequest,
    ValidationResult,
    ValidationToolState,
    _build_projected_delta_from,
    validate_guardrail,
    validate_guardrail_batch,
)

__all__ = [
    "build_initial_validation_state",
    "build_validate_guardrail_mcp_server",
]


_VALIDATE_GUARDRAIL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["instrument", "size", "action"],
    "properties": {
        "instrument": {
            "type": "object",
            "required": ["ticker", "asset_type"],
            "properties": {
                "ticker": {"type": "string"},
                "asset_type": {"enum": ["equity", "options", "strategy"]},
                "direction": {
                    "enum": ["long", "short"],
                    "description": (
                        "Position-level direction. Required for an equity or "
                        "options instrument; omit it for a multi-leg strategy "
                        "— a strategy's directional sign lives in its per-leg "
                        "directions, not a position-level field."
                    ),
                },
                "strike": {"type": "number"},
                "expiration": {"type": "string", "format": "date-time"},
                "contract_type": {"enum": ["call", "put"]},
                "legs": {"type": "array", "items": {"type": "object"}},
            },
        },
        "size": {
            "type": "object",
            "required": ["quantity", "dollar_value"],
            "properties": {
                "quantity": {"type": "integer", "minimum": 1},
                "dollar_value": {"type": "number", "minimum": 0},
                "premium_at_risk_usd": {"type": "number"},
            },
        },
        "action": {"enum": ["OPEN", "ADD", "CLOSE", "ADJUST"]},
        "reserves_capital": {"type": "boolean", "default": False},
    },
}


_VALIDATE_GUARDRAIL_BATCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": _VALIDATE_GUARDRAIL_INPUT_SCHEMA,
            "description": (
                "Ordered list of single-proposal validation requests. Each "
                "item follows the validate_guardrail input schema. Proposals "
                "are projected in order with prior-PASS impact threaded into "
                "subsequent projections."
            ),
        }
    },
}


@dataclass
class _ValidationStateCell:
    """Mutable container for ValidationToolState, captured by the MCP closure.

    The MCP tool callback reads this cell, calls validate_guardrail, and on PASS
    rewrites the cell with state.with_accepted_proposal(delta). FAIL leaves the
    cell unchanged.
    """

    state: ValidationToolState


def build_validate_guardrail_mcp_server(
    initial_state: ValidationToolState,
    *,
    server_name: str = "alphamind_decision_validation",
    include_batch_tool: bool = True,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *initial_state*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains
    ``mcp__<server_name>__validate_guardrail`` for one-off proposals and,
    when ``include_batch_tool`` is ``True``,
    ``mcp__<server_name>__validate_guardrail_batch`` for coordinated
    multi-position remedies. Both close over the same
    ``_ValidationStateCell`` so cumulative-impact tracking is shared.

    ``include_batch_tool`` defaults to ``True`` — the batch tool is wired
    for strategist and PM invocations. The analyst harness passes
    ``include_batch_tool=False`` because the analyst prompt does not document
    the batch tool and the agents.yaml ``tools`` allowlist for ``analyst``
    does not include it; the agents.yaml allowlist is the authoritative
    surface and the factory mirrors that for analyst-side composition.

    The factory captures *initial_state* in a ``_ValidationStateCell`` the
    tool callbacks read and write; each call's PASS result advances the cell
    via ``state.with_accepted_proposal(delta)`` so subsequent calls within
    this invocation see cumulative-impact tracking. For the batch tool, an
    aggregate-PASS result advances the cell once per per-proposal entry (one
    ``replace`` per proposal — same pattern as the single-call wrapper); an
    aggregate-FAIL or aggregate-UNAVAILABLE leaves the cell unchanged (no
    partial advancement).
    """
    cell = _ValidationStateCell(state=initial_state)

    @tool(
        "validate_guardrail",
        (
            "Validate a proposed instrument/size/action against current guardrails; "
            "returns per-rule pass/fail with current and projected-after headroom. "
            "overall is PASS, FAIL (a guardrail would breach), or UNAVAILABLE (the "
            "ticker is outside validation-infrastructure coverage this cycle — an "
            "infrastructure gap, not a breach; unavailable_reason names the gap). "
            "Use this for one-off proposals; for a coordinated multi-position "
            "package where each proposal would FAIL standalone because a "
            "portfolio-scoped rule stays red until all reductions are applied, "
            "use validate_guardrail_batch instead."
        ),
        _VALIDATE_GUARDRAIL_INPUT_SCHEMA,
    )
    async def _validate_guardrail(args: dict[str, Any]) -> dict[str, Any]:
        try:
            request = ValidationRequest.model_validate(_coerce_enum_case(args))
        except ValidationError as exc:
            return {
                "content": [{"type": "text", "text": _format_request_validation_error(exc)}],
                "is_error": True,
            }

        result = validate_guardrail(request=request, state=cell.state)

        if result.overall == "PASS":
            cell.state = cell.state.with_accepted_proposal(
                _build_projected_delta_from(request=request, result=result, state=cell.state)
            )

        return {
            "content": [{"type": "text", "text": _serialize_validation_result(result)}],
        }

    @tool(
        "validate_guardrail_batch",
        (
            "Validate a coordinated package of proposals as one transaction; "
            "returns per-proposal pass/fail plus a worst-of aggregate (FAIL > "
            "UNAVAILABLE > PASS). Use for multi-position remediation where each "
            "proposal would FAIL standalone because a portfolio-scoped rule "
            "stays red until all offending positions are reduced, but the "
            "cumulative package brings the rule back inside its limit. On "
            "aggregate PASS the cumulative-impact cell advances by one delta "
            "per proposal; on FAIL or UNAVAILABLE the cell is unchanged (no "
            "partial advancement). Input is {proposals: [<single-call shape>, ...]}."
        ),
        _VALIDATE_GUARDRAIL_BATCH_INPUT_SCHEMA,
    )
    async def _validate_guardrail_batch(args: dict[str, Any]) -> dict[str, Any]:
        raw_proposals = args.get("proposals", [])
        if not isinstance(raw_proposals, list):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Invalid validate_guardrail_batch request:\nproposals: must be an array"
                        ),
                    }
                ],
                "is_error": True,
            }
        try:
            requests = tuple(
                ValidationRequest.model_validate(_coerce_enum_case(p)) for p in raw_proposals
            )
        except ValidationError as exc:
            return {
                "content": [{"type": "text", "text": _format_request_validation_error(exc)}],
                "is_error": True,
            }

        result = validate_guardrail_batch(requests=requests, state=cell.state)

        if result.overall == "PASS":
            # Advance the cell once per per-proposal entry — one with_accepted_proposal
            # call per proposal, preserving the per-call replace pattern the single-call
            # wrapper uses. Threaded state propagates through each iteration's sector
            # lookup so a per-proposal cumulative_impact_note in the batch result
            # remains internally consistent with the cell after the batch.
            for request, per_proposal in zip(requests, result.per_proposal, strict=True):
                cell.state = cell.state.with_accepted_proposal(
                    _build_projected_delta_from(
                        request=request, result=per_proposal, state=cell.state
                    )
                )

        return {
            "content": [{"type": "text", "text": _serialize_batch_validation_result(result)}],
        }

    tools_list = [_validate_guardrail]
    allowed = [f"mcp__{server_name}__validate_guardrail"]
    if include_batch_tool:
        tools_list.append(_validate_guardrail_batch)
        allowed.append(f"mcp__{server_name}__validate_guardrail_batch")
    server = create_sdk_mcp_server(name=server_name, tools=tools_list)
    return {server_name: server}, allowed


def build_initial_validation_state(  # noqa: PLR0913 — runner-facing assembler mirrors ValidationToolState fields
    *,
    invocation_id: str,
    starting_snapshot: PortfolioStateSnapshot,
    starting_risk_budget: RiskBudgetConsumption,
    starting_active_risk_parameters: ActiveRiskParameterSet,
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
) -> ValidationToolState:
    """Construct the initial :class:`ValidationToolState` for one decision-layer
    invocation (analyst, strategist, or PM).

    Returns a state with ``accumulated_deltas=()`` so the first
    ``validate_guardrail`` call sees itself as proposal #1. The runner (story
    08) constructs this once per invocation; cumulative tracking via the MCP
    wrapper closes over the result.
    """
    return ValidationToolState(
        invocation_id=invocation_id,
        starting_snapshot=starting_snapshot,
        starting_risk_budget=starting_risk_budget,
        starting_active_risk_parameters=starting_active_risk_parameters,
        profile_feature_flags=profile_feature_flags,
        library_config=library_config,
        library_market=library_market,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
        accumulated_deltas=(),
    )


# ---------------------------------------------------------------------------
# Input-error formatting
# ---------------------------------------------------------------------------


def _format_request_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic ValidationError as agent-friendly field-path lines.

    Each error becomes one ``<dotted.field.path>: <message>`` line so the
    agent can locate the offending field and revise the call. The full
    Pydantic representation (with documentation URLs and input dumps) is
    omitted — agent output is bounded by token budget, and the dotted path
    plus message contain the actionable signal.
    """
    lines = "\n".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
    )
    return f"Invalid validate_guardrail request:\n{lines}"


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------


_INSTRUMENT_ENUM_FIELDS = ("asset_type", "direction")


def _uppercase_enum_fields(obj: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *obj* with ``asset_type`` and ``direction``
    uppercased when present as strings."""
    coerced = dict(obj)
    for field in _INSTRUMENT_ENUM_FIELDS:
        value = coerced.get(field)
        if isinstance(value, str):
            coerced[field] = value.upper()
    return coerced


def _coerce_enum_case(args: dict[str, Any]) -> dict[str, Any]:
    """Uppercase agent-facing enum values to match the underlying StrEnum members.

    The MCP input schema exposes lowercase ``asset_type`` and ``direction``
    (``"equity"``, ``"long"``) as the agent-facing surface; the underlying
    :class:`InstrumentType` and :class:`Direction` enums are uppercase. The
    ``contract_type`` enum (``"call"``/``"put"``) is consumed as-is by the
    instrument's ``Literal`` field, so it is not coerced here.
    """
    instrument = args.get("instrument")
    if not isinstance(instrument, dict):
        return args
    coerced_instrument = _uppercase_enum_fields(instrument)
    legs = coerced_instrument.get("legs")
    if isinstance(legs, list):
        coerced_instrument["legs"] = [
            _uppercase_enum_fields(leg) for leg in legs if isinstance(leg, dict)
        ]
    return {**args, "instrument": coerced_instrument}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_validation_result(result: ValidationResult) -> str:
    """JSON-serialise *result* with a UTC ``checked_at`` timestamp.

    The analyst, strategist, and PM schemas mirror the tool's output with the
    addition of ``checked_at`` — emitted here at serialization time so the
    invocation runtime, not the deterministic library, owns the wall clock.
    """
    payload = result.model_dump(mode="json")
    payload["checked_at"] = datetime.now(UTC).isoformat()
    return json.dumps(payload)


def _serialize_batch_validation_result(result: BatchValidationResult) -> str:
    """JSON-serialise *result* with a UTC ``checked_at`` timestamp.

    Mirrors :func:`_serialize_validation_result` — emits the batch envelope
    plus a top-level ``checked_at`` so consumers see a single wall-clock
    stamp for the whole batch decision. Per-proposal entries carry the same
    fields as a single-call ``ValidationResult`` (sans ``checked_at`` — the
    batch's stamp is authoritative).
    """
    payload = result.model_dump(mode="json")
    payload["checked_at"] = datetime.now(UTC).isoformat()
    return json.dumps(payload)
