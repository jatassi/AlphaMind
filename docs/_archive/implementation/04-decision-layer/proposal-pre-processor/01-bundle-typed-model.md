# 01 — Bundle typed model

## Goal

Ship the Pydantic models mirroring `proposal-pre-processor-bundle-schema.md` plus the canonical `BUNDLE_OUTPUT_SCHEMA` export. The schema doc is the authoritative contract; this story produces a Python type system that round-trips against it. Inner records (`Recommendation`, `WatchlistEntry`, `PositionAssessment`, `PendingOrderAssessment`, `PortfolioLevelObservations`) are imported from `alphamind.decision.{analyst, strategist}` and held verbatim inside wrapper envelopes — never redefined. Downstream stories (02b, 02c, 03, 04) build the bundle by composing these models; <issue id="5e187e5c-03a3-4d2b-9b33-d94c44c793af">ALP-310</issue> imports `BUNDLE_OUTPUT_SCHEMA` for PM-side validation.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` § Schema — every `$defs` definition, every `required` list, every enum, every regex pattern. The reference implementation.
* `docs/design/04-decision-layer/proposal-pre-processor.md` § Output, § §1, §2, §3 — prose framing for why each field is shaped the way it is; conflict-type enum semantics; mode-conditional structure for analyst.watchlist and strategist.defensive_posture.
* `src/alphamind/decision/analyst/models.py` — the imported inner records (`Recommendation`, `WatchlistEntry`, `Sector`, `GuardrailValidationResult`, `RuleProjection`).
* `src/alphamind/decision/strategist/models.py` — the imported inner records (`PositionAssessment`, `PendingOrderAssessment`, `PortfolioLevelObservations`, `Sector`, `ThesisStatus`).
* `src/alphamind/decision/analyst/__init__.py` and `src/alphamind/decision/strategist/__init__.py` — public re-export pattern; `models.py`'s public surface goes here.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `RuleProjection`, `Greeks` shapes referenced by §1.A `per_rule_entry` and the inner `guardrail_validation_result` records.

## Depends on

None — wave 1.

## Scope

In scope, all under `src/alphamind/decision/proposal_pre_processor/`. Tests at `tests/decision/proposal_pre_processor/test_models.py`.

### 1\. Top-level envelope and §1 nested models

Create `src/alphamind/decision/proposal_pre_processor/models.py`. Define Pydantic models with `model_config = ConfigDict(frozen=True, extra="forbid")`:

* `ProposalPreProcessorBundle` — `invocation_id: str`, `timestamp: datetime`, `aggregate_observations: AggregateObservations`, `strategist_section: StrategistSection`, `analyst_section: AnalystSection`.
* `AggregateObservations` — `combined_set_impact: CombinedSetImpact`, `conviction_distribution: ConvictionDistribution`, `book_health_summary: BookHealthSummary`.
* `CombinedSetImpact` — `basis: BasisSection`, `per_rule: tuple[PerRuleEntry, ...]`, `breaches: tuple[BreachEntry, ...]`.
* `BasisSection` — `analyst_proposal_ids: tuple[str, ...]` (each element matching `^REC-[0-9]+$`), `strategist_action_ids: tuple[str, ...]` (each matching `^SA-[0-9]+$`), `strategist_holds_excluded_count: int` (≥ 0), `snapshot_timestamp: datetime`.
* `PerRuleEntry` — `rule: str`, `status: Literal["PASS", "WARNING", "FAIL"]`, `current: float`, `limit: float`, `projected_after: float`, `headroom_remaining: float`, `unit: str`.
* `BreachEntry` — `rule: str`, `overage: float`, `unit: str`, `contributors: tuple[ContributorEntry, ...]`.
* `ContributorEntry` — `proposal_id: str` (matching `^(REC|SA)-[0-9]+$`), `contribution: float`.

### 2\. Histogram models

The schema's histogram dicts (`by_level`, `by_thesis_status`, `by_recommended_action`) require non-Python-identifier keys (`"1"`, `"on-track"`, `"adjust-bracket"`). Use Pydantic `Field(alias=...)` with `model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)` so each field can be set by alias and serializes by alias.

* `ConvictionHistogram` — five `int` fields aliased `"1"` through `"5"` (each ≥ 0).
* `ConvictionDistribution` — `by_level: ConvictionHistogram`, `total: int` (≥ 0).
* `ByThesisStatus` — five `int` fields aliased `"on-track"`, `"partially-realized"`, `"at-risk"`, `"stale"`, `"invalidated"` (each ≥ 0).
* `ByRecommendedAction` — five `int` fields aliased `"hold"`, `"reduce"`, `"close"`, `"adjust-bracket"`, `"add"` (each ≥ 0).
* `BookHealthSummary` — `by_thesis_status: ByThesisStatus`, `by_recommended_action: ByRecommendedAction`, `remedy_flagged_count: int` (≥ 0), `total: int` (≥ 0).

### 3\. Strategist section + wrappers

* `StrategistSection` — `mode: Literal["normal", "defensive_posture"]`, `position_assessments: tuple[WrappedPositionAssessment, ...]`, `pending_order_assessments: tuple[WrappedPendingOrderAssessment, ...]`, `portfolio_level_observations: PortfolioLevelObservations` (imported from strategist).
* `WrappedPositionAssessment` — `assessment: PositionAssessment` (imported), `pre_processor_annotations: StrategistSideAnnotations`.
* `WrappedPendingOrderAssessment` — `pending_order_assessment: PendingOrderAssessment` (imported), `pre_processor_annotations: StrategistSideAnnotations`.
* `StrategistSideAnnotations` — `conflicts: tuple[StrategistSideConflict, ...]`.
* `StrategistSideConflict` — `with_recommendation_id: str` (matching `^REC-[0-9]+$`), `underlying: str`, `conflict_type: ConflictType`.

### 4\. Analyst section + mode-dispatch + analyst-side conflicts

* `AnalystSection` — `mode: Literal["normal", "watchlist"]`, `recommendations: tuple[WrappedRecommendation, ...] | None = None`, `watchlist: tuple[WatchlistEntry, ...] | None = None`. Add a `model_validator(mode="after")` that enforces:
  * `mode == "normal"` ⇒ `recommendations is not None` and `watchlist is None`.
  * `mode == "watchlist"` ⇒ `watchlist is not None` and `recommendations is None`.
* `WrappedRecommendation` — `recommendation: Recommendation` (imported), `pre_processor_annotations: AnalystSideAnnotations`.
* `AnalystSideAnnotations` — `conflicts: tuple[AnalystSideConflict, ...]`.
* `AnalystSideConflict` — `with_assessment_id: str | None = None` (matching `^SA-[0-9]+$` when populated), `with_pending_order_assessment_id: str | None = None` (matching `^SA-ORD-[0-9]+$` when populated), `underlying: str`, `conflict_type: ConflictType`. Add a `model_validator(mode="after")` enforcing exactly one of the two cross-reference fields is populated (the JSON Schema's `oneOf`).
* `ConflictType(StrEnum)` — values `entry_vs_close`, `entry_vs_add`, `entry_vs_hold`, `entry_direction_conflict`, `entry_vs_pending_maintain`, `entry_vs_pending_modify`, `entry_vs_pending_cancel`. Member names should equal their value strings.

### 5\. Schema export

At module level in `models.py`:

```python
BUNDLE_OUTPUT_SCHEMA: dict[str, Any] = ProposalPreProcessorBundle.model_json_schema()


