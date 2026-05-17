# 05b — AnalystOutput validator (Layer-2/3)

## Goal

Implement the cross-field-invariant validator that runs against a parsed `AnalystOutput`. Layer 2 enforces structural invariants Pydantic can't see across siblings (e.g., `invalidation_rationale.leg_id` matches an `invalidation_legs[].leg_id`); Layer 3 enforces referential integrity (every `[XX-N]` reference in narrative fields resolves in the per-invocation `RetrievalStore`); plus a soft Layer-2 WARN check for the conviction-band sizing deviation (per parent issue's pre-resolved decision E). Returns a `ValidationResult` with errors AND warnings; the harness's corrective-retry path triggers only on errors.

## Reading

* `docs/design/testing/llm-output-validation.md` § Layer 2 — structural invariants, § Layer 3 — referential integrity. Authoritative for the validator's contract.
* `src/alphamind/analysis/qualitative_research/validation.py` — sibling-pattern for `ValidationResult`/`ValidationError` shape and the validator function signature.
* `src/alphamind/analysis/adaptive_research/validation.py` — second sibling; closer in scope (multi-thread referential checks).
* `docs/design/04-decision-layer/analyst-output-schema.md` § Notes on cross-field invariants — every invariant the validator must enforce.
* `docs/design/04-decision-layer/analyst.md` § Conviction scale — the band lookup table for the soft WARN check.
* `src/alphamind/decision/analyst/models.py` (story 03) — `AnalystOutput`, `Recommendation`, `WatchlistEntry`.
* `src/alphamind/analysis/synthesizer/retrieval.py` — `RetrievalStore.lookup` for the Layer-3 reference resolution.
* `src/alphamind/analysis/synthesizer/models.py` — `parse_reference_id`, `ReferencePrefix` for ref-ID parsing.

## Depends on

* `03 — AnalystOutput typed model` (ALP-293, this work tree).

## Scope

In scope: `src/alphamind/decision/analyst/validation.py` plus exports in `src/alphamind/decision/analyst/__init__.py`. Tests at `tests/decision/analyst/test_validation.py`.

### 1\. ValidationError + ValidationWarning + ValidationResult records

Mirror qualitative_research/validation.py:

```python
class ValidationError(BaseModel, frozen=True):
    field_path: str
    rule: str
    message: str

class ValidationWarning(BaseModel, frozen=True):
    field_path: str
    rule: str
    message: str

class ValidationResult(BaseModel, frozen=True):
    is_valid: bool  # True iff no errors (warnings don't disqualify)
    errors: tuple[ValidationError, ...]
    warnings: tuple[ValidationWarning, ...]
```

### 2\. The validator function

```python
def validate_analyst_output(
    output: AnalystOutput,
    *,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
    conviction_bands: dict[int, tuple[float, float]] | None = None,
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on *output*.

    Parameters
    ----------
    output
        The parsed analyst output.
    retrieval_store
        The per-invocation retrieval store assembled by the synthesizer.
        Used for Layer-3 reference-ID resolution in narrative fields.
    active_sectors
        The active portfolio profile's ``active_sectors`` set (4-way risk
        taxonomy: subset of {"tech", "semis", "financials", "energy"}).
        Recommendations whose ``sector`` is outside this set are an error.
    conviction_bands
        Mapping conviction_level → (lower_pct, upper_pct) for the soft
        sizing-band WARN check. Defaults to ``DEFAULT_CONVICTION_BANDS``
        derived from analyst.md § Conviction scale: {1: (0.25, 0.75),
        2: (0.5, 1.5), 3: (1.0, 3.0), 4: (2.0, 4.0), 5: (3.0, 5.0)}.
    """
```

### 3\. Layer-2 invariants

Walk each `Recommendation` and check:

**(a)** `invalidation_rationale[].leg_id` matches an existing `invalidation_legs[].leg_id`. Build the leg_id set from `invalidation_legs`; assert every `invalidation_rationale[].leg_id` is in the set. Also assert every `invalidation_legs[].leg_id` has a matching rationale entry. Bidirectional reference; missing in either direction is an ERROR.

**(b)** `underlying` matches `instrument.ticker` for equity, or `instrument.underlying` for option/strategy. Type-aware lookup based on `instrument.asset_type`. Mismatch is ERROR.

**(c) At least one** `invalidation_legs[]` entry has `is_hard: True`. Schema's allOf already forces this conditionally per type; this is a defensive cross-check that no recommendation slips through with all-event legs. Failure is ERROR.

**(d)** `recommendation_id` is unique within the invocation. Build a set; collision is ERROR.

**(e)** `sector` is in `active_sectors`. Failure is ERROR with rule `sector_not_active`.

