# 04 — Bundle assembler and public runner

## Goal

Ship `run_proposal_pre_processor(...)` — the public entry point and bundle assembler for this work tree. Composes the §1 observations (from stories 02b and 03), the §2/§3 wrap orchestration with conflict annotations (from story 02c), and the mode-conditional shape (analyst.normal vs. watchlist; strategist.normal vs. defensive_posture) into a single `ProposalPreProcessorBundle`. Preserves the analyst's and strategist's emitted presentation order. The runner is what `ALP-310`'s decision-layer pipeline composition will call directly; story 05's verify script also calls it.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor.md` — entire document. Story 04 is where the design's three-section bundle assembly is realized.
* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` § Notes on cross-field invariants — the runner must satisfy these (mode consistency with halt state, conflict cross-references resolve in-bundle, mirror-symmetry of conflicts, basis ID consistency, total-equals-sum identities, etc.).
* `docs/design/04-decision-layer/strategist.md` § Presentation order and token budget — strategist's emitted order is preserved verbatim into §2.
* `docs/design/04-decision-layer/analyst.md` § Presentation order — analyst's emitted order is preserved verbatim into §3.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — typed bundle envelope and wrappers; the runner builds these.
* <issue id="402fc84a-b75f-4d26-8af6-175f21b788a5">ALP-314</issue> (this work tree, story 02a) — `translate_*` functions called transitively via story 03.
* <issue id="1dd96d68-d2d5-4044-81a2-b748ac764816">ALP-315</issue> (this work tree, story 02b) — `compute_conviction_distribution`, `compute_book_health_summary`.
* <issue id="3b982e5a-7b1d-4233-aecc-8b286a58d0ba">ALP-316</issue> (this work tree, story 02c) — `detect_conflicts`, `ConflictDetectionResult`.
* <issue id="63970f52-e681-419c-bc25-f5fd280597f9">ALP-317</issue> (this work tree, story 03) — `compute_combined_set_impact`.
* `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` — `LibraryConfig`, `MarketInputs`, `PortfolioStateSnapshot` types passed through to story 03.
* `src/alphamind/decision/{analyst, strategist}/__init__.py` — `AnalystOutput`, `StrategistOutput` input shapes.

## Depends on

* <issue id="1dd96d68-d2d5-4044-81a2-b748ac764816">ALP-315</issue> (this work tree, story 02b) — histogram computations.
* <issue id="3b982e5a-7b1d-4233-aecc-8b286a58d0ba">ALP-316</issue> (this work tree, story 02c) — conflict-detection result.
* <issue id="63970f52-e681-419c-bc25-f5fd280597f9">ALP-317</issue> (this work tree, story 03) — combined-set impact.

## Scope

In scope, two modules under `src/alphamind/decision/proposal_pre_processor/`:

* `assembler.py` — internal helpers for bundle assembly (mode dispatch, wrap construction, presentation-order preservation).
* `runner.py` — the public entry point `run_proposal_pre_processor`.

Tests at `tests/decision/proposal_pre_processor/test_assembler.py` and `tests/decision/proposal_pre_processor/test_runner.py`.

### 1\. Public runner signature

```python
def run_proposal_pre_processor(
    *,
    analyst_output: AnalystOutput,
    strategist_output: StrategistOutput,
    snapshot: PortfolioStateSnapshot,
    library_config: LibraryConfig,
    market: MarketInputs,
    snapshot_timestamp: datetime,
    timestamp: datetime,
) -> ProposalPreProcessorBundle:
    """Compute the proposal pre-processor bundle.

    Inputs are the persisted analyst and strategist outputs (after their own
    Layer-2/3 validation), the Phase-1 portfolio snapshot in library shape,
    the library config + market inputs that the agent-side validation tool
    used, the snapshot's effective timestamp (for §1.A basis), and the
    bundle's finalization timestamp (for the bundle envelope's `timestamp`
    field).

    Pure: same inputs produce identical bundle output. No clock reads, no
    UUID generation, no I/O.
    """
```

### 2\. Halt-state cross-check

