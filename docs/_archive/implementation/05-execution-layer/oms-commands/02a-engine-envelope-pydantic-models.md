# 02a — Engine envelope Pydantic models

## Goal

Land the canonical Pydantic models for engine-originated command envelopes at `src/alphamind/execution/oms/engine_envelope.py`. Translates `docs/design/05-execution-layer/engine-envelope-schema.md` to typed Pydantic shapes — `EngineEnvelope`, `GuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult` — with `model_validator`s for cross-field invariants. Provides the contract the continuous monitor (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>) consumes when emitting protective CLOSEs, and that story 04's `submit_engine_envelope` validates on receipt.

## Reading

* `docs/design/05-execution-layer/engine-envelope-schema.md` — full JSON Schema (Draft 2020-12) for the engine envelope and the embedded CLOSE constraints (`risk_management_subtype: engine_guardrail`); cross-field invariants section
* `docs/design/05-execution-layer/oms-commands.md` § Command origins — protective CLOSE semantics, secondary breach checking, cascade logic
* `docs/design/06-risk-guardrails/breach-behavior.md` § Traceability for engine-originated actions — guardrail trigger record content, cascade_id semantics
* `docs/design/oms-command-ids.md` § Engine-originated command IDs — `MON.{session}.{trigger}.{ordinal}` envelope_id format
* `src/alphamind/execution/oms/command_models.py` (story 01a) — `CloseCommand` is what `EngineEnvelope.commands[0]` must be
* `src/alphamind/decision/portfolio_manager/models.py` — pattern for Pydantic envelope with `model_validator(mode="after")` and `Field(min_length=N, max_length=M)` on lists

## Depends on

* <issue id="11c3d121-2d77-4218-9355-90a234d5ff7b">ALP-370</issue> (01a) — engine envelope embeds `CloseCommand` from canonical command_models.py

## Scope

In scope, all under `src/alphamind/execution/oms/`. Tests at `tests/execution/oms/`.

### 1\. `engine_envelope.py`

Author the module exporting:

* **Vocabulary literals** — `SourceProvenance = Literal["engine_guardrail"]`; `SecondaryBreachResult = Literal["no_secondary_breach", "secondary_breach_avoided", "deferred_to_pm"]`.
* `BreachDetails(current_value: float, limit_value: float, overage: float, unit: str | None = None, regime_at_breach: str | None = None)` — frozen Pydantic model. Per design: `overage` is signed magnitude; `unit` and `regime_at_breach` are optional.
* `SecondaryBreachCheckResult(result: SecondaryBreachResult, notes: str | None = None)` — frozen Pydantic model. Captures the secondary-breach check's outcome when the monitor's position selection required one.
* `GuardrailTriggerRecord(rule_breached: str, trigger_timestamp: datetime, breach_details: BreachDetails, position_selection_rationale: str, cascade_id: str | None = None, secondary_breach_check_result: SecondaryBreachCheckResult | None = None)` — frozen Pydantic model.
* `EngineEnvelope(envelope_id: str, invocation_id: None = None, trigger_timestamp: datetime, source_provenance: SourceProvenance, guardrail_trigger_record: GuardrailTriggerRecord, commands: tuple[CloseCommand, ...])` — frozen Pydantic model. Field-level constraints: `envelope_id` matches `^MON\.[^.]+\.[0-9]+$`; `commands` has `Field(min_length=1, max_length=1)` per design (`maxItems: 1`); `source_provenance` is the const literal.
* `EngineEnvelope.model_validator(mode="after")` enforces:
  * `commands[0].close_rationale_type == "risk_management"` and `commands[0].risk_management_subtype == "engine_guardrail"` (the per-design embedded-command constraint).
  * Top-level `trigger_timestamp` equals `guardrail_trigger_record.trigger_timestamp` (denormalized for OMS intake convenience; equality is a structural invariant per design).
  * `invocation_id is None` (always null for engine-originated envelopes per design).
* **Schema-export accessor** — `def engine_envelope_schema() -> dict[str, Any]` returns the JSON Schema dict via `TypeAdapter(EngineEnvelope).json_schema()`.

### 2\. `__init__.py`

Append re-exports for `EngineEnvelope`, `GuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult`, `SourceProvenance`, `SecondaryBreachResult`, `engine_envelope_schema`.

### 3\. Tests at `tests/execution/oms/test_engine_envelope.py`

* Happy path: construct a valid `EngineEnvelope` with one CLOSE; round-trip via `TypeAdapter`.
* Negative: `commands` empty (raises); `commands` length 2 (raises per `max_length=1`); embedded CLOSE with `close_rationale_type != "risk_management"` (raises); embedded CLOSE with `risk_management_subtype != "engine_guardrail"` (raises); `invocation_id != None` (raises); top-level `trigger_timestamp` != `guardrail_trigger_record.trigger_timestamp` (raises); `envelope_id` not matching `MON.*.*` regex (raises).
* Cascade: envelope with `cascade_id` set parses cleanly; envelope with `cascade_id=None` parses cleanly.
* Secondary breach: each `SecondaryBreachResult` literal value parses cleanly; `notes` field optional.
* Schema export: `engine_envelope_schema()` returns a dict with `commands` constrained to `minItems: 1, maxItems: 1` and `source_provenance` as a const.

### Out of scope

* `submit_engine_envelope` write-path function — story 04
* Invocation-context integration / SQL writeback — story 04
* Continuous monitor's envelope production — that's the monitor's work tree

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/engine_envelope.py` exists and exports `EngineEnvelope`, `GuardrailTriggerRecord`, `BreachDetails`, `SecondaryBreachCheckResult`, `SourceProvenance`, `SecondaryBreachResult`, `engine_envelope_schema`.
- [ ] `EngineEnvelope` has `commands: tuple[CloseCommand, ...] = Field(min_length=1, max_length=1)` (single CLOSE per envelope).
- [ ] `EngineEnvelope.envelope_id` is constrained to `^MON\.[^.]+\.[0-9]+$` via `Field(pattern=...)` or model_validator.
- [ ] `EngineEnvelope.invocation_id` is typed as `None` and rejects non-None values.
- [ ] `model_validator` raises `ValidationError` when embedded CLOSE has `close_rationale_type != "risk_management"`.
- [ ] `model_validator` raises `ValidationError` when embedded CLOSE has `risk_management_subtype != "engine_guardrail"`.
- [ ] `model_validator` raises `ValidationError` when top-level `trigger_timestamp` != `guardrail_trigger_record.trigger_timestamp`.
- [ ] `EngineEnvelope` rejects empty `commands` list.
- [ ] `EngineEnvelope` rejects `commands` list longer than 1.
- [ ] All Pydantic models in this module use `model_config = ConfigDict(frozen=True)`.
- [ ] `engine_envelope_schema()` returns a dict whose `commands` property has `minItems: 1, maxItems: 1` and whose `source_provenance` property is `const: "engine_guardrail"`.
- [ ] `tests/execution/oms/test_engine_envelope.py` covers every acceptance criterion above and passes under `uv run pytest tests/execution/oms/test_engine_envelope.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/execution/oms/test_engine_envelope.py -n auto`. Spot-check that `engine_envelope_schema()` matches the JSON Schema in `docs/design/05-execution-layer/engine-envelope-schema.md` § Schema (every `$defs` entry appears, the `commands.allOf` constraints are reflected).
