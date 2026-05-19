# 03 — Combined-set impact + breach contributors

## Goal

Ship `compute_combined_set_impact(...)` — the §1.A producer. Given the analyst recommendations, the non-hold strategist position assessments, the portfolio snapshot, and the library config + market inputs, run `evaluate_proposals` once, map each `RuleProjection` to the schema's `PerRuleEntry`, and for each FAIL rule attribute per-proposal signed contributions by re-walking `RuleSpec.contribute(proposal, dae, state, config)` from the library's rule registry. Returns the typed `CombinedSetImpact` from story 01. This is the only place the pre-processor calls the library; story 04's runner orchestrates the call.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A `combined_set_impact` — basis section, per_rule shape, breaches with signed contributors, edge cases.
* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` `$defs/combined_set_impact`, `$defs/basis`, `$defs/per_rule_entry`, `$defs/breach_entry`, `$defs/contributor_entry` — the typed return shape.
* `docs/design/06-risk-guardrails/guardrail-evaluation.md` — entry-point contract for `evaluate_proposals`; `RuleProjection` semantics; `RuleSpec.contribute` signature.
* `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` — `evaluate_proposals`, `LibraryOutput`, `RuleProjection`, `ProposedDelta`, `LibraryConfig`, `MarketInputs`, `PortfolioStateSnapshot`, `RuleSpec`, `build_active_specs`, `Status` are all imported from here.
* `src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py` — call shape and the orchestration pattern; what's already in `LibraryOutput.per_rule` vs. what story 03 must compute on top.
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py` — `build_active_specs(config)` shape, `RuleSpec` fields (`rule_id`, `read_current`, `contribute`, `effective_limit_key`, `unit`, `magnitude`, `inverse`).
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/_helpers.py` — `RuleSpec` dataclass definition (read for the contribute callable's exact signature).
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/exposure.py`, `.../capital.py`, `.../options_greeks.py`, `.../shorts.py` — the per-rule contribute closures; understand how each spec computes per-proposal contributions (especially sector_concentration, which is generated dynamically per active sector).
* `src/alphamind/risk_guardrails/guardrail_evaluation/projection.py` — `project_rule` semantics; how `current` and `projected_after` relate to summed contributions.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — typed return shape `CombinedSetImpact`, `BasisSection`, `PerRuleEntry`, `BreachEntry`, `ContributorEntry`.
* <issue id="402fc84a-b75f-4d26-8af6-175f21b788a5">ALP-314</issue> (this work tree, story 02a) — `translate_recommendation_to_proposed_delta`, `translate_position_assessment_to_proposed_delta`, `TranslatorError`.

## Depends on

* <issue id="402fc84a-b75f-4d26-8af6-175f21b788a5">ALP-314</issue> (this work tree, story 02a) — proposal translation. Story 03 calls these translators on every analyst recommendation and every non-hold strategist position assessment.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — return-type shapes (transitive via 02a).

## Scope

