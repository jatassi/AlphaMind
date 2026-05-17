# 03 — StrategistOutput typed model + JSON-Schema export

## Goal

Implement the typed in-memory representation of the strategist's output document — the Pydantic model the parser (story 05a) coerces SDK `structured_output` payloads to and the validator (story 05b) consumes. Ships at `src/alphamind/decision/strategist/models.py`. The model's `model_json_schema()` output is fed to the SDK's `output_format={"type": "json_schema", "schema": ...}` mode by the harness (story 06) so the API enforces shape post-generation.

## Reading

* `docs/design/04-decision-layer/strategist-output-schema.md` — formal JSON Schema (Draft 2020-12), authoritative contract. The Pydantic translation must round-trip: `model_json_schema()` produces a schema equivalent to the document's schema (subject to Pydantic's normal export quirks).
* `docs/design/04-decision-layer/strategist.md` — output structure prose, used to disambiguate field semantics where the schema is terse.
* `src/alphamind/decision/analyst/models.py` — sibling pattern (`AnalystOutput` with `recommendations[]` and `watchlist[]` mode-dispatched). Mirror its module structure (frozen models, `model_config = ConfigDict(frozen=True, extra="forbid")`, discriminated union pattern for `instrument`).
* `src/alphamind/portfolio_state/records/theses.py` — existing `ThesisStatus` enum vocabulary (`on-track`, `partially-realized`, `at-risk`, `stale`, `invalidated`) — the `thesis_status` and `prior_status` fields must use the SAME enum vocabulary; if a `ThesisStatus` enum already exists there, import it; otherwise define a `ThesisStatus` Literal that matches.
* `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` — `RuleProjection`, `Greeks`. Story 03's `GuardrailValidationResult` model imports rather than redefines these.
* `tests/decision/analyst/test_models.py` — sibling test pattern.

## Depends on

* None (Wave 1 of work tree).

## Scope

In scope, all under `src/alphamind/decision/strategist/`. Tests at `tests/decision/strategist/test_models.py`.

### 1\. `StrategistOutput` top-level model

```python
class StrategistOutput(BaseModel):
    invocation_id: str
    timestamp: datetime
    mode: Literal["normal", "defensive_posture"]
    position_assessments: tuple[PositionAssessment, ...]
    pending_order_assessments: tuple[PendingOrderAssessment, ...]
    portfolio_level_observations: PortfolioLevelObservations
```

