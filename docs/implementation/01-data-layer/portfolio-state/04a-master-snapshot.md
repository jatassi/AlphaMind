---
status: in_progress
completed_date:
commit_id:
---

# 04a — Master snapshot

## Goal

Define `PortfolioStateSnapshot` — the frozen Pydantic v2 aggregate that collects everything raw state categories 1–6 deliver — at `src/alphamind/portfolio_state/snapshot.py`, with a corresponding test suite at `tests/portfolio_state/test_snapshot.py`. The snapshot is read once per invocation (after Phase 1 commits, before the analysis pipeline runs) and is shared across all downstream consumers: synthesizer, analyst, strategist, and PM. Per-consumer view projections (story 07) operate on this snapshot.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — the full feature spec; the section structure (categories 1–6) drives the snapshot's field map
- `../../../design/01-data-layer/internal/README.md` — consumer / cadence map
- `../../../design/05-execution-layer/state-persistence.md` § Read paths § Invocation snapshot — the snapshot read contract; the consumption mode is "a consistent point-in-time read of all data the ingestion layer needs to assemble raw state categories 1–6"
- `02-package-skeleton-and-config.md` — package layout (`snapshot.py` is this story's target)
- `03a-position-records.md` — `PositionRecord`
- `03b-thesis-records.md` — `ThesisRecord`, `RecentThesisResolution`
- `03c-order-and-bracket-records.md` — `OrderRecord`, `BracketRecord`
- `03d-capital-state-records.md` — `CashLedger`, `DrawdownState`, `RiskBudgetConsumption`, `ActiveRiskParameterSet`
- `03e-activity-log-records.md` — `ActivityLogEntry`, `EventType`
- `03f-thesis-quality-aggregate-records.md` — `ThesisQualityAggregate`
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern (frozen value objects, validation, AC style)

## Depends on

- 02
- 03a
- 03b
- 03c
- 03d
- 03e
- 03f

## Scope

In scope:

### 1. Snapshot-time rollup types (declared in `snapshot.py`)

These are typed values produced by the computation layer (stories 05b, 05c) at snapshot-assembly time. They live in `snapshot.py` rather than in `records/` because they are derived rollups, not OMS-state entities. Computation of their values is owned by the corresponding 05* stories; this story owns the type declarations.

#### 1a. `PortfolioPnL` — snapshot-time P/L rollup

A frozen Pydantic v2 model. Fields, all sourced from `portfolio-state.md` § 2b:

- `total_unrealized_pnl_usd: float` — sum of unrealized P/L across all open positions in absolute dollars
- `total_unrealized_pnl_pct_of_portfolio: float` — unrealized P/L as a percentage of total portfolio value
- `daily_realized_pnl_usd: float` — realized P/L locked in from positions closed today
- `daily_total_pnl_usd: float` — realized plus unrealized; the running daily score
- `cumulative_realized_pnl_usd: float` — lifetime realized track record
- `rolling_realized_pnl: dict[Literal["1d", "3d", "5d", "20d"], float]` — trailing window realized P/L per `portfolio-state.md` § 2b "Rolling P/L windows: trailing 1d, 3d, 5d, 20d realized"; each key is always present
- `win_rate_pct: float | None` — win rate from resolved theses; `None` when no resolved theses exist yet
- `average_win_size_usd: float | None` — average P/L of winning trades; `None` when no wins
- `average_loss_size_usd: float | None` — average P/L (magnitude) of losing trades; `None` when no losses
- `profit_factor: float | None` — total gains divided by total losses; `None` when total losses are zero

#### 1b. `SectorExposureEntry` — per-sector long/short rollup (raw state 1b)

A frozen Pydantic v2 model. One entry per sector with at least one open position; sectors with zero positions are absent from the snapshot's `sector_exposure` tuple.

- `sector: str` — sector label as resolved by the assembler's sector resolver; the sentinel `"UNCLASSIFIED"` is used for positions whose underlying has no sector classification
- `long_delta_adjusted_usd: float` — non-negative; sum of delta-adjusted exposure for `Direction.LONG` positions in this sector
- `short_delta_adjusted_usd: float` — non-negative magnitude; sum of `abs(delta_adjusted_exposure_usd)` for `Direction.SHORT` positions
- `long_pct_of_portfolio: float` — `(long_delta_adjusted_usd / total_portfolio_value_usd) * 100`; `0.0` when total is zero
- `short_pct_of_portfolio: float` — analogous
- `long_short_ratio: float | None` — `long / short` when `short > 0`; `None` when `short == 0`

#### 1c. `DirectionalExposure` — portfolio-level directional rollup (raw state 1b)

A frozen Pydantic v2 model. Bucket assignment is by sign of `delta_adjusted_exposure_usd` (a long put is bucketed short despite `Direction.LONG`); see story 05c notes for the rationale.

- `total_long_delta_adjusted_usd: float` — non-negative; sum of `delta_adjusted_exposure_usd` over positions where the value is positive
- `total_short_delta_adjusted_usd: float` — non-negative magnitude
- `net_directional_pct_of_portfolio: float` — signed; positive net long, negative net short
- `gross_pct_of_portfolio: float` — non-negative

### 2. `PortfolioStateSnapshot` — master frozen aggregate

A frozen Pydantic v2 model. The category structure (1–6) organises the fields for human comprehension; the shape is flat (one type, no category-specific sub-objects) per `feedback_simplify_before_building.md`.

**Identity / scaffolding:**

- `invocation_id: str` — the pipeline invocation this snapshot was assembled for; non-empty
- `phase1_committed_at: datetime` — tz-aware UTC; the moment Phase 1's transaction commit completed, per `state-persistence.md` "The transaction commits before the snapshot read"
- `snapshot_assembled_at: datetime` — tz-aware UTC; when the assembler (story 06) finished building this record
- `pipeline_invocation_started_at: datetime | None` — tz-aware UTC when non-`None`; for invocation-level latency tracking; `None` until the pipeline reports it

**Category 1 — Position inventory:**

- `open_positions: tuple[PositionRecord, ...]` — all positions with `status == OPEN`; ordered by `position_id` ascending for stable consumption
- `pending_positions: tuple[PositionRecord, ...]` — all positions with `status == PENDING`; ordered by `position_id` ascending
- `sector_exposure: tuple[SectorExposureEntry, ...]` — per-sector rollup over `open_positions` (raw state 1b sector allocation); ordered by sector label ascending; sectors with zero positions are omitted
- `directional_exposure: DirectionalExposure` — portfolio-level net / gross / long-side / short-side rollup (raw state 1b net directional and gross exposure); always present (zero-portfolio yields all-zero values)

**Category 2 — P/L and performance:**

- `portfolio_pnl: PortfolioPnL` — snapshot-time P/L rollup (defined in this file)
- `drawdown: DrawdownState` — current drawdown state from `records/capital.py`

**Category 3 — Thesis registry:**

- `active_theses: tuple[ThesisRecord, ...]` — theses with `status == ACTIVE`; component-level detail included; consumers project to summary level via story 07
- `recent_thesis_resolutions: tuple[RecentThesisResolution, ...]` — rolling window per `thesis_resolutions.lookback_trading_days` config

**Category 4 — Capital and capacity:**

- `cash_ledger: CashLedger`
- `pending_orders: tuple[OrderRecord, ...]` — orders with `status` in `{PENDING, PARTIALLY_FILLED}`; ordered by `submission_timestamp` ascending
- `risk_budget: RiskBudgetConsumption`
- `active_risk_parameters: ActiveRiskParameterSet`

**Category 5 — Activity log:**

- `intra_invocation_changelog: tuple[ActivityLogEntry, ...]` — entries with `invocation_id == self.invocation_id`; chronological
- `recent_pm_decision_log: tuple[ActivityLogEntry, ...]` — entries with `event_type == EventType.PM_DECISION` from the most recent N invocations per `pm_decision_log.sliding_window_invocations` config; chronological
- `position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]` — keyed by `position_id`; chronological per position; keys are the union of position IDs from `open_positions` and `pending_positions`

**Category 6 — Thesis quality trends:**

- `thesis_quality_aggregates: ThesisQualityAggregate`

**Cross-cutting brackets reference:**

- `brackets: tuple[BracketRecord, ...]` — one per open or pending position, the bracket bound to that position; ordered by `bracket_id` ascending. Surfaced at top level rather than embedded in `PositionRecord` so consumers projecting to position-summary views do not pay the bracket-loading cost.

### 3. Helper methods on `PortfolioStateSnapshot`

Implemented as `@property` methods or regular methods returning derived projections. Pure function, no I/O. All are O(N) over small N; caching is deferred to profiling.

- `position_by_id(self, position_id: str) -> PositionRecord | None` — searches both `open_positions` and `pending_positions`; returns `None` when not found
- `bracket_for_position(self, position_id: str) -> BracketRecord | None` — returns the `BracketRecord` whose `position_id` matches; `None` when not found
- `active_thesis_for_position(self, position_id: str) -> ThesisRecord | None` — returns the `ThesisRecord` from `active_theses` whose `position_id` matches; `None` when not found
- `pending_orders_for_position(self, position_id: str) -> tuple[OrderRecord, ...]` — returns all `OrderRecord` entries from `pending_orders` whose `position_id` matches; empty tuple when none found

### 4. Validation rules (Pydantic `model_validator`)

All validators are structural — cross-reference integrity, status alignment, ordering invariants. They are not semantic (e.g., "P/L is consistent with positions" — that is the assembler's correctness, not this type's invariants).

- **Status alignment — open positions:** every `PositionRecord` in `open_positions` has `status == PositionStatus.OPEN`; violation raises `ValidationError`.
- **Status alignment — pending positions:** every `PositionRecord` in `pending_positions` has `status == PositionStatus.PENDING`; violation raises `ValidationError`.
- **Pending order status:** every `OrderRecord` in `pending_orders` has `status` in `{OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED}`; violation raises `ValidationError`.
- **Orphan-free brackets:** for every `BracketRecord` in `brackets`, `position_by_id(bracket.position_id)` must resolve (the referenced position exists in `open_positions` or `pending_positions`); an unresolvable reference raises `ValidationError`.
- **Scoped position modification trail:** for every `position_id` key and every `ActivityLogEntry` in the associated tuple within `position_modification_trail`, `position_by_id(position_id)` must resolve; an unresolvable key raises `ValidationError`.
- **Intra-invocation changelog scoping:** all entries in `intra_invocation_changelog` must carry `invocation_id == self.invocation_id`; any mismatch raises `ValidationError`.
- **Timestamp ordering:** `phase1_committed_at <= snapshot_assembled_at`; violation raises `ValidationError`.
- **Pipeline start timestamp ordering:** when `pipeline_invocation_started_at` is non-`None`, it must be `>= snapshot_assembled_at`; violation raises `ValidationError`.
- **PM decision log event type:** all entries in `recent_pm_decision_log` must have `event_type == EventType.PM_DECISION`; violation raises `ValidationError`.
- **Position ID uniqueness:** `position_id` values are unique across `open_positions ∪ pending_positions`; duplicates raise `ValidationError`.
- **Bracket ID uniqueness:** `bracket_id` values are unique across `brackets`; duplicates raise `ValidationError`.
- **Invocation ID non-empty:** `invocation_id` is non-empty; enforced via `Field(min_length=1)` or equivalent validator.
- **Timestamp timezone awareness:** `phase1_committed_at`, `snapshot_assembled_at`, and `pipeline_invocation_started_at` (when non-`None`) must be tz-aware UTC datetimes; naive datetimes raise `ValidationError`.

### 5. Tests at `tests/portfolio_state/test_snapshot.py`

**Happy-path fixture:**

Build a `PortfolioStateSnapshot` with:
- At least 2 open positions (equity, ordered by `position_id`)
- At least 1 pending position
- At least 1 active thesis linked to one of the open positions
- At least 1 pending order linked to one of the open positions
- At least 1 `BracketRecord` per open and pending position
- A few `ActivityLogEntry` items covering the `intra_invocation_changelog` and `position_modification_trail` for at least one position
- A populated `ThesisQualityAggregate`
- `portfolio_pnl` with all rolling window keys present; non-`None` `win_rate_pct`

Verify on the happy-path fixture:
- `position_by_id` returns the correct `PositionRecord` for an open position ID, a pending position ID, and `None` for an unknown ID.
- `bracket_for_position` returns the correct `BracketRecord` for a known position ID and `None` for an unknown ID.
- `active_thesis_for_position` returns the correct `ThesisRecord` for a position that has one and `None` for a position without one.
- `pending_orders_for_position` returns the correct subset for a position with pending orders, an empty tuple for a position with none, and an empty tuple for an unknown position ID.
- All validation rules pass without error.

**Failing fixtures — one per validation rule:**

Each must produce a `ValidationError` with a clear path identifying the violating field or entry. At minimum:

- Open position with `status != OPEN` raises `ValidationError`.
- Pending position with `status != PENDING` raises `ValidationError`.
- Pending order with `status` not in `{PENDING, PARTIALLY_FILLED}` raises `ValidationError`.
- Bracket whose `position_id` does not resolve in `open_positions ∪ pending_positions` raises `ValidationError`.
- `position_modification_trail` entry keyed on a `position_id` not in `open_positions ∪ pending_positions` raises `ValidationError`.
- `intra_invocation_changelog` entry with `invocation_id` mismatching `self.invocation_id` raises `ValidationError`.
- `phase1_committed_at > snapshot_assembled_at` raises `ValidationError`.
- `pipeline_invocation_started_at < snapshot_assembled_at` (when non-`None`) raises `ValidationError`.
- `recent_pm_decision_log` entry with `event_type != EventType.PM_DECISION` raises `ValidationError`.
- Duplicate `position_id` across `open_positions ∪ pending_positions` raises `ValidationError`.
- Duplicate `bracket_id` across `brackets` raises `ValidationError`.
- Empty `invocation_id` raises `ValidationError`.
- Naive (tz-unaware) `phase1_committed_at` raises `ValidationError`.

**Immutability:**

- Attempting to assign to any field on a fully-constructed `PortfolioStateSnapshot` instance raises (Pydantic `ValidationError` or `AttributeError`, per the frozen model's behavior).

**Deep copy equality:**

- `copy.deepcopy(snapshot) == snapshot` is `True`.

**Empty portfolio:**

- A `PortfolioStateSnapshot` with empty tuples for `open_positions`, `pending_positions`, `brackets`, `pending_orders`, `active_theses`, `recent_thesis_resolutions`, `intra_invocation_changelog`, `recent_pm_decision_log`, and an empty `position_modification_trail` dict constructs successfully without `ValidationError`. This is a valid initial state before any positions are opened.

Out of scope:

- The snapshot assembler (story 06).
- Per-consumer view projections (story 07).
- Freshness / staleness reporting (story 08).
- Repository read protocols (story 04b).
- Per-record validation (already enforced by stories 03a–03f).

## Notes

`PortfolioPnL` is declared in `snapshot.py` rather than `records/capital.py` because it is a snapshot-time derived rollup, not an OMS-state entity. `records/capital.py` owns OMS-state records (`CashLedger`, `DrawdownState`, `RiskBudgetConsumption`, `ActiveRiskParameterSet`); rollups live with the snapshot that consumes them.

Brackets are surfaced at the top level of `PortfolioStateSnapshot` rather than embedded inside `PositionRecord`. Consumers projecting to position-summary views (the synthesizer's `PositionSummary`) do not need bracket detail; surfacing brackets top-level avoids loading them when consumers project to those views. Lookup via `bracket_for_position(position_id)` is O(N) over the `brackets` tuple. N is bounded by the number of open and pending positions, which is small in normal operation. Caching can be added transparently inside the method if profiling warrants it.

`position_modification_trail` is `dict[str, tuple[ActivityLogEntry, ...]]` rather than a flat tuple because consumers (the strategist's per-position evaluation loop) repeatedly look up entries scoped to one position. Pre-grouping at snapshot time avoids N filter passes downstream per `portfolio-state.md` § 5c "For each open position, activity log entries scoped to that position ID."

The snapshot does not carry freshness / staleness flags. Those live on `freshness.py` (story 08). The snapshot is the data-shape contract; freshness reporting is its own seam.

Per `feedback_simplify_before_building.md`, the snapshot is one type with helper methods rather than a layered hierarchy with category-specific sub-objects. The category groupings are for documentation; the runtime shape is flat.

Per `feedback_no_inventing_component_names.md`, every field name aligns with `portfolio-state.md`'s category vocabulary and `state-persistence.md`'s invocation snapshot section. No new field names are introduced.

Per `feedback_avoid_numeric_anchors.md`, no field docstrings impose threshold-style interpretations. The snapshot carries the data; downstream interpretation belongs to consumers.

The `rolling_realized_pnl` dict on `PortfolioPnL` uses `Literal["1d", "3d", "5d", "20d"]` keys matching `portfolio-state.md` § 2b "trailing 1d, 3d, 5d, 20d realized" exactly. All four keys are always present (the assembler populates zero when no data is available); consumers may treat an absent key as a type error.

`pipeline_invocation_started_at` is `None` at snapshot assembly time and is populated by the pipeline runtime when it begins execution. The type carries the field as nullable from the start so the same type serves both assembly-time and mid-pipeline consumers without a separate mutable wrapper.

`recent_pm_decision_log` contains entries from the most recent N invocations, where N is `pm_decision_log.sliding_window_invocations` from `config/portfolio_state.yaml`. The snapshot carries the materialized window; the assembler handles the N-invocation query.

## Acceptance criteria

- [ ] `PortfolioPnL` is a frozen Pydantic v2 model with the documented fields; `rolling_realized_pnl` is typed `dict[Literal["1d", "3d", "5d", "20d"], float]`.
- [ ] `SectorExposureEntry` is a frozen Pydantic v2 model with the documented fields; `long_pct_of_portfolio`, `short_pct_of_portfolio` accept zero values; `long_short_ratio` is `None` when `short_delta_adjusted_usd == 0`.
- [ ] `DirectionalExposure` is a frozen Pydantic v2 model with the documented fields; `gross_pct_of_portfolio >= 0` enforced; bucket-total fields are non-negative.
- [ ] `PortfolioStateSnapshot` is a frozen Pydantic v2 model with all top-level fields from all categories (1–6 plus brackets) and identity / scaffolding fields, including `sector_exposure` and `directional_exposure`.
- [ ] `position_by_id(position_id)` returns the correct `PositionRecord` from `open_positions` or `pending_positions`, or `None` when not found — tested on happy-path and miss cases.
- [ ] `bracket_for_position(position_id)` returns the correct `BracketRecord`, or `None` when not found.
- [ ] `active_thesis_for_position(position_id)` returns the correct `ThesisRecord`, or `None` when not found.
- [ ] `pending_orders_for_position(position_id)` returns the correct subset of `pending_orders`, or an empty tuple when none match.
- [ ] Status-alignment validator: open-position `status != OPEN` raises; pending-position `status != PENDING` raises — each has a passing and failing test.
- [ ] Pending-order status validator: `status` not in `{PENDING, PARTIALLY_FILLED}` raises — has a passing and failing test.
- [ ] Orphan-bracket validator: bracket whose `position_id` does not resolve raises — has a passing and failing test.
- [ ] Position-modification-trail key validator: an unresolvable `position_id` key raises — has a passing and failing test.
- [ ] Intra-invocation changelog scoping validator: mismatched `invocation_id` on any entry raises — has a passing and failing test.
- [ ] Timestamp ordering validator: `phase1_committed_at > snapshot_assembled_at` raises — has a passing and failing test.
- [ ] Pipeline start timestamp validator: non-`None` `pipeline_invocation_started_at < snapshot_assembled_at` raises — has a passing and failing test.
- [ ] PM decision log event-type validator: non-`PM_DECISION` entry raises — has a passing and failing test.
- [ ] Position ID uniqueness validator: duplicate `position_id` across `open_positions ∪ pending_positions` raises — has a passing and failing test.
- [ ] Bracket ID uniqueness validator: duplicate `bracket_id` raises — has a passing and failing test.
- [ ] Empty `invocation_id` raises.
- [ ] Naive (tz-unaware) `phase1_committed_at` raises.
- [ ] Empty-portfolio snapshot (all tuples / dicts empty) constructs without error.
- [ ] Mutating any field on a constructed snapshot raises.
- [ ] `copy.deepcopy(snapshot) == snapshot` is `True`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