In scope, all under `src/alphamind/decision/proposal_pre_processor/observations.py` (extend the module from story 02b — keep histograms and combined-set-impact in the same module per parent decision A's "observations" naming). Tests at `tests/decision/proposal_pre_processor/test_combined_set_impact.py`.

### 1\. Public function

```python
def compute_combined_set_impact(
    *,
    recommendations: Sequence[Recommendation],
    non_hold_position_assessments: Sequence[PositionAssessment],
    snapshot: PortfolioStateSnapshot,
    library_config: LibraryConfig,
    market: MarketInputs,
    snapshot_timestamp: datetime,
    strategist_holds_excluded_count: int,
) -> CombinedSetImpact:
    """Compute §1.A combined_set_impact.

    Caller (story 04) is responsible for filtering hold-action assessments
    out before calling; strategist_holds_excluded_count is the count of
    assessments excluded from non_hold_position_assessments. The function
    translates each input record to a ProposedDelta, calls evaluate_proposals
    once, maps the per-rule output, and computes per-proposal signed
    contributors for FAIL rules.
    """
```

### 2\. Translation step

For each `recommendation` in `recommendations`: call `translate_recommendation_to_proposed_delta(recommendation, snapshot=snapshot)`. For each `assessment` in `non_hold_position_assessments`: call `translate_position_assessment_to_proposed_delta(assessment, snapshot=snapshot)`. Collect both into a single tuple of `ProposedDelta` instances, preserving caller order (analyst recs first, then strategist non-hold actions). The translator's `id` field carries the original `REC-N` / `SA-N` identifier so the basis section and contributor attribution work natively.

### 3\. Library call

Call `evaluate_proposals(state=snapshot, proposals=<tuple from step 2>, config=library_config, market=market)`. Capture the returned `LibraryOutput`: `per_rule: tuple[RuleProjection, ...]`, `delta_adjusted: Mapping[str, DeltaAdjustedExposure]`, `feature_disabled: tuple[FeatureDisabledRejection, ...]`. The pre-processor does NOT surface `feature_disabled` records in the bundle — they represent proposals filtered before projection. (If a feature-disabled rejection occurs, the upstream agent's validation tool should have already rejected the proposal; the pre-processor treats their presence as a fixture bug. **Surface to operator** if any are present.)

### 4\. Map `RuleProjection` to `PerRuleEntry`

Build the `per_rule` tuple of `PerRuleEntry` instances:

```python
per_rule_entries = tuple(
    PerRuleEntry(
        rule=p.rule,
        status=p.status.value,  # Status enum -> "PASS" | "WARNING" | "FAIL" string
        current=p.current,
        limit=p.limit,
        projected_after=p.projected_after,
        headroom_remaining=p.headroom_remaining,
        unit=p.unit,
    )
    for p in library_output.per_rule
)
```

The library's `RuleProjection` carries an `inverse: bool` field that the schema's `per_rule_entry` does not; drop it (the floor-vs-cap semantics are absorbed by `headroom_remaining`'s sign).

### 5\. Compute breach contributors

For each `RuleProjection p` where `p.status is Status.FAIL`:

* Look up the `RuleSpec` whose `rule_id == p.rule` from `build_active_specs(library_config)`. The lookup is by `rule_id` equality.
* For each `(proposal, dae)` pair in the input set, compute `contribution = spec.contribute(proposal, dae, snapshot, library_config)`. The result is a signed float — the spec's per-proposal contribution toward the rule's projected delta.
* Drop contributors with `contribution == 0.0` (proposals that don't touch the rule). Keep negative contributions (proposals pulling the rule away from breach).
* Build a `BreachEntry` with: `rule=p.rule`, `overage=p.projected_after - p.limit` (always positive for a breach when `inverse=False`; for inverse rules the breach is below the floor, `overage = p.limit - p.projected_after`), `unit=p.unit`, `contributors=<tuple of ContributorEntry>`.

Spec lookup helper:

```python
def _build_spec_lookup(config: LibraryConfig) -> Mapping[str, RuleSpec]:
    return {spec.rule_id: spec for spec in build_active_specs(config)}
```

### 6\. Build the basis section

```python
basis = BasisSection(
    analyst_proposal_ids=tuple(r.recommendation_id for r in recommendations),
    strategist_action_ids=tuple(a.assessment_id for a in non_hold_position_assessments),
    strategist_holds_excluded_count=strategist_holds_excluded_count,
    snapshot_timestamp=snapshot_timestamp,
)
```

### 7\. Assemble

```python
return CombinedSetImpact(basis=basis, per_rule=per_rule_entries, breaches=breach_entries)
```

Where `breach_entries` is the tuple built in step 5.

### 8\. Public re-exports

Add `compute_combined_set_impact` to `src/alphamind/decision/proposal_pre_processor/__init__.py`.

### Out of scope

* Translator implementation (story 02a).
* Histogram computations (story 02b — same module, different functions).
* Conflict detection (story 02c).
* Bundle assembly + runner (story 04).
* Verify script (story 05).

## Acceptance criteria

- [ ] Empty inputs (zero recommendations, zero non-hold assessments) produce a `CombinedSetImpact` whose `basis.analyst_proposal_ids = ()`, `basis.strategist_action_ids = ()`, `per_rule` is the library's projection of zero proposals (matches `state` baseline), `breaches = ()`.
- [ ] Single equity recommendation that does NOT breach any rule produces `breaches = ()` and a `per_rule[]` whose statuses are all PASS or WARNING.
- [ ] Single recommendation that breaches `net_long_exposure` produces a `BreachEntry` for `net_long_exposure` whose `contributors[]` contains the recommendation's `REC-N` id with positive `contribution` equal to its delta-adjusted exposure contribution to the rule.
- [ ] Combined set with two recommendations and one strategist `close` action where the close pulls the rule away from breach produces a `BreachEntry` with three contributors: two positive (the recommendations) and one negative (the close).
- [ ] When a per_rule entry has `status=FAIL` and `inverse=True` (e.g., `min_cash_reserve_pct`), `BreachEntry.overage = limit - projected_after` (positive). The story's spec-lookup must consult the spec's `inverse` flag.
- [ ] When `RuleSpec.contribute` returns 0.0 for a proposal, that proposal does NOT appear in `contributors[]`.
- [ ] When library returns a non-empty `feature_disabled` tuple, the function raises `LibraryFeatureDisabledError("upstream did not filter feature-disabled proposals: <ids>")` — surfacing condition, do not paper over.
- [ ] The function is pure: same inputs produce same outputs (deterministic). No clock reads (the `snapshot_timestamp` is passed in).
- [ ] The function is called once with all proposals — does NOT call `evaluate_proposals` per-proposal.
- [ ] Spec lookup raises `KeyError` (or a wrapped error) if a FAIL rule's `rule_id` does not appear in `build_active_specs(config)` — this is the per-sector dynamic-spec edge case the parent's surfacing condition covers; the orchestrator will pause if hit.
- [ ] `tests/decision/proposal_pre_processor/test_combined_set_impact.py` exists and passes under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_combined_set_impact.py -n auto`. The breach-attribution test is the correctness gate: construct a fixture state where exactly one rule fails under a known proposal set, then assert each contributor's signed contribution matches the spec's individual `contribute()` output.
