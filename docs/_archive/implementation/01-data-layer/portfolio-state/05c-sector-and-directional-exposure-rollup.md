---
status: done
completed_date: 2026-04-27
commit_id: 70ecfd7
---

# 05c — Sector and directional exposure rollup

## Goal

Implement pure-function rollups for raw state category 1b: per-sector long/short exposure (in dollars and as portfolio percentage), net directional exposure, gross exposure, and long/short ratio per sector. Operates on already-enriched `PositionRecord` instances (story 05a populated `current_market_value_usd`, `notional_exposure_usd`, `delta_adjusted_exposure_usd`) plus an injected sector resolver. Uses delta-adjusted exposure for sector and directional rollups per `position-model.md` § Exposure calculations.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 1b — sector allocation (long and short per sector, dollars and percentage; uses delta-adjusted for options/strategy), net directional exposure (long minus short), gross exposure (long plus short), long/short ratio per sector
- `../../../design/05-execution-layer/position-model.md` § Exposure calculations: delta-adjusted — the design's authoritative formulation: equity uses notional and delta-adjusted identically; options use `contract_count × multiplier × delta × underlying_price`; strategy uses `strategy_greeks.delta`
- `02-package-skeleton-and-config.md` — package layout (`computations/exposure.py` is this story's target)
- `03a-position-records.md` — `PositionRecord`, `Direction`, `InstrumentType`, instrument-detail types
- `05a-position-computations.md` — sibling pattern for pure-function structure; relies on its `delta_adjusted_exposure_usd` enrichment

## Depends on

- 02
- 03a (consumes `PositionRecord` and `Direction`)
- 04a (imports `SectorExposureEntry` and `DirectionalExposure` from `snapshot.py`; this story produces values of these types but does not declare them)

## Scope

In scope, all under `src/alphamind/portfolio_state/computations/exposure.py` — pure functions with no I/O. Tests at `tests/portfolio_state/computations/test_exposure.py`.

### 1. Sector resolver type

```python
SectorResolver = Callable[[PositionRecord], str | None]
```

A callable injected by the assembler that returns the position's sector label as a string, or `None` when the position's underlying is unclassified (e.g., new ticker not yet in `sector_classification`). Functions in this module take `SectorResolver` as an argument; this story does not own the resolver implementation.

The sector label is typed `str` to keep this story's dependency graph closed and to avoid premature coupling to a `Sector` enum that lives in the analysis layer. When a canonical `Sector` enum stabilises across the codebase, the resolver's return type can tighten without changing this module's public API.

### 2. Per-sector exposure rollup

`SectorExposureEntry` is declared in `alphamind.portfolio_state.snapshot` (story 04a). This story imports it; it does not redeclare. Field shape per 04a § 1b.

`compute_sector_exposure(open_positions: tuple[PositionRecord, ...], resolver: SectorResolver, total_portfolio_value_usd: float) -> tuple[SectorExposureEntry, ...]`

Behavior:
- Iterates `open_positions`. For each, calls `resolver(position)`; positions where the resolver returns `None` are aggregated under the sentinel sector `"UNCLASSIFIED"` so they remain visible without crashing the rollup.
- Groups positions by sector. For each sector group, sums delta-adjusted exposure split by direction (LONG into long bucket, SHORT into short bucket using `abs()`).
- Returns one `SectorExposureEntry` per sector with at least one position. Sectors with zero positions are absent from the tuple (not zero-filled).
- Ordering: by sector label ascending (string sort; `"UNCLASSIFIED"` sorts naturally to its alphabetical position).

Validation:
- `total_portfolio_value_usd >= 0` — negative raises `ValueError`.
- Every position's `delta_adjusted_exposure_usd` must be non-`None` (story 05a's enrichment is mandatory before this rollup); else raise `ValueError` naming the position_id.

### 3. Directional and gross exposure

`DirectionalExposure` is declared in `alphamind.portfolio_state.snapshot` (story 04a). This story imports it; it does not redeclare. Field shape per 04a § 1c.

`compute_directional_exposure(open_positions: tuple[PositionRecord, ...], total_portfolio_value_usd: float) -> DirectionalExposure`

