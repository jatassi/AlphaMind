---
status: done
completed_date: 2026-04-27
commit_id: fcf29bc
---

# 03f — Thesis quality aggregate records

## Goal

Define the consumer-facing typed records for thesis quality trends (raw state category 6) at `src/alphamind/portfolio_state/records/thesis_quality.py`, with field constraints validated via Pydantic v2 and a corresponding test suite at `tests/portfolio_state/records/test_thesis_quality.py`.

These are the read-side shapes the ingestion layer delivers to the analysis pipeline. Computation is the persistence layer's responsibility (Phase 1 write path, updated on each thesis resolution per `state-persistence.md` Tier 3). This story owns only the record definitions and their structural invariants.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — § 6 (thesis quality trends): 6a (thesis accuracy trend), 6b (signal reliability and conviction calibration), 6c (performance attribution trailing) — the consumer-facing delivery contract this story models
- `../../../design/05-execution-layer/state-persistence.md` § Tier 3 — Derived/aggregate entities — the persistence-layer aggregate description, its update trigger (thesis resolution), and its role in the raw state contract
- `../../../design/05-execution-layer/thesis-model.md` § Resolution — the four resolution categories and per-component outcomes the aggregates are computed over
- `../../../design/04-decision-layer/analyst.md` § Conviction scale — the 1–5 conviction level definition and the calibration target driving `ConvictionCalibrationEntry`
- `02-package-skeleton-and-config.md` — package layout (`src/alphamind/portfolio_state/records/thesis_quality.py`) and the `thesis_quality_aggregates.trailing_windows_days: [5, 20]` config knob whose three-member shape (5d, 20d, inception) `TrailingWindow` models
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: frozen Pydantic v2 records, StrEnum discriminators, validation tests, acceptance-criteria style

## Depends on

- 02
- 03d (for `RegimeLabel`; see Notes)

## Scope

In scope:

### 1. Enums — all `StrEnum`, members in ALL CAPS

- **`TrailingWindow`** — the three aggregate windows per portfolio-state.md § 6a "trailing validation rate over 5d, 20d, inception windows":
  - `FIVE_DAYS`
  - `TWENTY_DAYS`
  - `INCEPTION`

- **`ThesisType`** — the four thesis natures that drive P/L attribution per portfolio-state.md § 6c "P/L by thesis type: realized P/L by nature":
  - `EVENT_DRIVEN`
  - `MEAN_REVERSION`
  - `MOMENTUM`
  - `CROSS_ASSET_DIVERGENCE`

- **`InvalidationTimingClass`** — per portfolio-state.md § 6a "Invalidation quality: when invalidated, was the invalidation hit early (mistimed entry, weak thesis) or late (well-calibrated invalidation, wrong direction)":
  - `EARLY`
  - `ON_TIME`
  - `LATE`

- **`AttributionDimension`** — the three dimensions used in `PerformanceAttributionEntry`:
  - `SECTOR`
  - `THESIS_TYPE`
  - `REGIME`

- **`RegimeLabel`** — imported from `alphamind.portfolio_state.records.capital` (story 03d). Do not redeclare. See Notes for the convergence handling if story 03d has not yet landed.

- **`SignalType`** — not modelled as a closed enum. `portfolio-state.md` § 6b lists "unusual options activity, earnings revisions, prediction market shifts" as illustrative examples, not an exhaustive taxonomy. No closed enumeration exists anywhere in the design docs; adding one here would invent a contract. All signal-type fields are typed `str`.

### 2. Value objects — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`

- **`ResolutionWindowCounts`** — trailing resolution counts per thesis-model.md resolution categories for one window:
  - `window: TrailingWindow`
  - `total_resolutions: int` — total resolved theses contributing to this window; non-negative
  - `validated: int` — "validated — correct for the right reasons" per thesis-model.md; non-negative
  - `profitable_but_wrong: int` — non-negative
  - `invalidated_stopped_correctly: int` — non-negative
  - `invalidated_wrong_on_exit: int` — non-negative
  - `cancelled_never_entered: int` — non-negative
  - Property `validation_rate -> float | None`: `validated / total_resolutions` when `total_resolutions > 0`; `None` otherwise. Never raises on zero-divisor input.

