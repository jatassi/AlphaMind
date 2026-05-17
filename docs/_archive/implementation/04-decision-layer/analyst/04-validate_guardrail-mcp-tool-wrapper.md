# 04 — `validate_guardrail` MCP tool wrapper

## Goal

Wrap the existing `validate_guardrail` Python function (`alphamind.risk_guardrails.state_delivery.validation_tool`) as a Claude Agent SDK MCP tool the analyst, strategist, and PM all call during reasoning. Tool input mirrors `ValidationRequest` (instrument, size, action, optional reserves_capital); tool output is a JSON-serialized `ValidationResult` returned in the MCP `text` content so the model copies fields verbatim into its `position_size.delta_adjusted_exposure` and `guardrail_validation_result`. The wrapper holds a mutable cell over `ValidationToolState` so successive calls within one invocation accumulate cumulative-impact tracking. Ships at `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` — strategist (ALP-116) and PM (ALP-117) work trees consume the wrapper unchanged.

## Reading

* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` — `validate_guardrail`, `ValidationRequest`, `ValidationResult`, `ValidationToolState`, `ProjectedDelta`, `ValidationInstrument`, `ValidationSize`, `ValidationAction`. The wrapper composes against these.
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — canonical pattern for an MCP wrapper that closes over per-invocation state. The validate_guardrail wrapper has the same shape (factory returning `(mcp_servers_dict, allowed_tool_names)`) but holds a mutable cell rather than an immutable store.
* `src/alphamind/analysis/synthesizer/portfolio_tools.py` — second canonical pattern for per-invocation MCP wrapper.
* `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — input contract, output contract, behavioral contract (cumulative tracking, state reset at agent-invocation boundaries).
* `docs/design/04-decision-layer/analyst.md` § Pre-submission guardrail validation — analyst's two-layer workflow with the tool.
* `src/alphamind/portfolio_state/records/positions.py` — `Direction`, `InstrumentType` enums used by `ValidationInstrument`.

## Depends on

(none — independent of stories 03/05a/05b/06; consumed by 07)

## Scope

In scope: `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` plus the package `__init__.py` re-export. Tests at `tests/risk_guardrails/state_delivery/test_validation_tool_mcp.py`.

### 1\. The state-cell dataclass

```python
@dataclass
class _ValidationStateCell:
    """Mutable container for ValidationToolState, captured by the MCP closure.

    The MCP tool callback reads this cell, calls validate_guardrail, and on PASS
    rewrites the cell with state.with_accepted_proposal(delta). FAIL leaves the
    cell unchanged.
    """
    state: ValidationToolState
```

### 2\. The factory function

```python
def build_validate_guardrail_mcp_server(
    initial_state: ValidationToolState,
    *,
    server_name: str = "alphamind_decision_validation",
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *initial_state*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains
    the single name ``mcp__<server_name>__validate_guardrail``.

    The factory captures *initial_state* in a ``_ValidationStateCell`` the
    tool callback reads and writes; each call's PASS result advances the cell
    via ``state.with_accepted_proposal(delta)`` so subsequent calls within
    this invocation see cumulative-impact tracking.
    """
```

### 3\. The tool input schema

JSON Schema accepted by the SDK's `@tool` decorator. Mirrors `ValidationRequest`:

```python
_VALIDATE_GUARDRAIL_INPUT_SCHEMA = {
    "type": "object",
    "required": ["instrument", "size", "action"],
    "properties": {
        "instrument": {
            "type": "object",
            "required": ["ticker", "asset_type", "direction"],
            "properties": {
                "ticker": {"type": "string"},
                "asset_type": {"enum": ["equity", "options", "strategy"]},
                "direction": {"enum": ["long", "short"]},
                "strike": {"type": "number"},
                "expiration": {"type": "string", "format": "date-time"},
                "contract_type": {"enum": ["call", "put"]},
                "legs": {"type": "array", "items": {"type": "object"}}
            }
        },
        "size": {
            "type": "object",
            "required": ["quantity", "dollar_value"],
            "properties": {
                "quantity": {"type": "integer", "minimum": 1},
                "dollar_value": {"type": "number", "minimum": 0},
                "premium_at_risk_usd": {"type": "number"}
            }
        },
        "action": {"enum": ["OPEN", "ADD", "CLOSE", "ADJUST"]},
        "reserves_capital": {"type": "boolean", "default": False}
    }
}
```

The wrapper coerces the dict input to a `ValidationRequest` (catching Pydantic ValidationError and surfacing as a tool-error MCP response with `isError: true`).

### 4\. The tool callback

