# 10e — Convert risk_guardrails state_delivery internal Pydantic types to frozen dataclass

## Goal

Convert the 5 `state_delivery/portfolio_manager.py` parameter-bag Pydantic types to `@dataclass(frozen=True, slots=True)`: `CrossConstraintImpactPerRule` (L61), `CrossConstraintImpact` (L74), `RegimeOverride` (L85), `CorrelationState` (L95), `DependencyRiskFlag` (L108). These are inputs to a string-rendering function (`render_pm_header`) — they never cross HTTP/MCP/file/DB boundaries. The audit's risk_guardrails LB-2 names these as textbook P5 violations.

Also verify the `breach_behavior/types.py` BaseModel population — 10 of those are likely correct boundary types (engine-envelope JSON schema mirrors). Triage and convert any that aren't.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L5 + risk_guardrails-subdivision LB-2
* `src/alphamind/risk_guardrails/state_delivery/portfolio_manager.py:61,74,85,95,108` — 5 parameter-bag types
* `src/alphamind/risk_guardrails/breach_behavior/types.py` — 10 L9 hits; verify each per engine-envelope schema dependency
* `src/alphamind/risk_guardrails/breach_behavior/{config,emergency_triggers,position_selection,secondary_breach}.py` — 6 more L9 hits; triage
* Story 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/regime.py` extraction; this story comes after; types depend on the cleanly-extracted enums

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — enum extraction must complete first so the 10 `breach_behavior/types.py` BaseModels reference enums from `_kernel.regime` cleanly

## Scope

In scope: convert 5 state_delivery parameter-bags + triage and convert any breach_behavior internal types not strictly boundary. Tests update.

### 1\. state_delivery/portfolio_manager.py conversions

```python
# Before
class CrossConstraintImpactPerRule(BaseModel):
    rule_name: str
    impact_usd: float
    ...

# After
@dataclass(frozen=True, slots=True)
class CrossConstraintImpactPerRule:
    rule_name: str
    impact_usd: Money  # was float (uses _kernel.money from stories 04+05b)
    ...
```

### 2\. breach_behavior types triage

For each of the 10 `breach_behavior/types.py` BaseModel hits, verify:

* Does it appear in `engine-envelope-schema.md` JSON schema? → boundary, keep Pydantic
* Is it consumed by an MCP tool? → boundary, keep Pydantic
* Is it round-tripped via codec? → likely boundary, keep Pydantic
* Otherwise → convert to frozen dataclass

Likely-internal candidates per audit: `HaltState`, `EmergencyContext` (verify).

Similar triage for the 6 hits in `breach_behavior/{config,emergency_triggers,position_selection,secondary_breach}.py`.

### 3\. state_delivery/validation_tool_mcp.py

`_ValidationStateCell` at line 83 is documented mutable (MCP closure for cumulative state). Mark as warranted; do not convert.

### Out of scope

`state_delivery/validation_tool.py` 7 Pydantic types — these cross the MCP tool boundary (validated via `ValidationRequest.model_validate(args)`); keep as Pydantic.

## Acceptance criteria

- [ ] 5 state_delivery/portfolio_manager.py parameter-bag types are `@dataclass(frozen=True, slots=True)`.
- [ ] breach_behavior/types.py types triaged: boundary types stay Pydantic with documented rationale; internal types are frozen dataclass.
- [ ] `_ValidationStateCell` documented as warranted mutable (`# noqa: D-style comment` or inline rationale).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "class.*BaseModel" src/alphamind/risk_guardrails/state_delivery/portfolio_manager.py` returns zero hits. Spot-check rendering: `render_pm_header(...)` produces byte-identical output pre/post conversion (the function signature changes but the return value is the same string).