- **`ThesisDurationStat`** — thesis duration accuracy for one window, per portfolio-state.md § 6a "Thesis duration accuracy: are theses resolving faster or slower than predicted?":
  - `window: TrailingWindow`
  - `mean_actual_to_expected_ratio: float | None` — `None` when the window contains no resolutions; finite when present
  - `median_actual_to_expected_ratio: float | None` — `None` when the window contains no resolutions; finite when present
  - `count: int` — number of resolved theses contributing to this stat; non-negative

- **`InvalidationTimingStat`** — per portfolio-state.md § 6a "Invalidation quality: early / late":
  - `window: TrailingWindow`
  - `class_distribution: dict[InvalidationTimingClass, int]` — exhaustive mapping over all `InvalidationTimingClass` members; zero counts permitted; all values non-negative
  - `mean_position_age_at_invalidation_hours: float | None` — `None` when no invalidations in window; finite when present; non-negative when present

- **`SignalHitRate`** — trailing hit rate for one `(signal_type, window)` pair, per portfolio-state.md § 6b "Signal hit rate: trailing hit rate for each signal type cited in theses":
  - `signal_type: str` — opaque signal type string; see Notes on `SignalType`
  - `window: TrailingWindow`
  - `cited_count: int` — number of theses in which this signal type was cited; non-negative
  - `validated_count: int` — number of those theses that resolved as validated; non-negative
  - Property `hit_rate -> float | None`: `validated_count / cited_count` when `cited_count > 0`; `None` otherwise.

- **`SignalToThesisConversion`** — per portfolio-state.md § 6b "Signal-to-thesis conversion: how often each signal type leads to a PM-approved thesis":
  - `signal_type: str`
  - `window: TrailingWindow`
  - `signal_observed_count: int` — number of times this signal type was observed in the data layer during the window; non-negative
  - `pm_approved_count: int` — number of theses citing this signal type that received PM approval; non-negative
  - Property `conversion_rate -> float | None`: `pm_approved_count / signal_observed_count` when `signal_observed_count > 0`; `None` otherwise.

- **`ConvictionCalibrationEntry`** — one `(conviction_level, window)` slot per portfolio-state.md § 6b "Conviction calibration: validation rate by conviction level (1–5)":
  - `conviction_level: int` — in `{1, 2, 3, 4, 5}` per analyst.md conviction scale; enforced by validator
  - `window: TrailingWindow`
  - `count: int` — number of resolved theses at this conviction level in the window; non-negative
  - `validation_rate: float | None` — `None` when `count == 0`; finite when present
  - `mean_realized_pnl_pct: float | None` — mean realized P/L percentage across resolved theses at this conviction level; `None` when `count == 0`; finite when present

- **`ConvictionSizingDeviation`** — per portfolio-state.md § 6b "Conviction-sizing deviation tracking: how often the PM sizes outside the advisory band, direction (above/below), and outcome correlation":
  - `window: TrailingWindow`
  - `total_proposals: int` — total PM-evaluated proposals in the window; non-negative
  - `pm_sized_above_advisory_count: int` — proposals where PM sized above analyst advisory band; non-negative
  - `pm_sized_below_advisory_count: int` — proposals where PM sized below analyst advisory band; non-negative
  - `pm_sized_within_advisory_count: int` — proposals where PM sized within analyst advisory band; non-negative
  - Property `deviation_rate -> float | None`: `(pm_sized_above_advisory_count + pm_sized_below_advisory_count) / total_proposals` when `total_proposals > 0`; `None` otherwise.
  - `outcome_correlation_above: float | None` — mean realized P/L on above-advisory sized proposals minus mean on within-advisory proposals; `None` when insufficient data; finite when present
  - `outcome_correlation_below: float | None` — mean realized P/L on below-advisory sized proposals minus mean on within-advisory proposals; `None` when insufficient data; finite when present

