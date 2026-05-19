# 02c — Conflict detection

## Goal

Ship `detect_conflicts(recommendations, position_assessments, pending_order_assessments)` — a pure function that produces the same-underlying conflict matrix between analyst recommendations and strategist records, classifying each conflict into one of seven `conflict_type` values, and returning mirror-symmetric annotation tuples keyed by record id. Story 04 consumes the returned dictionaries to attach `pre_processor_annotations.conflicts` to each wrapper. The mirror-symmetry invariant is the central correctness property: every analyst-side entry has a strategist-side counterpart on the matching record.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor.md` § Wrap pattern and conflicts annotation — the conflict-type enum table; mirror-symmetry semantics; pending-order conflict scope (only `entry_limit` / `entry_stop_limit`, not bracket legs).
* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` `$defs/strategist_side_conflict`, `$defs/analyst_side_conflict`, `$defs/conflict_type` — the typed shape of each conflict entry; cross-reference IDs (`with_recommendation_id`, `with_assessment_id`, `with_pending_order_assessment_id`).
* `docs/design/04-decision-layer/analyst-output-schema.md` `$defs/recommendation` `instrument`, `direction`, `underlying` — to resolve same-underlying matches and direction comparisons.
* `docs/design/04-decision-layer/strategist-output-schema.md` `$defs/position_assessment` `recommended_action`, `underlying`, `position_id`; `$defs/pending_order_assessment` `order_type`, `recommended_action`, `position_id`, `linked_position_assessment_id` — to resolve which interactions are eligible.
* `src/alphamind/decision/{analyst, strategist}/models.py` — the imported records' field names.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — the typed return shapes (`AnalystSideConflict`, `StrategistSideConflict`, `ConflictType`).

## Depends on

* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree) — typed conflict-annotation models.

## Scope

In scope, all under `src/alphamind/decision/proposal_pre_processor/conflicts.py`. Tests at `tests/decision/proposal_pre_processor/test_conflicts.py`.

### 1\. Public function

```python
def detect_conflicts(
    *,
    recommendations: Sequence[Recommendation],
    position_assessments: Sequence[PositionAssessment],
    pending_order_assessments: Sequence[PendingOrderAssessment],
) -> ConflictDetectionResult:
    """Build the same-underlying conflict matrix.

    Returns a dataclass-shaped result with three lookup maps:
      * analyst_conflicts_by_rec_id: dict[str, tuple[AnalystSideConflict, ...]]
      * strategist_position_conflicts_by_assessment_id: dict[str, tuple[StrategistSideConflict, ...]]
      * strategist_pending_conflicts_by_assessment_id: dict[str, tuple[StrategistSideConflict, ...]]

    Each record id (REC-N, SA-N, SA-ORD-N) appears as a key with its full conflict tuple
    (possibly empty). The result keys exactly cover the input records — no missing keys,
    no extra keys.

    Mirror symmetry: for every analyst-side entry pointing at SA-X, there is a
    strategist-side entry on SA-X pointing back at the originating REC-N (with the same
    underlying and the same conflict_type). Same invariant for SA-ORD-X.

    Watchlist mode: if recommendations is empty, all maps are empty / all-empty-tuples.
    No conflicts can exist on §2 entries when there are no recommendations.
    """
```

### 2\. ConflictDetectionResult dataclass

```python
@dataclass(frozen=True, slots=True)
class ConflictDetectionResult:
    analyst_conflicts_by_rec_id: Mapping[str, tuple[AnalystSideConflict, ...]]
    strategist_position_conflicts_by_assessment_id: Mapping[str, tuple[StrategistSideConflict, ...]]
    strategist_pending_conflicts_by_assessment_id: Mapping[str, tuple[StrategistSideConflict, ...]]
```

### 3\. Conflict-type classification rules

Per the design-doc enum table:

* `entry_vs_close` — analyst entry on `T` × strategist `close` on a position with `underlying=T`.
* `entry_vs_add` — analyst entry on `T` × strategist `add` on a position with `underlying=T`.
* `entry_vs_hold` — analyst entry on `T` × strategist `hold` on a position with `underlying=T`, AND the analyst's instrument direction matches the held position's direction.
* `entry_direction_conflict` — analyst entry on `T` × strategist `hold` on a position with `underlying=T`, AND the analyst's direction is OPPOSITE the held position's direction. (Implementation note: read the held direction from the snapshot's `existing_positions[assessment.position_id].direction` — but story 02c does NOT take the snapshot. Per parent decision: pass the direction lookup as an additional parameter `held_direction_resolver: Callable[[str], Direction]` that maps `position_id` → `Direction`. Caller seeds this from the snapshot.) **Add** `held_direction_resolver` to the function signature.
* `entry_vs_pending_maintain` — analyst entry on `T` × pending entry order on `T` with `recommended_action="maintain"`.
* `entry_vs_pending_modify` — same, `recommended_action="modify"`.
* `entry_vs_pending_cancel` — same, `recommended_action="cancel"`.

For `reduce`-action position assessments, treat as a position the analyst's entry interacts with using the `entry_vs_close` classification (a partial close is still "the strategist is reducing exposure here"). For `adjust-bracket`-action: no conflict — the strategist isn't changing the underlying position's direction or size, so an analyst entry on the same underlying isn't conflicting in the way the enum captures. (Surface to operator if this proves wrong; the design doc's enum doesn't include an `entry_vs_adjust_bracket` case, so no conflict is the right reading.)

### 4\. Pending-order scope

Only `pending_order_assessment.order_type ∈ {"entry_limit", "entry_stop_limit"}` is eligible for conflict matching. Bracket legs (`bracket_target`, `bracket_price_stop`, `bracket_time_stop`, `bracket_event_stop`) carry empty conflict tuples — their conflicts are captured by the parent position's assessment (per design doc). The function MUST still emit the bracket-leg pending-order id as a key in `strategist_pending_conflicts_by_assessment_id` with an empty tuple.

### 5\. Resolving the held-direction lookup

The function signature becomes:

```python
def detect_conflicts(
    *,
    recommendations: Sequence[Recommendation],
    position_assessments: Sequence[PositionAssessment],
    pending_order_assessments: Sequence[PendingOrderAssessment],
    held_direction_resolver: Callable[[str], Literal["long", "short"]],
) -> ConflictDetectionResult:
    ...
```

`held_direction_resolver(position_id)` returns the direction of the existing position. Caller (story 04) seeds this from the snapshot's existing_positions map.

### 6\. Public re-exports

Add `detect_conflicts`, `ConflictDetectionResult` to `src/alphamind/decision/proposal_pre_processor/__init__.py`.

### Out of scope

* Translation to `ProposedDelta` (story 02a).
* Histograms (story 02b).
* Combined-set impact (story 03).
* Bundle assembly (story 04) — story 04 wires the returned dict-maps into per-record `pre_processor_annotations`.

## Acceptance criteria

- [ ] Empty inputs (zero recommendations, zero position assessments, zero pending orders) produce empty maps.
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="close"` produces an analyst-side conflict `entry_vs_close` referencing the assessment id, and a strategist-side conflict `entry_vs_close` on that assessment referencing the recommendation id. Mirror-symmetric.
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="hold"` and same direction produces `entry_vs_hold` on both sides.
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="hold"` and OPPOSITE direction produces `entry_direction_conflict` on both sides.
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="add"` produces `entry_vs_add`.
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="reduce"` produces `entry_vs_close` (partial-close-as-reduction reading).
- [ ] Recommendations on `T` paired with a position assessment on `T` with `recommended_action="adjust-bracket"` produces NO conflict on either side (empty tuple).
- [ ] Recommendations on `T` paired with a pending entry order on `T` (`order_type="entry_limit"`, `recommended_action="modify"`) produces `entry_vs_pending_modify` referencing the pending-order assessment id.
- [ ] Bracket-leg pending orders (`order_type="bracket_price_stop"`) produce empty conflict tuples even when an analyst recommendation targets the same underlying.
- [ ] Mirror-symmetry property test: every analyst-side conflict has a corresponding strategist-side conflict with the same `underlying` and `conflict_type`, on the cross-referenced record. Test by walking the result and asserting bidirectional consistency.
- [ ] The result's three dict keys exactly cover the input records — no missing keys (every recommendation_id, assessment_id, pending_order_assessment_id appears as a key) and no extra keys.
- [ ] Multiple recommendations on the same underlying each get their own conflict entries; the result correctly handles the N×M product when N analyst recommendations share an underlying with M strategist records.
- [ ] `tests/decision/proposal_pre_processor/test_conflicts.py` exists and passes under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_conflicts.py -n auto`. The mirror-symmetry property test is the central correctness check; if it passes, the matrix is consistent. Spot-check the seven-case enum coverage by looking at the test's parameterization — every conflict_type value should be exercised by at least one test case.