Before any computation, verify mode consistency:

* `analyst_output.mode == "watchlist" ⇔ strategist_output.mode == "defensive_posture"`. The schema's halt-state invariant requires both halves to agree.
* If they disagree, raise `BundleAssemblyError("halt-state inconsistency: analyst.mode=<X>, strategist.mode=<Y>")`. Per parent surfacing condition, this should never occur from a valid pipeline; surface as fixture bug.

### 3\. Build §1 (aggregate observations)

* Compute `conviction_distribution` via `compute_conviction_distribution(<recommendations from analyst_output>)`. In watchlist mode, the recommendation list is empty (None on the typed model) — pass an empty tuple; the function handles it correctly.
* Compute `book_health_summary` via `compute_book_health_summary(<position_assessments from strategist_output>)`.
* Filter `non_hold_position_assessments = tuple(a for a in strategist_output.position_assessments if a.recommended_action != "hold")`. `strategist_holds_excluded_count = len(strategist_output.position_assessments) - len(non_hold_position_assessments)`.
* In watchlist mode, the analyst's recommendations are empty; pass `recommendations=()` to `compute_combined_set_impact`. Strategist actions still flow normally (defensive_posture allows reduce/close/adjust-bracket).
* Compute `combined_set_impact` via `compute_combined_set_impact(...)`.

### 4\. Build the conflict matrix

* Construct the `held_direction_resolver` from the snapshot:

  ```python
  def _held_direction_resolver(position_id: str) -> Literal["long", "short"]:
      pos = snapshot.existing_positions[position_id]
      return "long" if pos.direction is Direction.LONG else "short"
  ```
* In watchlist mode, conflict detection is skipped — pass empty inputs to `detect_conflicts`. The result has empty conflict tuples for every strategist record (per design doc: "conflict detection does not apply" in watchlist mode); strategist-side annotations on every wrapped record carry `conflicts = ()`.
* Otherwise: call `detect_conflicts(recommendations=<analyst.recommendations>, position_assessments=<strategist.position_assessments>, pending_order_assessments=<strategist.pending_order_assessments>, held_direction_resolver=_held_direction_resolver)`.

### 5\. Assemble §2 (strategist section)

For each `assessment` in `strategist_output.position_assessments` (preserving order):

```python
WrappedPositionAssessment(
    assessment=assessment,
    pre_processor_annotations=StrategistSideAnnotations(
        conflicts=conflict_result.strategist_position_conflicts_by_assessment_id.get(
            assessment.assessment_id, ()
        ),
    ),
)
```

For each `pending` in `strategist_output.pending_order_assessments` (preserving order):

```python
WrappedPendingOrderAssessment(
    pending_order_assessment=pending,
    pre_processor_annotations=StrategistSideAnnotations(
        conflicts=conflict_result.strategist_pending_conflicts_by_assessment_id.get(
            pending.pending_order_assessment_id, ()
        ),
    ),
)
```

`strategist_section.mode` mirrors `strategist_output.mode`. `portfolio_level_observations` passes through verbatim from `strategist_output.portfolio_level_observations`.

### 6\. Assemble §3 (analyst section)

In normal mode (`analyst_output.mode == "normal"`):

For each `recommendation` in `analyst_output.recommendations` (preserving order):

```python
WrappedRecommendation(
    recommendation=recommendation,
    pre_processor_annotations=AnalystSideAnnotations(
        conflicts=conflict_result.analyst_conflicts_by_rec_id.get(
            recommendation.recommendation_id, ()
        ),
    ),
)
```

`analyst_section.mode = "normal"`, `recommendations = <tuple>`, `watchlist = None`.

In watchlist mode (`analyst_output.mode == "watchlist"`):

`analyst_section.mode = "watchlist"`, `watchlist = analyst_output.watchlist`, `recommendations = None`. Watchlist entries pass through verbatim (no wrap, no annotations).

### 7\. Final bundle