- **`PerformanceAttributionEntry`** — one `(dimension, key, window)` slice, per portfolio-state.md § 6c "P/L by sector", "P/L by thesis type", "P/L by market regime":
  - `dimension: AttributionDimension`
  - `key: str` — sector name, `ThesisType` member name, or `RegimeLabel` member name for the slice
  - `window: TrailingWindow`
  - `cumulative_realized_pnl_usd: float` — cumulative realized P/L in dollars for this slice and window; finite
  - `realized_pnl_pct_of_window_capital: float | None` — realized P/L as a percentage of total capital deployed in this window; `None` when window capital is zero or undefined; finite when present
  - `count: int` — number of resolved theses contributing to this slice; non-negative

- **`AlphaBetaDecomposition`** — per portfolio-state.md § 6c "Alpha vs. beta decomposition (trailing): how much total return came from directional market exposure vs. thesis-driven selection":
  - `window: TrailingWindow`
  - `total_realized_pnl_usd: float` — total realized P/L for the window; finite
  - `market_component_usd: float` — portion of total P/L attributable to directional market exposure; finite
  - `sector_component_usd: float` — portion of total P/L attributable to sector-level exposure; finite
  - `alpha_component_usd: float` — portion of total P/L attributable to thesis-driven selection; finite
  - Property `attribution_ratio -> float | None`: `alpha_component_usd / total_realized_pnl_usd` when `total_realized_pnl_usd != 0`; `None` otherwise. Signed — negative when alpha detracted.

- **`ThesisQualityAggregate`** — the outer record consumed via raw state category 6:
  - `as_of_timestamp: datetime` — tz-aware UTC; when the aggregate was last recomputed by the persistence layer
  - `resolution_counts_by_window: tuple[ResolutionWindowCounts, ...]` — one entry per window; ordered `FIVE_DAYS`, `TWENTY_DAYS`, `INCEPTION`; each window appears at most once (validated)
  - `duration_stats_by_window: tuple[ThesisDurationStat, ...]` — one entry per window; each window at most once
  - `invalidation_timing_stats_by_window: tuple[InvalidationTimingStat, ...]` — one entry per window; each window at most once
  - `signal_hit_rates: tuple[SignalHitRate, ...]` — one entry per `(signal_type, window)` pair; pairs are unique (validated)
  - `signal_to_thesis_conversions: tuple[SignalToThesisConversion, ...]` — one entry per `(signal_type, window)` pair; pairs are unique (validated)
  - `conviction_calibration: tuple[ConvictionCalibrationEntry, ...]` — one entry per `(conviction_level, window)` pair; pairs are unique (validated)
  - `conviction_sizing_deviation_by_window: tuple[ConvictionSizingDeviation, ...]` — one entry per window; each window at most once
  - `performance_attribution: tuple[PerformanceAttributionEntry, ...]` — one entry per `(dimension, key, window)` triple; triples are unique (validated)
  - `alpha_beta_decomposition_by_window: tuple[AlphaBetaDecomposition, ...]` — one entry per window; each window at most once
  - Helper methods:
    - `counts_for(window: TrailingWindow) -> ResolutionWindowCounts | None` — returns the `ResolutionWindowCounts` entry for `window`; `None` if not present
    - `signal_hit_rate(signal_type: str, window: TrailingWindow) -> SignalHitRate | None` — returns the `SignalHitRate` for the `(signal_type, window)` pair; `None` if not present
    - `conviction_entries_for(window: TrailingWindow) -> tuple[ConvictionCalibrationEntry, ...]` — returns all `ConvictionCalibrationEntry` instances for `window`; empty tuple if none

### 3. Validation rules

Enforced via Pydantic `model_validator` or `field_validator` as appropriate:

- All count fields (`total_resolutions`, `validated`, `profitable_but_wrong`, `invalidated_stopped_correctly`, `invalidated_wrong_on_exit`, `cancelled_never_entered`, `cited_count`, `validated_count`, `signal_observed_count`, `pm_approved_count`, `count`, `total_proposals`, `pm_sized_above_advisory_count`, `pm_sized_below_advisory_count`, `pm_sized_within_advisory_count`) are non-negative integers.
- `ResolutionWindowCounts` conservation invariant: `validated + profitable_but_wrong + invalidated_stopped_correctly + invalidated_wrong_on_exit + cancelled_never_entered == total_resolutions`.
- All `*_pct` float fields are finite (no NaN, no ±Inf).
- `InvalidationTimingStat.class_distribution` is exhaustive over `InvalidationTimingClass` (all three members present as keys); all values non-negative.
- `InvalidationTimingStat.mean_position_age_at_invalidation_hours` is non-negative when not `None`.
- `ThesisDurationStat.mean_actual_to_expected_ratio` and `median_actual_to_expected_ratio` are finite when not `None`.
- `ConvictionCalibrationEntry.conviction_level` is in `{1, 2, 3, 4, 5}`; violation raises `ValidationError`.
- `ConvictionCalibrationEntry.validation_rate` and `mean_realized_pnl_pct` are finite when not `None`.
- `ConvictionSizingDeviation`: `pm_sized_above_advisory_count + pm_sized_below_advisory_count + pm_sized_within_advisory_count == total_proposals` (conservation invariant).
- `ConvictionSizingDeviation.outcome_correlation_above` and `outcome_correlation_below` are finite when not `None`.
- `PerformanceAttributionEntry.cumulative_realized_pnl_usd` is finite; `realized_pnl_pct_of_window_capital` is finite when not `None`.
- `AlphaBetaDecomposition.total_realized_pnl_usd`, `market_component_usd`, `sector_component_usd`, `alpha_component_usd` are all finite.
- `ThesisQualityAggregate.as_of_timestamp` is tz-aware UTC; a naive datetime raises `ValidationError`.
- `ThesisQualityAggregate` window uniqueness: each `TrailingWindow` value appears at most once in each `*_by_window` tuple (`resolution_counts_by_window`, `duration_stats_by_window`, `invalidation_timing_stats_by_window`, `conviction_sizing_deviation_by_window`, `alpha_beta_decomposition_by_window`).
- `ThesisQualityAggregate` pair uniqueness: `(signal_type, window)` unique within `signal_hit_rates`; `(signal_type, window)` unique within `signal_to_thesis_conversions`; `(conviction_level, window)` unique within `conviction_calibration`; `(dimension, key, window)` unique within `performance_attribution`.

### 4. Tests at `tests/portfolio_state/records/test_thesis_quality.py`

- Each enum: correct member set, correct string values; `TrailingWindow` has exactly `FIVE_DAYS`, `TWENTY_DAYS`, `INCEPTION`; `ThesisType` has exactly `EVENT_DRIVEN`, `MEAN_REVERSION`, `MOMENTUM`, `CROSS_ASSET_DIVERGENCE`; `InvalidationTimingClass` has exactly `EARLY`, `ON_TIME`, `LATE`; `AttributionDimension` has exactly `SECTOR`, `THESIS_TYPE`, `REGIME`.
- `ResolutionWindowCounts`: valid construction passes; `validation_rate` returns the correct ratio when `total_resolutions > 0`; `validation_rate` returns `None` when `total_resolutions == 0` (no exception); conservation invariant violation raises `ValidationError`; any count field below zero raises `ValidationError`.
- `ThesisDurationStat`: valid construction with populated and `None` ratios; negative `count` raises `ValidationError`; non-finite `mean_actual_to_expected_ratio` raises `ValidationError`.
- `InvalidationTimingStat`: valid construction; missing `InvalidationTimingClass` key in `class_distribution` raises `ValidationError`; negative value in `class_distribution` raises `ValidationError`; non-finite `mean_position_age_at_invalidation_hours` raises `ValidationError`; negative `mean_position_age_at_invalidation_hours` raises `ValidationError`.
- `SignalHitRate`: valid construction; `hit_rate` returns correct ratio; `hit_rate` returns `None` when `cited_count == 0`; `validated_count > cited_count` is permitted (no constraint imposed — the counts track different things across different time slices per window).
- `SignalToThesisConversion`: valid construction; `conversion_rate` returns `None` when `signal_observed_count == 0`.
- `ConvictionCalibrationEntry`: valid construction; `conviction_level` outside `{1, 2, 3, 4, 5}` raises `ValidationError`; non-finite `validation_rate` raises `ValidationError`; `validation_rate` accepts `None` when `count == 0`.
- `ConvictionSizingDeviation`: valid construction; `deviation_rate` returns `None` when `total_proposals == 0`; `deviation_rate` returns the correct ratio; conservation invariant violation raises `ValidationError`; finite `outcome_correlation_above` accepted; `None` accepted.
- `PerformanceAttributionEntry`: valid construction with all `AttributionDimension` values; non-finite `cumulative_realized_pnl_usd` raises `ValidationError`.
- `AlphaBetaDecomposition`: valid construction; `attribution_ratio` returns `None` when `total_realized_pnl_usd == 0`; `attribution_ratio` returns the signed ratio when non-zero; non-finite component field raises `ValidationError`.
- `ThesisQualityAggregate`:
  - Happy-path fixture: build a minimal but complete aggregate with one entry per window for each `*_by_window` tuple, at least two distinct `signal_type` values and two distinct conviction levels, and verify all helpers return the expected projections.
  - `counts_for` returns the correct `ResolutionWindowCounts` for an existing window; returns `None` for an absent window.
  - `signal_hit_rate` returns the correct `SignalHitRate` for a known `(signal_type, window)` pair; returns `None` for an absent pair.
  - `conviction_entries_for` returns the correct entries for a known window; returns an empty tuple for a window with no entries.
  - Naive `as_of_timestamp` raises `ValidationError`.
  - Duplicate window in any `*_by_window` tuple raises `ValidationError`.
  - Duplicate `(signal_type, window)` in `signal_hit_rates` raises `ValidationError`.
  - Duplicate `(conviction_level, window)` in `conviction_calibration` raises `ValidationError`.
  - Duplicate `(dimension, key, window)` in `performance_attribution` raises `ValidationError`.