def bundle_schema() -> dict[str, Any]:
    """Return the JSON Schema (Draft 2020-12) for ProposalPreProcessorBundle."""
    return ProposalPreProcessorBundle.model_json_schema()
```

### 6\. Public re-exports

Update `src/alphamind/decision/proposal_pre_processor/__init__.py` to export every model name introduced here plus `BUNDLE_OUTPUT_SCHEMA` and `bundle_schema`. Mirror the analyst/strategist pattern. Stories 02–05 will append their additions to the same `__init__.py`; this story establishes the file.

### Out of scope

* Translation logic (story 02a).
* Aggregate-observations computation (stories 02b, 03).
* Conflict-detection logic (story 02c).
* Bundle assembly (story 04).
* Verify script (story 05).

## Acceptance criteria

- [ ] `ProposalPreProcessorBundle.model_json_schema()` produces a JSON object whose top-level `required` matches the design doc's top-level required list (`invocation_id`, `timestamp`, `aggregate_observations`, `strategist_section`, `analyst_section`).
- [ ] A schema-parity test in `test_models.py` walks each `$defs` in the design doc's schema block and asserts the corresponding Pydantic model agrees on `required`, enum values, regex patterns, and `additionalProperties: false`. The test loads the design doc's schema by parsing the markdown JSON code block.
- [ ] `AnalystSection(mode="normal", recommendations=(), watchlist=None)` constructs successfully; `AnalystSection(mode="normal", watchlist=(), recommendations=None)` raises `ValidationError`.
- [ ] `AnalystSection(mode="watchlist", watchlist=(), recommendations=None)` constructs; `AnalystSection(mode="watchlist", recommendations=(), watchlist=None)` raises.
- [ ] `AnalystSideConflict(with_assessment_id="SA-1", with_pending_order_assessment_id="SA-ORD-1", underlying="NVDA", conflict_type="entry_vs_close")` raises `ValidationError` (both populated).
- [ ] `AnalystSideConflict(underlying="NVDA", conflict_type="entry_vs_close")` raises `ValidationError` (neither populated).
- [ ] `AnalystSideConflict(with_assessment_id="SA-1", underlying="NVDA", conflict_type="entry_vs_close")` constructs.
- [ ] `BUNDLE_OUTPUT_SCHEMA` is importable from `alphamind.decision.proposal_pre_processor` (the package `__init__.py` re-exports it).
- [ ] `WrappedPositionAssessment(assessment=<PositionAssessment instance>, pre_processor_annotations=...)` accepts the imported `PositionAssessment` Pydantic instance unchanged; `model_dump()` round-trips the inner record without reformatting.
- [ ] `ConvictionHistogram.model_validate({"1": 0, "2": 1, "3": 3, "4": 1, "5": 0})` constructs with the alias-based keys; `model_dump(by_alias=True)` returns the same dict shape.
- [ ] `ByRecommendedAction.model_validate({"hold": 7, "reduce": 1, "close": 1, "adjust-bracket": 1, "add": 0})` constructs and round-trips.
- [ ] `ConflictType.entry_vs_pending_modify.value == "entry_vs_pending_modify"`.
- [ ] `tests/decision/proposal_pre_processor/test_models.py` exists and passes under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_models.py -n auto`. Inspect the schema-parity test report — every `$defs` definition in the design doc must have a matching Pydantic model on `required`, enums, regex, and `additionalProperties: false`. Spot-check by importing `BUNDLE_OUTPUT_SCHEMA` from `alphamind.decision.proposal_pre_processor` in a Python REPL and confirming the JSON has `"$defs"` with all expected keys.
