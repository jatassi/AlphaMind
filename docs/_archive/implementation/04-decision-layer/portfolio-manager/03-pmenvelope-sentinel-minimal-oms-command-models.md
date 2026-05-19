# 03 — PMEnvelope + sentinel + minimal OMS command models

## Goal

Author the typed Pydantic models the PM work tree builds on: (1) `PMEnvelope` — discriminated union (`pm_analyst` / `pm_strategist`) mirroring `pm-envelope-schema.md` with the verdict-conditional invariants and the embedded-command shape; (2) `PMCompletionRecord` — the thin completion sentinel the harness's JSON-Schema mode targets per parent decision (D); (3) inline minimal Pydantic OMS command models (5 types: open / close / adjust / cancel / add) at `oms_command_models.py` per parent decision (C), modeling only fields the Layer-2/3 validator reads. Plus a parity test that asserts `PMEnvelope.model_json_schema()` matches the design doc's `pm-envelope-schema.md` byte-equivalently up to `$ref` indirection. Mirrors strategist story 03 ([ALP-303](<https://linear.app/alphamind-jatassi/issue/ALP-303>)).

## Reading

* `docs/design/04-decision-layer/pm-envelope-schema.md` — authoritative envelope JSON Schema; the Pydantic translation must match byte-for-byte up to `$ref` indirection.
* `docs/design/04-decision-layer/portfolio-manager.md` § Envelope structure, § Evaluation framework, § Verdict aggregation, § Anti-patterns — prose definitions for the criterion enums, anti-pattern canonical strings, and verdict invariants.
* `docs/design/05-execution-layer/oms-command-schema.md` § open_command / close_command / adjust_command / cancel_command / add_command — the embedded-command shape; this story models only the fields the Layer-2/3 validator reads.
* `docs/design/oms-command-ids.md` — `command_id` format and the `attempt_seq` ↔ `post_rejection` modification count bijection.
* `src/alphamind/decision/strategist/models.py` — sibling Pydantic translation pattern (discriminated unions, model_json_schema export, frozen=True, pattern regex on string fields).
* `src/alphamind/decision/proposal_pre_processor/models.py` — pre-processor's pattern of inlining types from upstream (similar pragmatic stance to this story's minimal OMS command models).
* `src/alphamind/risk_guardrails/guardrail_evaluation` — `RuleProjection` and `Greeks` types this story imports for embedded `validation_metadata` and `acknowledgment.greeks`. Do not redefine.
* `src/alphamind/decision/analyst/models.py` — for the `Sector` enum (risk-side 4-way: tech / semis / financials / energy) the embedded OMS command instrument shape uses.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decisions (C), (D), (L) for the model shape.

## Depends on

None — Wave 1 dispatch.

## Scope

Code under `src/alphamind/decision/portfolio_manager/`. Tests under `tests/decision/portfolio_manager/`.

### 1\. `models.py` — `PMEnvelope`, evaluation models, modification record, sentinel

Author `src/alphamind/decision/portfolio_manager/models.py` exporting the following Pydantic models. All models `frozen=True` and use `model_config = ConfigDict(frozen=True)`. Field types use `tuple[...]` where the schema's `array` is read-only; `Decimal` is acceptable for monetary fields if the design uses it.

**Top-level types:**

* `PMEnvelope` — discriminated union via Pydantic's `Field(discriminator="source_provenance")` over `PMAnalystEnvelope` and `PMStrategistEnvelope`. Required-field set per `pm-envelope-schema.md` § required.
* `PMAnalystEnvelope` — `source_provenance: Literal["pm_analyst"]`, `recommendation_type: Literal["new_entry"]`, `envelope_id: str` matching `^ENV-REC-[0-9]+$`, `source_recommendation_id: str` matching `^REC-[0-9]+$`, `position_id: None` (forbidden — schema's `false`), `evaluation: ThesisQualityEvaluation`, `verdict: Verdict` enum, `modifications: tuple[ModificationRecord, ...]`, `concerns: tuple[ConcernRecord, ...]`, `rationale_narrative: str`, `anti_patterns_identified: tuple[AntiPattern, ...] | None`, `commands: tuple[OMSCommand, ...]`. Verdict-conditional invariants enforced via Pydantic `model_validator(mode='after')` on the union container.
* `PMStrategistEnvelope` — `source_provenance: Literal["pm_strategist"]`, `recommendation_type: Literal["position_assessment", "pending_order_assessment"]`, `envelope_id: str` matching `^ENV-(SA|SA-ORD)-[0-9]+$`, `source_recommendation_id: str` matching `^SA(-ORD)?-[0-9]+$`, `position_id: str` (required), `evaluation: PositionActionEvaluation`, plus the same verdict-conditional invariants.
* `PMCompletionRecord` — `invocation_id: str`, `timestamp: datetime`, `envelopes_submitted: int` (≥ 0), `verdict_summary: VerdictSummary`. The harness's JSON-Schema-mode target.
* `VerdictSummary` — `approve: int` (≥ 0), `approve_with_modification: int` (≥ 0), `reject: int` (≥ 0). Invariant: `approve + approve_with_modification + reject == envelopes_submitted` (asserted by `PMCompletionRecord.model_validator(mode='after')`).

**Evaluation models:**

* `ThesisQualityEvaluation` — five required keys: `falsifiability`, `sizing_proportionality`, `portfolio_coherence`, `timing_plausibility`, `counterargument_consideration`. Each value is `CriterionAssessment`.
* `PositionActionEvaluation` — four required keys: `status_classification_warrant`, `action_status_alignment`, `action_specific_justification`, `portfolio_coherence`. Each value is `CriterionAssessment`.
* `CriterionAssessment` — `status: Literal["pass", "fail"]`, `note: str | None = None`.

**Modification + concern + anti-pattern:**

* `ModificationRecord` — `phase: Literal["pre_submission", "post_rejection"]`, `field_changed: str`, `original_value: Any`, `approved_value: Any`, `adjustment_category: AdjustmentCategory` (enum: `risk_reduction` / `conviction_disagreement` / `capital_constraint` / `portfolio_balance` / `guardrail_rejection_response`), `rationale: str`, `triggering_rule: str | None = None`. Cross-field invariant: `adjustment_category == "guardrail_rejection_response"` ↔ `phase == "post_rejection"` AND `triggering_rule` populated.
* `ConcernRecord` — `source: str` (description: matches an evaluation criterion key or `"other"`), `summary: str`.
* `AntiPattern` — Literal alias of the five canonical strings: `"conviction_inflation"`, `"sunk_cost_persistence"`, `"rationalized_continuation"`, `"thesis_contradiction_suppression"`, `"engine_originated_closure_signal"`.

**Verdict + provenance enums:**

* `Verdict` — Literal `"approve"` / `"approve_with_modification"` / `"reject"`.
* `SourceProvenance` — Literal `"pm_analyst"` / `"pm_strategist"`. (Not strictly necessary as a separate alias since the union discriminates, but useful as a typed value for the validator.)
* `RecommendationType` — Literal `"new_entry"` / `"position_assessment"` / `"pending_order_assessment"`.

**Verdict-conditional invariants** (Pydantic `model_validator(mode='after')` on each envelope variant):

* `verdict == "reject"` → `len(commands) == 0` AND `len(modifications) == 0` AND `len(concerns) >= 1`.
* `verdict == "approve"` → `len(modifications) == 0`.
* `verdict == "approve_with_modification"` → `len(modifications) >= 1` AND `len(commands) >= 1`.

**Public surface from** `__init__.py`:

* `PMEnvelope`, `PMAnalystEnvelope`, `PMStrategistEnvelope`, `PMCompletionRecord`, `VerdictSummary`.
* `ThesisQualityEvaluation`, `PositionActionEvaluation`, `CriterionAssessment`.
* `ModificationRecord`, `ConcernRecord`, `AntiPattern`.
* `Verdict`, `SourceProvenance`, `RecommendationType`, `AdjustmentCategory`.
* `OMSCommand`, `OpenCommand`, `CloseCommand`, `AdjustCommand`, `CancelCommand`, `AddCommand` (from oms_command_models — re-export).
* `envelope_schema()` accessor that returns `PMEnvelope.model_json_schema()`.
* `completion_record_schema()` accessor that returns `PMCompletionRecord.model_json_schema()`.

### 2\. `oms_command_models.py` — five inline minimal models

Author `src/alphamind/decision/portfolio_manager/oms_command_models.py` with five Pydantic models (frozen, discriminated on `command_type`):

* `OpenCommand` — `command_type: Literal["open"]`, `instrument: OMSInstrument`, `position_size: OMSPositionSize` (with `sector: Sector`), enough fields to detect "is this OPEN" structurally. Do NOT model `entry_order` / `target` / `invalidation_legs` / `thesis` — those are full-OMS-model territory; the validator does not read them.
* `CloseCommand` — `command_type: Literal["close"]`, `position_id: str`, `close_rationale_type: Literal["thesis_invalidated", "target_reached", "risk_management", "tactical_exit"]`, `risk_management_subtype: Literal["pm_directed", "engine_guardrail"] | None = None`. Cross-field invariant: `close_rationale_type == "risk_management"` → `risk_management_subtype` is required.
* `AdjustCommand` — `command_type: Literal["adjust"]`, `position_id: str`. No additional fields the validator reads.
* `CancelCommand` — `command_type: Literal["cancel"]`, `order_id: str`, `cancel_reason: str | None = None`.
* `AddCommand` — `command_type: Literal["add"]`, `position_id: str`, `instrument: OMSInstrument`, `position_size: OMSPositionSize`. Same minimal modeling as OpenCommand.
* `OMSCommand` — discriminated union via `Field(discriminator="command_type")` over the five.

**Sub-records:**

* `OMSInstrument` — `asset_type: Literal["equity", "option", "strategy"]`, `direction: Literal["long", "short"]`, `underlying: str` (root ticker). Other instrument fields the validator does not read are omitted.
* `OMSPositionSize` — `sector: Sector` (the risk-side 4-way enum imported from `alphamind.decision.analyst`). Other size fields omitted.

**Module docstring** explicitly notes the file is *transitional* — replaced in a coordinated edit when [ALP-120](<https://linear.app/alphamind-jatassi/issue/ALP-120>) ships full Pydantic OMS command models. Reference parent issue's pre-resolved decision (C).

### 3\. Parity test against the design schema

Author `tests/decision/portfolio_manager/test_models.py` with two test classes:

* `TestPMEnvelopeSchemaParity` — loads `docs/design/04-decision-layer/pm-envelope-schema.md`'s embedded JSON Schema (extract via the same fenced-code-block reader the strategist's parity test uses, or copy that helper), compares against `PMEnvelope.model_json_schema()` with selective normalization (sort key order, normalize `$ref` URIs, etc.). Mirror strategist's parity test exactly.
* `TestVerdictInvariants` — three positive tests (one per verdict) that an envelope with the documented shape constructs successfully; three negative tests asserting `ValidationError` for mis-shaped envelopes (e.g., `verdict: reject` with non-empty `commands`).
* `TestModificationInvariants` — positive: `pre_submission` + `risk_reduction` constructs. Negative: `pre_submission` + `guardrail_rejection_response` (must be `post_rejection`); `post_rejection` + `risk_reduction` (must be `pre_submission`); `guardrail_rejection_response` without `triggering_rule`.
* `TestEnvelopeIdSourceProvenance` — positive: `ENV-REC-1` + `pm_analyst` + `REC-1` constructs; `ENV-SA-3` + `pm_strategist` + `SA-3` + `position_assessment` constructs; `ENV-SA-ORD-7` + `pm_strategist` + `SA-ORD-7` + `pending_order_assessment` constructs. Negative: `ENV-REC-1` + `pm_strategist` rejects.
* `TestPMCompletionRecord` — positive: a minimal record constructs; the verdict-summary sum equals `envelopes_submitted`. Negative: sum mismatch raises.

### 4\. Update `__init__.py`

Author `src/alphamind/decision/portfolio_manager/__init__.py` with the public-surface re-exports listed under "Public surface from `__init__.py`" above. Mirror analyst / strategist `__init__.py` shape.

### Out of scope

* The parser (story 06a), the validator (story 06b), the input-bundle assembler (story 04), the harness (story 07), the runner (story 08).
* The `submit_envelope` MCP wrapper (story 06c), the `get_thesis_components` MCP wrapper (story 05).
* Full OMS command Pydantic models — those are deferred to <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> per parent decision (C).
* Layer-2/3 cross-record validation (envelope_id ↔ source_recommendation_id integer bijection, reference resolution, etc.) — that lives in story 06b's validator. Story 03 enforces only the per-envelope intrinsic invariants Pydantic can see.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/models.py` exists and exports `PMEnvelope`, `PMCompletionRecord`, `VerdictSummary`, all evaluation/modification/concern records, the `Verdict` and `AdjustmentCategory` enums, the `AntiPattern` Literal alias, and `envelope_schema()` + `completion_record_schema()` accessors.
- [ ] `src/alphamind/decision/portfolio_manager/oms_command_models.py` exists with five Pydantic command models discriminated on `command_type`, exporting `OMSCommand` as the union.
- [ ] `src/alphamind/decision/portfolio_manager/__init__.py` exports the public surface.
- [ ] `PMEnvelope.model_json_schema()` matches `docs/design/04-decision-layer/pm-envelope-schema.md`'s embedded JSON Schema byte-equivalently up to `$ref` indirection.
- [ ] All verdict-conditional invariants are enforced by Pydantic validators (test cases pass for valid envelopes, raise `ValidationError` for invalid).
- [ ] All modification-record invariants (`adjustment_category` ↔ `phase`, `triggering_rule` required for `guardrail_rejection_response`) are enforced.
- [ ] `envelope_id` pattern matches `source_provenance` per the schema's regex.
- [ ] `oms_command_models.py`'s module docstring names this as transitional and references [ALP-120](<https://linear.app/alphamind-jatassi/issue/ALP-120>).
- [ ] `tests/decision/portfolio_manager/test_models.py` exercises every invariant with at least one positive and one negative test.
- [ ] `uv run pytest tests/decision/portfolio_manager/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_models.py -n auto` — all parity / invariant tests pass. Run `python -c "from alphamind.decision.portfolio_manager import PMEnvelope; import json; print(json.dumps(PMEnvelope.model_json_schema(), indent=2))" | diff <design-schema-extract> -` and confirm only acceptable differences (key ordering, `$ref` URI normalization).