Out of scope:

- Computation logic — the Phase 1 write path pre-computes these aggregates on each thesis resolution per `state-persistence.md` Tier 3. This story owns only the read-side shape.
- Feedback-loop dashboard rendering (command-center / feedback-loop story sets).
- Cross-aggregate trend analysis (month-over-month calibration trends) — owned by the feedback-loop work tree.
- LLM-driven retrospective evaluation of resolved theses — separate feature.
- Repository / OMS reads (story 04b).

## Notes

`RegimeLabel` is canonically declared in `src/alphamind/portfolio_state/records/capital.py` (story 03d). This story imports it from there: `from alphamind.portfolio_state.records.capital import RegimeLabel`. If story 03d has not yet landed when this story is implemented, the implementer may declare a local `RegimeLabel` alias with the same four members (`LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`) and document the convergence target in the commit message. Migration to the canonical import is a follow-up with no interface change.

`signal_type` is typed `str` throughout rather than as a closed enum. `portfolio-state.md` § 6b names "unusual options activity, earnings revisions, prediction market shifts" as illustrative examples of signal types, not a complete taxonomy. No closed signal enumeration appears elsewhere in the design docs. Modelling `signal_type` as an opaque string preserves the ability to track new signal types as the analyst's signal vocabulary evolves, without requiring a schema change. Downstream consumers that render signal names for display should treat the string as opaque.

Per `feedback_avoid_numeric_anchors.md`, no field docstring imposes a threshold for what constitutes a "good" validation rate, hit rate, deviation rate, or attribution ratio. The records carry the data; downstream consumers (PM context rendering, feedback-loop dashboards) apply interpretive thresholds. The `None` sentinel for zero-divisor properties communicates absence of data without substituting a default value that would signal to downstream consumers that a threshold has been crossed.

Per `feedback_no_inventing_component_names.md`, every type name, enum member, and field name is sourced from `portfolio-state.md` § 6 and `state-persistence.md`. `TrailingWindow`'s three-member shape (FIVE_DAYS, TWENTY_DAYS, INCEPTION) matches the design's "trailing validation rate over 5d, 20d, inception windows" literally. `ThesisType`'s four members match portfolio-state.md § 6c "P/L by thesis type: realized P/L by nature (event-driven, mean-reversion, momentum, cross-asset divergence)" literally.

Per `feedback_per_producer_schema.md`, each metric is its own typed record rather than a nested dict. `ConvictionCalibrationEntry`, `SignalHitRate`, and `PerformanceAttributionEntry` are distinct types even though they share structural similarities. This preserves type safety as the metric set evolves and makes each record independently addressable in the `ThesisQualityAggregate` tuple fields.