* `model_config = ConfigDict(frozen=True, extra="forbid")`.
* `model_json_schema()` produces the Draft-2020-12 schema the harness feeds to the SDK.
* Mode-conditional restrictions are encoded structurally where possible (Pydantic doesn't natively express the `defensive_posture → action ∈ {hold, reduce, close, adjust-bracket}` invariant; that goes in the validator (story 05b) and as a Pydantic `model_validator` here when straightforward).

### 2\. `PositionAssessment` model

Required fields per schema:

* `assessment_id: str` with pattern `^SA-[0-9]+$`.
* `position_id: str`, `thesis_id: str`, `underlying: str`.
* `sector: Literal["tech", "semis", "financials", "energy"]`.
* `thesis_status: ThesisStatus` (enum or Literal matching the canonical five values).
* `prior_status: ThesisStatus | None` — null on first invocation after position entry.
* `recommended_action: Literal["hold", "reduce", "close", "adjust-bracket", "add"]`.
* `action_parameters: ActionParameters | None` (None when action is `hold`; required for non-hold per schema's allOf).
* `exposure_impact: ExposureImpact | None` (required for non-hold per schema).
* `guardrail_validation_result: GuardrailValidationResult | None` (required for `add`; optional for close/reduce interacting with constraints).
* `remedy_flag: str | None` — breach identifier (e.g., `"BREACH-1"`).
* `status_rationale: str`, `action_rationale: str` (always required).
* `reduce_rationale: str | None` (required when action is `reduce`).
* `add_conviction_justification: str | None` (required when action is `add`).
* `adjustment_rationale: str | None` (required when action is `adjust-bracket`).
* `remedy_rationale: str | None` (required when `remedy_flag` is present).
* `cross_position_observations: str | None`.

Encode the conditional invariants via Pydantic `model_validator(mode="after")` where straightforward: action↔parameters action match, `invalidated → recommended_action == "close"`, action-specific rationale presence. The validator (story 05b) handles cross-document invariants Pydantic can't see across siblings.

### 3\. `ActionParameters` discriminated union

Mirror the schema's oneOf:

* `CloseParameters` (`action: "close"`, `quantity: float | Literal["all"]`, `order_type: Literal["market", "limit"]`, optional `limit_price`, `close_rationale_type: Literal["thesis_invalidated", "target_reached", "conviction_reduced", "risk_management"]`).
* `ReduceParameters` (`action: "reduce"`, `quantity: float`, `order_type`, optional `limit_price`).
* `AdjustBracketParameters` (`action: "adjust-bracket"`, optional `new_stop_level`, `new_target_level`, `new_time_expiration`, `new_event_invalidation`, `thesis_component_updates`; at least one must be present per schema's anyOf).
* `AddParameters` (`action: "add"`, `additional_quantity`, `additional_dollar_value`, `entry_order`, optional `bracket_adjustment`).

Use `Annotated[Union[...], Field(discriminator="action")]` so Pydantic can route by the `action` literal. Each variant's nested fields are typed sub-models (e.g., `EntryOrder`, `NewStopLevel`, `ThesisComponentUpdate`) — mirror the analyst pattern.

`close_parameters quantity: "all"` is forbidden when `close_rationale_type == "conviction_reduced"` (schema rule); encode via model_validator on `CloseParameters`.

### 4\. `ExposureImpact` and `GuardrailValidationResult` models

* `ExposureImpact`: `sector_delta_adjusted_change: float`, `net_directional_impact: float`.
* `GuardrailValidationResult`: `overall: Literal["PASS", "FAIL"]`, `per_rule: tuple[GuardrailValidationPerRule, ...]`, optional `delta_adjusted_exposure: float`, optional `greeks: Greeks` (imported from `alphamind.risk_guardrails.guardrail_evaluation`), optional `cumulative_impact_note: str`, `checked_at: datetime`.
* `GuardrailValidationPerRule`: `rule, status, current, limit, projected_after, headroom_remaining, unit` per schema.

### 5\. `PendingOrderAssessment` model

Required fields:

* `pending_order_assessment_id: str` with pattern `^SA-ORD-[0-9]+$`.
* `order_id, position_id, order_type, order_age_hours, fill_probability_assessment, recommended_action, drift_rationale, action_rationale` per schema.
* `order_type: Literal["entry_limit", "entry_stop_limit", "bracket_target", "bracket_price_stop", "bracket_time_stop", "bracket_event_stop"]`.
* `fill_probability_assessment: Literal["likely_soon", "plausible", "unlikely"]`.
* `recommended_action: Literal["maintain", "modify", "cancel"]`.
* `current_distance_pct: float | None` (omitted for time/event orders).
* `modification_parameters: ModificationParameters | None` — required when action is `modify`; encode via model_validator. At least one of `new_limit_price`, `new_trigger_price`, `new_deadline`, `new_order_type` must be present.
* `linked_position_assessment_id: str | None` matching `^SA-[0-9]+$`.

### 6\. `PortfolioLevelObservations` model

Required fields per schema:

* `aggregate_thesis_health: str`, `sector_balance_shifts: str`, `thesis_dependency_warnings: str`, `capital_allocation_observations: str`.
* `regime_transition_summary: RegimeTransitionSummary | None` (present when breaches were flagged in input).
* `defensive_posture_summary: DefensivePostureSummary | None` (required when mode is `defensive_posture`; encode via model_validator at `StrategistOutput` level).

### 7\. `__init__.py` exports

Update `src/alphamind/decision/strategist/__init__.py` to re-export the public surface: `StrategistOutput`, `PositionAssessment`, `PendingOrderAssessment`, `PortfolioLevelObservations`, `ActionParameters`, `ExposureImpact`, `GuardrailValidationResult`, `RegimeTransitionSummary`, `DefensivePostureSummary`, `ThesisStatus`. Mirror analyst's `__init__.py` shape.

### Out of scope

* Parser logic (deserialization from SDK output) — story 05a.
* Cross-document validation (referential checks against retrieval store, active_sectors filtering) — story 05b.
* Input bundle assembly — story 04.
* Harness, runner, e2e — stories 06, 07, 08.

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/models.py` exports `StrategistOutput` and all sub-models above.
- [ ] `StrategistOutput.model_json_schema()` produces a schema that validates the example JSON in `docs/design/04-decision-layer/strategist-output-schema.md` (Draft 2020-12).
- [ ] Action↔parameters action-literal match enforced via Pydantic discriminator on `ActionParameters`.
- [ ] `invalidated → recommended_action == "close"` enforced via `model_validator` on `PositionAssessment`.
- [ ] `mode == "defensive_posture" → recommended_action ∈ {"hold", "reduce", "close", "adjust-bracket"}` enforced via `model_validator` on `StrategistOutput`.
- [ ] `mode == "defensive_posture" → portfolio_level_observations.defensive_posture_summary is not None` enforced.
- [ ] `recommended_action == "modify" → modification_parameters is not None` enforced on `PendingOrderAssessment`.
- [ ] `close_parameters quantity != "all"` when `close_rationale_type == "conviction_reduced"` enforced on `CloseParameters`.
- [ ] `tests/decision/strategist/test_models.py` covers: schema export shape, valid roundtrip of the strategist-prompt's example_output JSON, discriminated-union routing for each action, conditional invariants raise `ValidationError` when violated.
- [ ] All models are frozen (`ConfigDict(frozen=True, extra="forbid")`).
- [ ] `uv run pytest tests/decision/strategist/test_models.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

```bash
uv run pytest tests/decision/strategist/test_models.py -n auto
```

Inspect the JSON-Schema export by running a one-off Python invocation:

```python
from alphamind.decision.strategist import StrategistOutput
import json
print(json.dumps(StrategistOutput.model_json_schema(), indent=2))
```

The output should be Draft-2020-12-compatible and structurally equivalent to the schema in the design doc.