```python
@tool("validate_guardrail", "<one-line description>", _VALIDATE_GUARDRAIL_INPUT_SCHEMA)
async def _validate_guardrail(args: dict[str, Any]) -> dict[str, Any]:
    try:
        request = ValidationRequest.model_validate(args)
    except ValidationError as exc:
        return {
            "content": [{"type": "text", "text": _format_request_validation_error(exc)}],
            "isError": True,
        }

    result = validate_guardrail(request=request, state=cell.state)

    # On PASS, advance the cell with the projected delta.
    if result.overall == "PASS":
        delta = ProjectedDelta(
            instrument=request.instrument,
            size=request.size,
            action=request.action,
            sector=cell.state.sector_resolver(request.instrument.ticker),
            delta_adjusted_exposure=result.delta_adjusted_exposure,
            greeks=result.greeks,
            proposal_index=result.proposal_index_in_invocation,
            reserves_capital=request.reserves_capital,
            existing_position_id=None,  # OPEN actions don't have one
        )
        cell.state = cell.state.with_accepted_proposal(delta)

    return {
        "content": [{"type": "text", "text": _serialize_validation_result(result)}],
    }
```

`_serialize_validation_result(result)` returns a JSON-formatted string with fields the analyst's schema mirrors:

```json
{
  "overall": "PASS",
  "per_rule": [...],
  "delta_adjusted_exposure": 3370.0,
  "greeks": {...},
  "cumulative_impact_note": "...",
  "failure_guidance": null,
  "checked_at": "..."
}
```

The fields match `GuardrailValidationResult` in story 03's model. Use `result.model_dump_json(...)` or hand-build to ensure stable ordering and ISO 8601 timestamps. `checked_at` is computed at serialization time from a UTC clock.

### 5\. The state-construction helper (for the runner)

A small helper the runner (story 08) calls to build `initial_state` from upstream inputs:

```python
def build_initial_validation_state(
    *,
    invocation_id: str,
    starting_snapshot: PortfolioStateSnapshot,
    starting_risk_budget: RiskBudgetConsumption,
    starting_active_risk_parameters: ActiveRiskParameterSet,
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    sector_resolver: Callable[[str], str],
    borrow_cost_resolver: Callable[[str], float] | None = None,
) -> ValidationToolState:
    """Construct the initial ValidationToolState for an analyst (or strategist/PM) invocation."""
```

Returns a `ValidationToolState` with `accumulated_deltas=()`.

### 6\. Re-export from package **init**

Add `build_validate_guardrail_mcp_server` and `build_initial_validation_state` to `src/alphamind/risk_guardrails/state_delivery/__init__.py` `__all__`.

### Out of scope

* The CLOSE/ADJUST proposal-flow paths (analyst only emits OPEN; strategist + PM stories 04-equivalents will exercise CLOSE/ADJUST). The wrapper supports them by enum, but the test scope here covers OPEN.
* Cross-invocation state caching (state-reset at invocation boundary is owned by the runner — fresh state per invocation).
* The validation tool's underlying math (lives in `guardrail_evaluation` library, already shipped).

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` exists with `_ValidationStateCell`, `build_validate_guardrail_mcp_server`, `build_initial_validation_state`.
- [ ] `build_validate_guardrail_mcp_server(state)` returns `(mcp_servers_dict, allowed_tools_list)` of the shape `tuple[dict[str, McpSdkServerConfig], list[str]]`. Allowed-tools list has one entry: `mcp__alphamind_decision_validation__validate_guardrail`.
- [ ] The tool callback coerces dict input to `ValidationRequest` via `model_validate`; ValidationError yields an MCP `isError: true` content with a clear field-path message.
- [ ] On PASS, the cell's state is advanced via `with_accepted_proposal(...)`; subsequent calls see cumulative-impact in their `cumulative_impact_note`.
- [ ] On FAIL, the cell's state is unchanged; the failure guidance is included in the JSON-serialized response.
- [ ] The serialized response includes `overall`, `per_rule[]`, `delta_adjusted_exposure`, `greeks`, `cumulative_impact_note`, `failure_guidance`, `checked_at` — every field the analyst's `GuardrailValidationResult` schema requires.
- [ ] `tests/risk_guardrails/state_delivery/test_validation_tool_mcp.py` covers: factory shape; PASS path advances cell; FAIL path preserves cell; cumulative tracking across two PASS calls; ValidationError input handled cleanly; one OPEN-equity scenario; one OPEN-options scenario (when feature-flag enables); disabled-feature returns FAIL with `feature_disabled` reason intact.
- [ ] `src/alphamind/risk_guardrails/state_delivery/__init__.py` re-exports the factory + helper.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/risk_guardrails/state_delivery/test_validation_tool_mcp.py -n auto` passes. Manually instantiate the factory in a Python REPL with a minimal fixture state, call the tool callback with a sample request dict, and confirm the returned text content is valid JSON containing the expected fields.