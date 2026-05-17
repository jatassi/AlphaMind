# 03 — AnalystOutput typed model + JSON-Schema export

## Goal

Implement the typed in-memory representation of the analyst's output document — the Pydantic model the parser (story 05a) coerces SDK `structured_output` payloads to and the validator (story 05b) consumes. Ships at `src/alphamind/decision/analyst/models.py`. The model's `model_json_schema()` output is fed to the SDK's `output_format={"type": "json_schema", "schema": ...}` mode by the harness (story 07) so the API enforces the shape post-generation.

## Reading

* `docs/design/04-decision-layer/analyst-output-schema.md` — the formal JSON Schema (Draft 2020-12) the model translates. Authoritative.
* `docs/design/04-decision-layer/analyst.md` § Output, § Conviction scale, § Entry window — the human-readable reference for field semantics.
* `src/alphamind/analysis/qualitative_research/models.py` — sibling pattern for Pydantic schema modeling (`QualitativeBrief` is structurally similar in scale).
* `src/alphamind/analysis/adaptive_research/models.py` — second sibling pattern; `AdaptiveBrief` has the multi-thread/array shape closest to analyst's `recommendations[]`.
* `src/alphamind/risk_guardrails/state_delivery/validation_tool.py` § `ValidationResult`, `RuleProjection` — the shape the analyst mirrors into `guardrail_validation_result`. Story 03 should NOT redefine `RuleProjection` or `Greeks`; reference them via import.
* `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` — exports `RuleProjection`, `Greeks` for re-use.
* `docs/design/testing/llm-output-validation.md` § Layer 1 — the parser's contract; story 03 ships the model, story 05a ships the parser.

## Depends on

(none — independent)

## Scope

In scope, all under `src/alphamind/decision/analyst/models.py` plus `src/alphamind/decision/analyst/__init__.py` re-exports. Tests at `tests/decision/analyst/test_models.py`.

### 1\. AnalystOutput root model

```python
class AnalystOutput(BaseModel):
    invocation_id: str
    timestamp: datetime  # tz-aware UTC
    mode: Literal["normal", "watchlist"]
    recommendations: tuple[Recommendation, ...] | None = None
    watchlist: tuple[WatchlistEntry, ...] | None = None
```

Mode-conditional invariant: when `mode == "normal"`, `recommendations` is required (may be empty tuple); `watchlist` is `None`. When `mode == "watchlist"`, `watchlist` is required (may be empty tuple); `recommendations` is `None`. Enforced via `model_validator(mode="after")`.