Behavior:
- `total_long_delta_adjusted_usd = sum(p.delta_adjusted_exposure_usd for p in open_positions if p.direction == Direction.LONG)` — note: for options where the greek delta has a sign opposite the position direction (e.g., long put has negative delta), the value can be negative; this rollup sums the signed values per the design's convention that delta-adjusted exposure carries directional information.

  Wait — reconsider. Per story 05a, `delta_adjusted_exposure_usd` for short equity is negative (`-share_count × price`). For long puts (a LONG position whose option delta is negative), the value is negative as well. The semantic is "directional risk contribution," not "magnitude in the long bucket."

  So a clearer rollup:
- `total_long_delta_adjusted_usd = sum(p.delta_adjusted_exposure_usd for p in open_positions if p.delta_adjusted_exposure_usd > 0)` — positions whose delta-adjusted exposure is *directionally long* (positive signed). This includes long calls, long equity, short puts (positive delta).
- `total_short_delta_adjusted_usd = sum(-p.delta_adjusted_exposure_usd for p in open_positions if p.delta_adjusted_exposure_usd < 0)` — magnitude of directionally-short contribution. Includes short equity, long puts (negative delta), short calls.
- `net_directional_pct_of_portfolio = ((total_long_delta_adjusted_usd - total_short_delta_adjusted_usd) / total_portfolio_value_usd) * 100`; `0.0` when total is zero.
- `gross_pct_of_portfolio = ((total_long_delta_adjusted_usd + total_short_delta_adjusted_usd) / total_portfolio_value_usd) * 100`; `0.0` when total is zero.

Validation:
- `total_portfolio_value_usd >= 0` — negative raises `ValueError`.
- Every position's `delta_adjusted_exposure_usd` must be non-`None`.

### 4. Tests

- `compute_sector_exposure` happy path: three positions across two sectors (e.g., NVDA tech long, AMD tech short, JPM financials long); resolver maps tickers to sectors. Verify the returned tuple has two entries (`"financials"`, `"tech"`), correctly summed long/short exposure, percentages computed from `total_portfolio_value_usd`, and `long_short_ratio` matching expected.
- `compute_sector_exposure` resolver returning `None`: an unclassified ticker is aggregated under `"UNCLASSIFIED"`; verify the entry exists and its values.
- `compute_sector_exposure` empty portfolio: `()` and any total → returns `()` (empty tuple).
- `compute_sector_exposure` with `total_portfolio_value_usd == 0` → percentages are `0.0`; no zero-division exception.
- `compute_sector_exposure` with negative total → `ValueError`.
- `compute_sector_exposure` with a position whose `delta_adjusted_exposure_usd is None` → `ValueError` naming position_id.
- `compute_sector_exposure` ordering: returned entries ordered by sector label ascending (verified across multi-sector fixture).
- `compute_sector_exposure` `long_short_ratio`: `None` when sector has only long positions or only short positions; finite when both present.
- `compute_directional_exposure` happy path: portfolio with two long-positive positions and one short-positive position; verify net, gross, totals match documented formulas.
- `compute_directional_exposure` long put case: position has `Direction.LONG` but `delta_adjusted_exposure_usd < 0` (negative-delta option); verify the value is included in the short bucket per the sign-based bucket assignment.
- `compute_directional_exposure` empty portfolio → all zeros.
- `compute_directional_exposure` `total_portfolio_value_usd == 0` → percentages `0.0`, no exception.
- `compute_directional_exposure` negative total → `ValueError`.
- `compute_directional_exposure` unenriched position (`delta_adjusted_exposure_usd is None`) → `ValueError`.
- Determinism: identical inputs → identical outputs across repeated calls.

Out of scope:
- Beta-adjusted exposure (derived metric 7a — distillation layer; requires beta data the data layer collects but this story does not consume).
- Position correlation profile (derived metric 7b).
- Sector ETF correlation cross-reference.
- Per-position contribution to drawdown (story 05b).
- Risk budget consumption against sector limits (story 05d).
- The Sector taxonomy itself (which sectors exist, their canonical names) — owned by the analysis layer's domain-researcher inventory; this story treats sector labels as opaque strings.
- Sector classification lookup — a future story implements `SectorResolver` against `sector_classification` rows (`src/alphamind/persistence/models.py`); this story takes the resolver as injected.

