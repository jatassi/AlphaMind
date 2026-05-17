# 06 — Analyst input bundle assembler

## Goal

Implement the function that composes the user-message text the analyst harness (story 07) sends to the LLM. The bundle is `=== GUARDRAIL STATE ===` block + a brief tool-reminder + synthesizer brief text. Pure function — no I/O, deterministic. Selects between normal-mode and halt-mode renderers based on the caller-supplied `mode` parameter.

## Reading

* `src/alphamind/analysis/synthesizer/input_bundle.py` — sibling pattern (composes regime + tool-list + brief texts). The analyst's bundle is structurally simpler — one brief, one header, two tools.
* `src/alphamind/risk_guardrails/state_delivery/__init__.py` — exports `render_analyst_header` and `render_analyst_header_halt_mode`. The bundle assembler calls one or the other.
* `src/alphamind/risk_guardrails/state_delivery/analyst.py` — `render_analyst_header` signature (the parameters the caller must supply).
* `src/alphamind/risk_guardrails/state_delivery/halt_mode.py` — `render_analyst_header_halt_mode` signature.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool_mcp.py` (story 04) — for the canonical `validate_guardrail` MCP tool name (`mcp__alphamind_decision_validation__validate_guardrail`).
* `src/alphamind/analysis/synthesizer/retrieval_tools.py` — `build_retrieve_brief_mcp_server` for the `retrieve_brief` tool name (`mcp__alphamind_synthesizer_retrieval__retrieve_brief`).
* `docs/design/04-decision-layer/analyst.md` § Inputs — the exact contract the prompt's `<inputs>` block consumes.
* `prompts/decision/analyst.md` — the prompt's `<inputs>` block describes user-turn order: header THEN brief.

## Depends on

* `04 — validate_guardrail MCP tool wrapper` (ALP-294, this work tree) — for the canonical tool-name surface. The bundle's tool-reminder section names the two tools; matching the actual `allowed_tools` list prevents drift.

## Scope

In scope: `src/alphamind/decision/analyst/input_bundle.py`. Tests at `tests/decision/analyst/test_input_bundle.py`.

### 1\. Public function — normal mode

```python
def assemble_input_bundle_normal(
    *,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
) -> str:
    """Compose the analyst's user-message text for a normal-mode invocation.

    Renders the guardrail header via ``render_analyst_header``, then a
    brief tool-reminder section, then the synthesizer brief verbatim.
    """
```

### 2\. Public function — halt (watchlist) mode

```python
def assemble_input_bundle_halt(
    *,
    # subset of normal-mode parameters relevant to the halt-mode renderer's signature
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    halt_context: HaltModeContext,
    synthesizer_brief_text: str,
    tool_names: tuple[str, ...],
) -> str:
    """Compose the analyst's user-message text for a halt-mode invocation.

    Renders the halt-mode guardrail header via ``render_analyst_header_halt_mode``,
    notes that watchlist mode is in effect (no validate_guardrail use), then the
    synthesizer brief verbatim.
    """
```

(The exact parameter list mirrors `render_analyst_header_halt_mode`'s signature; consult that module to confirm the parameter set and types.)

### 3\. Body shape

The bundle interleaves three sections separated by blank lines:

```
{rendered_guardrail_header}

=== AVAILABLE TOOLS ===
- validate_guardrail (call once per proposal before finalizing; OPEN action only)
- retrieve_brief (optional; pull source-brief sections by reference ID)

=== SYNTHESIZER BRIEF ===
{synthesizer_brief_text}
```

Halt-mode shape replaces the `validate_guardrail` line with a note that watchlist mode does not call the tool. The tool-reminder section's tool list is supplied by the caller via `tool_names`; the assembler renders one bullet per tool name. Tool names already include the `mcp__<server>__<name>` prefix; the assembler does not strip or rewrite them.

### 4\. Public surface

```python
__all__ = ["assemble_input_bundle_normal", "assemble_input_bundle_halt"]
```

### Out of scope

* The actual tool-name wiring — `tool_names` is supplied by the runner (story 08), which builds them from the factory return.
* The retrieval-store assembly — the synthesizer ships the store; the runner passes it through.
* Any post-bundle string mutation — the assembler is the last touchpoint before the harness's `query(prompt=user_message, ...)` call.

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/input_bundle.py` exists with `assemble_input_bundle_normal` and `assemble_input_bundle_halt`.
- [ ] Both functions are pure — no logging, no I/O, deterministic on identical inputs.
- [ ] The normal-mode bundle begins with `=== GUARDRAIL STATE` (the envelope marker from `render_envelope_open`) and ends with the synthesizer brief.
- [ ] The halt-mode bundle includes the halt-mode envelope and notes watchlist mode in the tool section.
- [ ] The tool-reminder section names exactly the two analyst tools (per the `tool_names` parameter), one bullet per tool.
- [ ] `tests/decision/analyst/test_input_bundle.py` covers: normal-mode round-trip (output contains all expected sections); halt-mode round-trip; tool-name interpolation; empty held-positions case; empty abandoned-openings case; one populated case for each.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_input_bundle.py -n auto` passes. Manually inspect a sample bundle output — confirm the structure matches the prompt's `<inputs>` block expectation (header first, then brief).