`timestamp` must be tz-aware (mirror `BriefBundle._validate_bundle`'s tz check).

### 2\. Recommendation model

Match every required and optional field in the schema's `recommendation` $def. Required:

* `recommendation_id: str` — pattern `^REC-[0-9]+$` enforced via `Field(pattern=...)`.
* `instrument: Instrument` (discriminated union — see step 3 below).
* `underlying: str`.
* `sector: Literal["tech", "semis", "financials", "energy"]` (risk-side 4-way taxonomy).
* `conviction_level: int` constrained `1 ≤ x ≤ 5` (`Field(ge=1, le=5)`).
* `entry_order: EntryOrder`.
* `position_size: PositionSize`.
* `target: Target`.
* `invalidation_legs: tuple[InvalidationLeg, ...]` (min length 1).
* `time_expectation_hours: float` constrained `0 < x ≤ 72` (`Field(gt=0, le=72)`).
* `guardrail_validation_result: GuardrailValidationResult`.
* `thesis_narrative: str`.
* `target_rationale: str`.
* `invalidation_rationale: tuple[InvalidationRationale, ...]` (min length 1).
* `position_size_rationale: str`.
* `counterarguments_acknowledged: str`.

Optional:

* `entry_window: EntryWindow | None = None`.
* `entry_window_rationale: str | None = None`.

Conditional invariant: `entry_window` and `entry_window_rationale` are paired — both present or both absent. Enforced via `model_validator(mode="after")`.

### 3\. Instrument variants (discriminated union)

`Instrument = Annotated[InstrumentEquity | InstrumentOption | InstrumentStrategy, Discriminator("asset_type")]`. Each variant's `asset_type` is a `Literal[...]` discriminator value matching the schema's `const` constraint. Variants:

* `InstrumentEquity`: `asset_type: Literal["equity"]`, `ticker: str`, `direction: Literal["long", "short"]`.
* `InstrumentOption`: `asset_type: Literal["option"]`, `underlying: str`, `strike: float` (gt 0), `expiration: date`, `contract_type: Literal["call", "put"]`, `direction: Literal["long", "short"]`.
* `InstrumentStrategy`: `asset_type: Literal["strategy"]`, `strategy_type: Literal["vertical_spread", "calendar_spread", "straddle", "strangle", "iron_condor", "custom"]`, `underlying: str`, `legs: tuple[StrategyLeg, ...]` (min length 2).

`StrategyLeg`: `strike: float (gt 0)`, `expiration: date`, `contract_type: Literal["call", "put"]`, `direction: Literal["long", "short"]`, `quantity_ratio: int (ge 1)`.

### 4\. EntryOrder, PositionSize, Target

* `EntryOrder`: `type: Literal["market", "limit", "stop_limit"]`; conditional: `limit_price` required when `type ∈ {limit, stop_limit}`; `stop_price` required when `type == "stop_limit"`. Both `gt 0` when present.
* `PositionSize`: `quantity: float (gt 0)`, `dollar_value: float (gt 0)`, `pct_of_portfolio: float (gt 0)`, optional `premium_at_risk: float (gt 0)`, optional `delta_adjusted_exposure: float` (LLM-mirrored from validation tool; can be negative for shorts).
* `Target`: `target_type: Literal["absolute_price", "pl_percentage", "pl_dollar"]`, `price: float (gt 0)`, `dollar_pl_target: float`; conditional: `pl_percentage: float` required when `target_type == "pl_percentage"`; `pl_dollar: float` required when `target_type == "pl_dollar"`.

### 5\. InvalidationLeg + InvalidationCondition variants

`InvalidationLeg` carries `leg_id: str` (pattern `^INV-[0-9]+$`), `type: Literal["price", "time", "event"]`, `is_hard: bool`, `condition: PriceCondition | TimeCondition | EventCondition`, optional `order_parameters: OrderParameters | None = None`.

Conditional invariants per the schema's `allOf`:

* `type == "price"` → `is_hard == True`, `order_parameters` required, `condition` matches PriceCondition shape.
* `type == "time"` → `is_hard == True`, `order_parameters` required, `condition` matches TimeCondition shape.
* `type == "event"` → `is_hard == False`, `order_parameters` omitted, `condition` matches EventCondition shape.

Enforced via `model_validator(mode="after")` raising structured ValueError.

* `PriceCondition`: `underlying_trigger: str`, `comparator: Literal["<=", ">=", "<", ">"]`, `trigger_price: float (gt 0)`.
* `TimeCondition`: `deadline: datetime` (tz-aware).
* `EventCondition`: `event_description: str` (min length 1).
* `OrderParameters`: `order_type: Literal["market", "limit", "stop", "stop_limit"]`, optional `limit_price: float (gt 0)`.

### 6\. EntryWindow

`EntryWindow`: `deadline: datetime` (tz-aware), `decay_type: Literal["binary", "gradual"]`, `rationale: str` (min length 1).

### 7\. InvalidationRationale

`InvalidationRationale`: `leg_id: str`, `rationale: str` (min length 1). The cross-field check that every `leg_id` resolves to an existing `invalidation_legs[].leg_id` lives in the validator (story 05b), not the model — Pydantic can't see across siblings.

### 8\. GuardrailValidationResult

Mirror the validation tool's `ValidationResult` shape — the analyst copies the JSON verbatim. Reuse `RuleProjection` from `alphamind.risk_guardrails.guardrail_evaluation` and `Greeks` from the same module rather than redefining.

`GuardrailValidationResult`: `overall: Literal["PASS", "FAIL"]`, `per_rule: tuple[RuleProjection, ...]`, optional `delta_adjusted_exposure: float | None = None`, optional `greeks: Greeks | None = None`, optional `cumulative_impact_note: str | None = None`, `checked_at: datetime` (tz-aware).

### 9\. WatchlistEntry

`WatchlistEntry`: `ticker: str`, `sector: Literal["tech", "semis", "financials", "energy"]`, `thesis_summary: str` (min length 1), `estimated_conviction: int (ge 1, le 5)`, optional `source_references: tuple[str, ...] | None = None`.

### 10\. Public surface and JSON-Schema export

`__all__` exports every model name. Confirm `AnalystOutput.model_json_schema()` produces a dict that the SDK's JSON-Schema mode accepts (no top-level `oneOf`/`allOf` Anthropic rejects; mirror the qualitative-researcher's adjustment if Pydantic generates an unaccepted shape — see comment block on `qualitative_research/harness.py` `_build_sdk_options`). Export the model names from `src/alphamind/decision/analyst/__init__.py`.

### Out of scope

* The parser (story 05a owns coercion of SDK payload → AnalystOutput).
* The Layer-2/3 validator (story 05b owns cross-field invariants).
* Writing the JSON Schema file to disk (Pydantic generates at runtime; not pre-rendered).

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/models.py` exists with `AnalystOutput`, `Recommendation`, `WatchlistEntry`, all instrument variants, all condition variants, `EntryOrder`, `PositionSize`, `Target`, `InvalidationLeg`, `InvalidationRationale`, `EntryWindow`, `GuardrailValidationResult`.
- [ ] Every field in the JSON Schema's `recommendation` $def has a typed counterpart with the right name, type, and constraint.
- [ ] Every Pydantic `Literal[...]` enum matches the JSON Schema's enum values verbatim (case-sensitive).
- [ ] Every conditional invariant in the JSON Schema's `allOf` blocks (`entry_window ↔ entry_window_rationale`, `mode ↔ recommendations/watchlist`, invalidation `type ↔ is_hard ↔ order_parameters`, `entry_order.type ↔ limit_price/stop_price`, `target.target_type ↔ pl_percentage/pl_dollar`) is enforced via a `model_validator(mode="after")` raising clear errors.
- [ ] `AnalystOutput.model_json_schema()` returns a valid Draft 2020-12 schema dict.
- [ ] `RuleProjection` and `Greeks` are imported from `alphamind.risk_guardrails.guardrail_evaluation` rather than redefined.
- [ ] `tests/decision/analyst/test_models.py` exists and covers: round-trip for the analyst.md `<example_output>` example + parametric tests for each conditional invariant (positive case + negative case).
- [ ] `src/alphamind/decision/analyst/__init__.py` re-exports the model names.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_models.py -n auto` passes. Manually call `AnalystOutput.model_json_schema()` and visually compare the output to `docs/design/04-decision-layer/analyst-output-schema.md` — the field set should be 1:1 (modulo Pydantic's idiomatic discriminator structure for the discriminated unions).