**(f)** `instrument.asset_type` is permitted. The validation tool returns `feature_disabled` failures upstream; if a recommendation reaches the validator with `instrument.asset_type == "option"` AND `guardrail_validation_result.overall == "FAIL"` AND the failure_guidance text matches "feature_disabled" semantics, the validator surfaces this as an error (defense in depth — should have been caught by the analyst before emit).

**(g)** `entry_window` deadline is not in the past. Compare against `output.timestamp` as the reference clock. Past deadline is ERROR (the analyst emitted an already-expired window).

**(h)** `time_expectation_hours` is consistent with entry_window deadline (when present). If `entry_window` exists, the deadline + a reasonable resolution window should fit within `time_expectation_hours`. Soft check — if the deadline distance + estimated execution time exceeds time_expectation_hours, flag as WARN, not ERROR.

**(i) Soft band check (the parent's pre-resolved decision E — WARN-only).** For each recommendation, look up the conviction band for `conviction_level`. If `position_size.pct_of_portfolio` is outside the band, emit a `ValidationWarning` with rule `conviction_band_deviation` and a message like "conviction 4 (band 2.0–4.0%) but pct_of_portfolio is 5.5%". This is WARN, not ERROR.

### 4\. Layer-3 referential integrity

For each `Recommendation`, scan narrative fields (`thesis_narrative`, `target_rationale`, `invalidation_rationale[].rationale`, `position_size_rationale`, `entry_window_rationale`, `counterarguments_acknowledged`) for reference-ID patterns matching `[XX-N]` or `[XX-Y-N]` where XX is one of the synthesizer's `ReferencePrefix` values.

Use a regex matching `\[([A-Z][A-Z0-9-]*-[0-9]+)\]` to extract candidate IDs, then call `parse_reference_id` from `alphamind.analysis.synthesizer.models` to validate the prefix is in the canonical taxonomy. Skip non-canonical prefixes (e.g., literal `[INV-1]` references inside an `invalidation_rationale.rationale` text — though those should be rare since `INV-N` is the leg ID prefix).

For each extracted reference ID, call `retrieval_store.lookup(ref_id)`. If the result is None (the reference isn't in the store), emit a `ValidationError` with rule `unknown_reference` and field_path naming the recommendation_id and the narrative field.

For watchlist entries, scan `source_references[]` strings the same way.

### 5\. Skip path for watchlist mode

When `output.mode == "watchlist"`, skip Layer-2 invariants (a)–(i) (they're recommendation-shaped) and run only the watchlist-entry checks: each entry's `sector` is in active_sectors; each entry's `source_references` resolves in the retrieval store (Layer-3).

### 6\. Public surface

```python
__all__ = [
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "validate_analyst_output",
    "DEFAULT_CONVICTION_BANDS",
]
```

`DEFAULT_CONVICTION_BANDS` is the design-doc table as a `dict[int, tuple[float, float]]`.

### Out of scope

* Cross-recommendation interactions (overlap detection, cumulative exposure) — owned by the proposal pre-processor (ALP-118).
* Re-running validate_guardrail with the model's emitted size — the trust contract is "if the LLM said PASS, we trust the LLM"; the pre-processor recomputes from scratch anyway.
* Domain reasoning about whether a thesis is well-constructed — the PM owns that judgment.

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/validation.py` exists with `ValidationError`, `ValidationWarning`, `ValidationResult`, `validate_analyst_output`, `DEFAULT_CONVICTION_BANDS`.
- [ ] Layer-2 invariants (a)–(g) all produce a `ValidationError` with the right `rule` slug when violated; positive cases pass cleanly.
- [ ] Layer-2 invariant (h) produces a `ValidationWarning`, not an error, on deadline-vs-horizon mismatch.
- [ ] Layer-2 invariant (i) (conviction band) produces a `ValidationWarning` with rule `conviction_band_deviation` for a recommendation whose `pct_of_portfolio` falls outside the band, never an error.
- [ ] Layer-3 unknown-reference produces a `ValidationError` with rule `unknown_reference`.
- [ ] Watchlist mode skips recommendation-shaped checks and runs only watchlist-entry checks.
- [ ] Validator does not short-circuit on first error — collects all errors + warnings before returning.
- [ ] `tests/decision/analyst/test_validation.py` covers: valid baseline + parametric tests for each invariant (positive + negative case), Layer-3 unknown-reference, watchlist-mode path, the WARN-only conviction band, multi-error case (validator collects all).
- [ ] `src/alphamind/decision/analyst/__init__.py` re-exports the validator names.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_validation.py -n auto` passes. Manually run the validator against the prompt's `<example_output>` example to confirm a real-world-shaped recommendation passes cleanly.