```python
return ProposalPreProcessorBundle(
    invocation_id=analyst_output.invocation_id,  # asserts ==strategist_output.invocation_id
    timestamp=timestamp,
    aggregate_observations=AggregateObservations(
        combined_set_impact=combined_set_impact,
        conviction_distribution=conviction_distribution,
        book_health_summary=book_health_summary,
    ),
    strategist_section=strategist_section,
    analyst_section=analyst_section,
)
```

Add an invocation-id consistency check before construction: if `analyst_output.invocation_id != strategist_output.invocation_id`, raise `BundleAssemblyError("invocation_id mismatch: analyst=<X>, strategist=<Y>")`.

### 8\. Public re-exports

Add `run_proposal_pre_processor` and `BundleAssemblyError` to `src/alphamind/decision/proposal_pre_processor/__init__.py`. After this story, `__init__.py` exports the full public surface (models, translators, observation functions, conflict detection, runner).

### Out of scope

* Live SDK integration (this work tree is fixture-based; live wiring is <issue id="5e187e5c-03a3-4d2b-9b33-d94c44c793af">ALP-310</issue>).
* Verify script (story 05).
* Translation, histogram, conflict, or library logic — all in dependency stories.

## Acceptance criteria

- [ ] `run_proposal_pre_processor` constructed with valid normal-mode inputs returns a `ProposalPreProcessorBundle` whose `aggregate_observations.combined_set_impact.basis.analyst_proposal_ids` matches the analyst output's recommendation IDs in order, and whose `strategist_action_ids` matches the strategist output's non-hold assessment IDs in order.
- [ ] In watchlist mode (analyst.mode="watchlist", strategist.mode="defensive_posture"), the returned bundle has `analyst_section.mode="watchlist"`, `analyst_section.watchlist` populated from `analyst_output.watchlist`, `analyst_section.recommendations is None`, `strategist_section.mode="defensive_posture"`, and `combined_set_impact.basis.analyst_proposal_ids = ()`.
- [ ] Disagreeing modes (analyst.mode="watchlist" with strategist.mode="normal", or vice versa) raise `BundleAssemblyError`.
- [ ] Disagreeing `invocation_id` values raise `BundleAssemblyError`.
- [ ] §2 position_assessments order matches `strategist_output.position_assessments` order; §2 pending_order_assessments order matches `strategist_output.pending_order_assessments` order; §3 recommendations order matches `analyst_output.recommendations` order. (Three separate order-preservation tests.)
- [ ] Inner records are preserved byte-for-byte: a wrapped record's `model_dump()` produces the same dict as the inner record's own `model_dump()` for the inner-record-shaped slice.
- [ ] Mirror-symmetry property holds end-to-end: walking the bundle and asserting that every `wrapped_recommendation.pre_processor_annotations.conflicts[i]` has a matching entry on the cross-referenced strategist record's annotations.
- [ ] `combined_set_impact.basis.strategist_holds_excluded_count` equals the count of `position_assessments` whose `recommended_action == "hold"`.
- [ ] `conviction_distribution.total` equals `len(analyst_section.recommendations or ())`.
- [ ] `book_health_summary.total` equals `len(strategist_section.position_assessments)`.
- [ ] The full `ProposalPreProcessorBundle.model_dump()` validates against `BUNDLE_OUTPUT_SCHEMA` using a JSON Schema validator (`jsonschema.validate`).
- [ ] The runner is pure: two calls with equal inputs produce equal outputs (`bundle_a == bundle_b` via Pydantic equality).
- [ ] No `claude_agent_sdk` import; no clock read; no `uuid.uuid4()` call inside the runner or assembler.
- [ ] `tests/decision/proposal_pre_processor/test_assembler.py` and `test_runner.py` exist and pass under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_assembler.py tests/decision/proposal_pre_processor/test_runner.py -n auto`. The end-to-end mirror-symmetry property test and the schema-validation acceptance criterion are the central correctness checks; if both pass, the bundle is structurally consistent. Spot-check by constructing a normal-mode invocation in a Python REPL: `bundle = run_proposal_pre_processor(...)`; print `bundle.aggregate_observations.combined_set_impact.breaches`; verify the contributors signs match expectations for the constructed proposal set.