## Notes

The bucket assignment for directional exposure rolls up by *sign of delta-adjusted exposure*, not by `Direction`. This matches the design's intent (`portfolio-state.md` § 1b) that delta-adjusted exposure is the basis: a long put position is directionally short despite carrying `Direction.LONG`, and a short call is directionally long despite `Direction.SHORT`. Bucketing by sign captures the directional risk correctly.

Per-sector bucket assignment uses `Direction` (not sign of delta-adjusted exposure) because the design's framing of sector concentration in `portfolio-state.md` § 1b — "long and short per sector" — is at the position level, not the directional-exposure level. A long put in tech is reported as a long tech position with negative delta-adjusted exposure inside the long bucket. Consumers reading the per-sector entries reconcile the magnitude vs. direction interplay through the long/short totals plus net directional exposure.

Note: this differs from the directional-exposure rollup intentionally. The reason is that consumers want to answer *both* questions: (a) "how concentrated am I in tech?" (per-sector, by `Direction`) and (b) "how much net directional risk do I carry?" (sign-based). Mixing the conventions inside one function would make per-sector results confusing for hedged positions.

Per `feedback_avoid_numeric_anchors.md`, no field carries threshold-style guidance. The structurally-defined buckets are the only classification.

Per `feedback_simplify_before_building.md`, two functions and two value objects in one module — no helper class hierarchy.

Per `feedback_no_inventing_component_names.md`, "sector exposure," "directional exposure," "gross exposure" come from `portfolio-state.md` § 1b literally. `SectorResolver` and `SectorExposureEntry`/`DirectionalExposure` are descriptive new names; the "Resolver" suffix matches the dependency-injection idiom used elsewhere (e.g., synthesizer's `PortfolioStateReader`).

The `"UNCLASSIFIED"` sentinel keeps the rollup robust to new tickers that haven't been classified yet. The sentinel is uppercase to be visually distinct from real sector labels (which are typically lowercase per existing `sector_classification` rows). Operators triaging an unclassified ticker can grep for `"UNCLASSIFIED"` to find the affected positions.

## Acceptance criteria

- [ ] `SectorResolver` type alias is declared as `Callable[[PositionRecord], str | None]`.
- [ ] `SectorExposureEntry` and `DirectionalExposure` are imported from `alphamind.portfolio_state.snapshot`; not redeclared here.
- [ ] `compute_sector_exposure` happy path returns the documented per-sector entries with correct long/short sums, percentages, and `long_short_ratio`.
- [ ] `compute_sector_exposure` aggregates `None`-resolver positions under `"UNCLASSIFIED"`.
- [ ] `compute_sector_exposure` returns entries ordered by sector label ascending.
- [ ] `compute_sector_exposure` returns `()` for empty open positions.
- [ ] `compute_sector_exposure` percentages are `0.0` when `total_portfolio_value_usd == 0`; no zero-division exception.
- [ ] `compute_sector_exposure` raises `ValueError` for negative `total_portfolio_value_usd`.
- [ ] `compute_sector_exposure` raises `ValueError` (with position_id) for unenriched `delta_adjusted_exposure_usd`.
- [ ] `compute_sector_exposure` `long_short_ratio` is `None` when the short bucket is zero; finite otherwise.
- [ ] `compute_directional_exposure` returns documented net/gross percentages and totals; bucket assignment uses sign of `delta_adjusted_exposure_usd`.
- [ ] `compute_directional_exposure` long put (negative delta-adjusted) is bucketed into the short total.
- [ ] `compute_directional_exposure` empty portfolio returns all zeros without exception.
- [ ] `compute_directional_exposure` raises `ValueError` for negative `total_portfolio_value_usd`.
- [ ] `compute_directional_exposure` raises `ValueError` (with position_id) for unenriched positions.
- [ ] All functions are deterministic across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