The `*_by_window` tuples on `ThesisQualityAggregate` are ordered `FIVE_DAYS`, `TWENTY_DAYS`, `INCEPTION` by convention (documented in field docstrings). The window uniqueness validator enforces no duplicates but does not enforce ordering. Consumers should use `counts_for(window)` and the other helper methods rather than positional indexing to remain robust to ordering changes.

The `class_distribution` field on `InvalidationTimingStat` uses a `dict[InvalidationTimingClass, int]` rather than three named fields (`early_count`, `on_time_count`, `late_count`) to keep the distribution shape stable as `InvalidationTimingClass` evolves and to enable programmatic iteration over all classes. The exhaustiveness validator ensures all three members are always present as keys.

The config knob `thesis_quality_aggregates.trailing_windows_days: [5, 20]` in `portfolio_state.yaml` (story 02) drives the persistence layer's computation windows. The INCEPTION window has no day count — it covers all history. `TrailingWindow` models the three resulting slots; the mapping from config day counts to `TrailingWindow` members is the persistence layer's concern, not this story's.

## Acceptance criteria

- [ ] `TrailingWindow` defines exactly `FIVE_DAYS`, `TWENTY_DAYS`, `INCEPTION`.
- [ ] `ThesisType` defines exactly `EVENT_DRIVEN`, `MEAN_REVERSION`, `MOMENTUM`, `CROSS_ASSET_DIVERGENCE`.
- [ ] `InvalidationTimingClass` defines exactly `EARLY`, `ON_TIME`, `LATE`.
- [ ] `AttributionDimension` defines exactly `SECTOR`, `THESIS_TYPE`, `REGIME`.
- [ ] `RegimeLabel` is imported from `alphamind.portfolio_state.records.capital`; not redeclared (or, if story 03d is not yet landed, a local alias is declared with a convergence note in the commit message).
- [ ] `ResolutionWindowCounts`, `ThesisDurationStat`, `InvalidationTimingStat`, `SignalHitRate`, `SignalToThesisConversion`, `ConvictionCalibrationEntry`, `ConvictionSizingDeviation`, `PerformanceAttributionEntry`, `AlphaBetaDecomposition`, and `ThesisQualityAggregate` are frozen Pydantic v2 models with the documented field sets.
- [ ] `ResolutionWindowCounts` conservation invariant (`validated + profitable_but_wrong + invalidated_stopped_correctly + invalidated_wrong_on_exit + cancelled_never_entered == total_resolutions`) is enforced; violation raises `ValidationError`.
- [ ] `ConvictionSizingDeviation` conservation invariant (`above + below + within == total_proposals`) is enforced; violation raises `ValidationError`.
- [ ] All count fields reject negative values.
- [ ] `InvalidationTimingStat.class_distribution` rejects any mapping missing a `InvalidationTimingClass` member.
- [ ] `ConvictionCalibrationEntry.conviction_level` outside `{1, 2, 3, 4, 5}` raises `ValidationError`.
- [ ] All `*_pct` and explicitly-finite float fields reject NaN and ±Inf.
- [ ] `ThesisQualityAggregate.as_of_timestamp` without timezone raises `ValidationError`.
- [ ] Window uniqueness invariant enforced for each `*_by_window` tuple; duplicate raises `ValidationError`.
- [ ] `(signal_type, window)` uniqueness enforced in `signal_hit_rates` and `signal_to_thesis_conversions`; duplicate raises `ValidationError`.
- [ ] `(conviction_level, window)` uniqueness enforced in `conviction_calibration`; duplicate raises `ValidationError`.
- [ ] `(dimension, key, window)` uniqueness enforced in `performance_attribution`; duplicate raises `ValidationError`.
- [ ] `validation_rate`, `hit_rate`, `conversion_rate`, `deviation_rate`, `attribution_ratio` properties return `None` on zero-divisor inputs; no exception raised.
- [ ] `counts_for(window)` returns the matching `ResolutionWindowCounts` or `None`.
- [ ] `signal_hit_rate(signal_type, window)` returns the matching `SignalHitRate` or `None`.
- [ ] `conviction_entries_for(window)` returns the matching entries or empty tuple.
- [ ] Happy-path aggregate fixture builds without error and all helpers return the expected values.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
