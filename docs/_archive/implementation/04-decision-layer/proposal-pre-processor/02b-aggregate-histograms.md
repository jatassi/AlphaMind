# 02b — Aggregate histograms

## Goal

Ship two pure functions that compute §1.B `conviction_distribution` and §1.C `book_health_summary` from analyst recommendations and strategist position assessments respectively. Both are deterministic histograms with a fixed key set per the schema enums; story 04's assembler calls these and embeds the results in the bundle.

## Reading

* `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.B `conviction_distribution`, § §1.C `book_health_summary` — the canonical definitions and the at-a-glance scoreboard semantics.
* `docs/design/04-decision-layer/proposal-pre-processor-bundle-schema.md` `$defs/conviction_distribution`, `$defs/book_health_summary`, `$defs/by_thesis_status`, `$defs/by_recommended_action` — required keys and `additionalProperties: false`.
* `src/alphamind/decision/analyst/models.py` — `Recommendation` shape, particularly `conviction_level: int 1..5`.
* `src/alphamind/decision/strategist/models.py` — `PositionAssessment` shape, particularly `thesis_status` (lowercase-hyphenated `"on-track"` … `"invalidated"`), `recommended_action` (lowercase-hyphenated `"hold"` … `"add"`), `remedy_flag: str | None`.
* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree, story 01) — the typed return types `ConvictionDistribution`, `ConvictionHistogram`, `BookHealthSummary`, `ByThesisStatus`, `ByRecommendedAction` are defined there.

## Depends on

* <issue id="e149a8a3-2d41-4bea-9ad1-1b891e793369">ALP-313</issue> (this work tree) — typed models for the return shapes.

## Scope

In scope, all under `src/alphamind/decision/proposal_pre_processor/observations.py`. Tests at `tests/decision/proposal_pre_processor/test_observations.py`.

### 1\. `compute_conviction_distribution`

```python
def compute_conviction_distribution(
    recommendations: Sequence[Recommendation],
) -> ConvictionDistribution:
    """Histogram analyst recommendations by conviction level (1 through 5).

    Watchlist mode: caller passes an empty sequence; result is all zeros with total=0.
    """
```

Behavior:

* Iterate `recommendations` once. For each `recommendation.conviction_level` (1–5), increment the corresponding `level_<N>` slot.
* `total` equals `len(recommendations)`.
* The schema's required keys (`"1"` through `"5"`) are always present (zero when no recommendation has that conviction). The Pydantic model from story 01 enforces presence.

### 2\. `compute_book_health_summary`

```python
def compute_book_health_summary(
    position_assessments: Sequence[PositionAssessment],
) -> BookHealthSummary:
    """Histogram strategist position assessments by thesis_status and recommended_action.

    Empty book: caller passes an empty sequence; result is all zeros, remedy_flagged_count=0,
    total=0.
    """
```

Behavior:

* Iterate `position_assessments` once.
* `by_thesis_status` — increment the slot for each assessment's `thesis_status` value.
* `by_recommended_action` — increment the slot for each assessment's `recommended_action` value.
* `remedy_flagged_count` — count assessments where `remedy_flag is not None` (per design doc: "Count of position assessments with remedy_flag populated").
* `total` equals `len(position_assessments)`.

### 3\. Public re-exports

Add `compute_conviction_distribution` and `compute_book_health_summary` to `src/alphamind/decision/proposal_pre_processor/__init__.py`.

### Out of scope

* Combined-set impact (§1.A) — story 03.
* Conflict detection — story 02c.
* Bundle assembly — story 04.
* Translation logic — story 02a.

## Acceptance criteria

- [ ] `compute_conviction_distribution(())` returns a `ConvictionDistribution` with all five histogram slots equal to 0 and `total=0`.
- [ ] `compute_conviction_distribution((rec_1, rec_2, rec_3))` where the three recommendations have `conviction_level` 2, 3, 3 returns `by_level={1:0, 2:1, 3:2, 4:0, 5:0}` and `total=3`.
- [ ] `compute_conviction_distribution(...)` rejects a recommendation whose `conviction_level` is outside 1..5 (this should never happen for valid `Recommendation` inputs — Pydantic enforces — but a defensive `ValueError` keeps the contract honest).
- [ ] `compute_book_health_summary(())` returns a `BookHealthSummary` with all ten histogram slots zero, `remedy_flagged_count=0`, `total=0`.
- [ ] For three position assessments with `thesis_status` values `"on-track"`, `"on-track"`, `"at-risk"` and `recommended_action` values `"hold"`, `"reduce"`, `"close"`, returns `by_thesis_status={"on-track":2, "partially-realized":0, "at-risk":1, "stale":0, "invalidated":0}`, `by_recommended_action={"hold":1, "reduce":1, "close":1, "adjust-bracket":0, "add":0}`, `total=3`.
- [ ] `remedy_flagged_count` equals the number of assessments whose `remedy_flag` is non-`None`. An assessment with `remedy_flag=""` (empty string) does NOT count — only `None` is excluded; a populated string is included. (The schema doesn't pin this; the natural reading of "populated" is "not None or empty". Use `bool(assessment.remedy_flag)` for the count.)
- [ ] The returned `ConvictionDistribution` and `BookHealthSummary` round-trip through `model_dump(by_alias=True)` and `model_validate` without value loss.
- [ ] Both functions are pure: same input sequence produces same output; no mutation of inputs.
- [ ] `tests/decision/proposal_pre_processor/test_observations.py` exists and passes under `uv run pytest tests/decision/proposal_pre_processor/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/decision/proposal_pre_processor/test_observations.py -n auto`. Spot-check that the histogram slots are populated by alias (the model dump uses the schema's hyphenated/numeric keys). Run a property-style sanity test: for any input sequence of length N, the histogram totals across all slots equal N.
