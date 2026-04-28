---
status: not_started
completed_date:
commit_id:
---

# 08 — Freshness contract and staleness reporting

## Goal

Define `SnapshotFreshness` — a typed sidecar to `PortfolioStateSnapshot` reporting how fresh the snapshot's data is. Implement the pure-function helpers that compute it from the snapshot plus the assembler's per-position price-fetch outcomes. Update the assembler's return contract to deliver an `AssembledSnapshot` bundle of `(snapshot, freshness)` so downstream consumers can ask "is this position's market value reliable?" without re-deriving the answer. The contract surfaces the design's freshness invariants (`state-persistence.md § Snapshot isolation`, `internal/README.md`'s mark-to-market freshness note) into a typed object the pipeline and command-center can observe.

## Reading

- `../../../design/01-data-layer/internal/README.md` — note: "The only residual freshness concern is mark-to-market pricing on open positions, which depends on quant category 1 freshness." This story names the residual freshness signals.
- `../../../design/05-execution-layer/state-persistence.md` § Snapshot isolation — the read-isolation contract; defines `phase1_committed_at` as the moment all integrations are durable
- `../../../design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload § Freshness guarantee — "State is computed once per invocation at Phase 1 start. All downstream consumers see the same snapshot."
- `02-package-skeleton-and-config.md` — `freshness.py` is this story's target; `snapshot_freshness.max_phase1_to_snapshot_seconds` and `snapshot_freshness.max_price_age_seconds` are the thresholds this story consumes
- `03a-position-records.md` — `PositionRecord` (whose price-staleness this story aggregates)
- `03g-current-price-provider-protocol.md` — `PriceQuote.is_stale`, `PriceSource.STALE_FALLBACK` — the upstream signals
- `04a-master-snapshot.md` — `PortfolioStateSnapshot.phase1_committed_at`, `snapshot_assembled_at`
- `06-snapshot-assembler.md` — the assembler that produces the snapshot; this story refines its return contract to include freshness alongside

## Depends on

- 02
- 03a (consumes `PositionRecord`)
- 04a (consumes `PortfolioStateSnapshot`)
- 06 (refines its return shape — see Notes; the implementer of this story updates the assembler's return type)

## Scope

In scope, all under `src/alphamind/portfolio_state/freshness.py`. Tests at `tests/portfolio_state/test_freshness.py`.

### 1. `PriceFetchOutcomes` — the assembler's per-position price-fetch record

A frozen Pydantic v2 value object the assembler produces during Step 5/6 of `assemble_snapshot` (story 06). Records what happened during pricing for each position, so freshness reporting is computable without re-invoking the price provider.

- `position_ids_priced_fresh: frozenset[str]` — positions whose pricing-ticker resolved to a fresh `PriceQuote` (`is_stale=False`)
- `position_ids_priced_stale: frozenset[str]` — positions whose pricing-ticker resolved but `PriceQuote.is_stale=True` (the provider returned a quote older than the freshness threshold)
- `position_ids_unknown_ticker: frozenset[str]` — positions whose pricing-ticker was absent from the `get_quotes` result; the assembler fell back to a `STALE_FALLBACK` sentinel quote
- `oldest_price_as_of: datetime | None` — the oldest `PriceQuote.as_of_timestamp` across all positions; `None` only when there are zero positions

Validation:
- The three position-ID sets are pairwise disjoint (a position is in exactly one category).
- `oldest_price_as_of` is tz-aware UTC when not `None`.

### 2. `SnapshotFreshness` — the sidecar typed report

A frozen Pydantic v2 value object the pipeline observes alongside the snapshot.

Top-level invocation timing fields:
- `phase1_committed_at: datetime` — copied from `snapshot.phase1_committed_at`
- `snapshot_assembled_at: datetime` — copied from `snapshot.snapshot_assembled_at`
- `phase1_to_snapshot_seconds: float` — `(snapshot_assembled_at - phase1_committed_at).total_seconds()`; non-negative
- `max_phase1_to_snapshot_seconds: float` — the threshold from `PortfolioStateConfig.snapshot_freshness_max_phase1_to_snapshot_seconds`
- `phase1_to_snapshot_within_threshold: bool` — `phase1_to_snapshot_seconds <= max_phase1_to_snapshot_seconds`

Price-freshness aggregate fields:
- `total_open_positions: int` — `len(snapshot.open_positions)`
- `total_pending_positions: int` — `len(snapshot.pending_positions)`
- `total_positions: int` — sum of the above
- `position_ids_priced_fresh: frozenset[str]`
- `position_ids_priced_stale: frozenset[str]`
- `position_ids_unknown_ticker: frozenset[str]`
- `count_priced_fresh: int`, `count_priced_stale: int`, `count_unknown_ticker: int` — derived counters for at-a-glance dashboarding
- `all_position_prices_fresh: bool` — `count_priced_stale == 0 and count_unknown_ticker == 0`
- `oldest_price_as_of: datetime | None`
- `oldest_price_age_seconds: float | None` — `(snapshot_assembled_at - oldest_price_as_of).total_seconds()` when `oldest_price_as_of is not None`; `None` when no positions
- `max_price_age_seconds: float` — the threshold from `PortfolioStateConfig.snapshot_freshness_max_price_age_seconds`

Convenience helpers (`@property` methods):
- `is_position_price_stale(self, position_id: str) -> bool` — `position_id in (position_ids_priced_stale | position_ids_unknown_ticker)`
- `staleness_summary(self) -> str` — short human-readable line of the form `"phase1→snapshot 2.3s (within 30.0s); 12/13 positions priced fresh; 1 stale (POS-NVDA-001)"`. The string format is fixed and tested.

Validation:
- `phase1_to_snapshot_seconds >= 0`.
- `total_positions == total_open_positions + total_pending_positions`.
- `count_priced_fresh + count_priced_stale + count_unknown_ticker == total_positions`.
- The three frozensets are pairwise disjoint.
- `oldest_price_as_of` tz-aware UTC when not `None`; `oldest_price_as_of <= snapshot_assembled_at` when not `None`.
- `oldest_price_age_seconds is None` if and only if `oldest_price_as_of is None`.

### 3. `compute_snapshot_freshness` — the pure function

```python
def compute_snapshot_freshness(
    snapshot: PortfolioStateSnapshot,
    *,
    fetch_outcomes: PriceFetchOutcomes,
    config: PortfolioStateConfig,
) -> SnapshotFreshness:
    ...
```

Builds the `SnapshotFreshness` from the snapshot's timestamps, the per-position fetch outcomes, and the config thresholds. Pure function, no I/O.

Validation at function entry:
- `fetch_outcomes`'s position-ID sets are subsets of `{p.position_id for p in snapshot.open_positions ∪ snapshot.pending_positions}`. A position ID in the fetch outcomes that is not in the snapshot raises `ValueError` with the offending ID — this catches assembler / freshness-input drift.
- The union of the three sets equals the snapshot's full position-ID set. A position in the snapshot but not in any of the three fetch-outcome sets raises `ValueError` (the assembler must classify every position).

### 4. `AssembledSnapshot` — refined assembler return type

A frozen Pydantic v2 value object bundling:
- `snapshot: PortfolioStateSnapshot`
- `freshness: SnapshotFreshness`

Exists in `freshness.py` (rather than `snapshot.py`) because freshness is logically a sidecar; the snapshot itself stays clean.

**Story 06 update**: the assembler's signature becomes:

```python
async def assemble_snapshot(
    *,
    repository: PortfolioStateRepository,
    price_provider: CurrentPriceProvider,
    sector_resolver: SectorResolver,
    config: PortfolioStateConfig,
    now: datetime,
) -> AssembledSnapshot:
    ...
```

The implementer of this story makes the matching edit to `assembler.py` (the type only — the function body already collects the per-position price-fetch outcomes during Step 6 per story 06's documented sequence; the implementer adds a `PriceFetchOutcomes` accumulator and calls `compute_snapshot_freshness` before returning). The implementer also updates story 06's tests to assert on the bundle shape.

### 5. Logging and observation

`compute_snapshot_freshness` is a pure function — it does not log. The assembler logs structured warnings when:
- `phase1_to_snapshot_seconds > max_phase1_to_snapshot_seconds`
- `count_priced_stale + count_unknown_ticker > 0`

These warnings are emitted by `assembler.py` (the implementer adds the logging calls when the freshness object is computed); the log format and routing is the pipeline's concern.

This story does NOT introduce new logger configuration, new metrics infrastructure, or new alerting hooks. Future command-center / feedback-loop stories consume `SnapshotFreshness` for dashboarding by inspecting the typed object directly.

### 6. Tests

- `PriceFetchOutcomes` happy path: three disjoint sets construct successfully.
- `PriceFetchOutcomes` overlapping sets (a position_id appears in two sets): `ValidationError`.
- `SnapshotFreshness` happy path: build from a known snapshot fixture and known fetch outcomes; verify all field values match the documented formulas.
- `SnapshotFreshness` count-conservation invariant: build with a counter that doesn't satisfy `fresh + stale + unknown == total` ⇒ `ValidationError`.
- `SnapshotFreshness.is_position_price_stale(position_id)` returns `True` for stale and unknown-ticker IDs, `False` for fresh, `False` for unknown-position IDs (no exception).
- `SnapshotFreshness.staleness_summary()` returns the documented format string for a known fixture.
- `compute_snapshot_freshness` happy path: snapshot with 2 open + 1 pending positions, fetch outcomes covering all three, fresh thresholds — produces the expected `SnapshotFreshness`.
- `compute_snapshot_freshness` raises `ValueError` when `fetch_outcomes` references a position_id not in the snapshot.
- `compute_snapshot_freshness` raises `ValueError` when the snapshot has a position not classified in any of the three fetch-outcome sets.
- `AssembledSnapshot` constructs and is frozen (mutating `snapshot` or `freshness` raises).
- `phase1_to_snapshot_within_threshold` is `True` when assembled_at is exactly `max` after committed_at; `False` when 1ms beyond. (Boundary test.)
- `oldest_price_age_seconds is None` if and only if there are zero positions.
- All freshness functions are deterministic across repeated calls.

Out of scope:
- Logging configuration (the assembler implementation owns the log calls; story 02 / future runner stories own logger setup).
- Per-data-source freshness for non-position categories (e.g., active risk parameters' age, thesis quality aggregates' `as_of_timestamp` — these are recorded on the records themselves but not aggregated here; future expansions can add per-category freshness fields if downstream consumers need them).
- Alerting / paging on freshness violations — command-center responsibility.
- Cross-invocation freshness comparisons — feedback-loop responsibility.
- Forecasting freshness based on data-source latencies (the freshness report is a passive sidecar over what just happened, not a predictor).
- Repairing stale data — staleness propagates as a flag; the pipeline's runner decides whether to abort, degrade, or proceed.

## Notes

The freshness sidecar is **passive**: it reports what is, it does not modify the snapshot or escalate. Downstream consumers (command-center dashboards, feedback-loop validators) inspect the typed object and decide what to do with the signals. This separation matches the design's "the snapshot is a passive deliverable" stance.

Per `feedback_simplify_before_building.md`, this story does not introduce a `FreshnessChecker` class, a freshness-policy registry, or a multi-tier alerting system. Three Pydantic models, one pure function, two convenience helpers.

Per `feedback_no_inventing_component_names.md`, "freshness" and "staleness" are sourced from the design's own vocabulary (`internal/README.md`, `state-delivery.md`, `state-persistence.md`). The classification (`fresh` / `stale` / `unknown_ticker`) covers the three observable outcomes from the price-provider Protocol per story 03g.

`PriceFetchOutcomes` is introduced as a typed value object rather than three separate `frozenset` arguments to the compute function so the assembler's accumulator logic produces a single immutable record that can be passed by reference. Tests can construct fixtures explicitly without juggling parallel argument lists.

The "max" thresholds (`max_phase1_to_snapshot_seconds`, `max_price_age_seconds`) are sourced from `PortfolioStateConfig` (story 02). Per `feedback_avoid_numeric_anchors.md`, no thresholds are hardcoded in this story; the operator tunes them in `config/portfolio_state.yaml`. The `phase1_to_snapshot_within_threshold` boolean and the `is_position_price_stale` helper are structural classifications based on those operator-set thresholds.

The `staleness_summary` string is fixed and tested so future log-aggregation tooling (Loki, ELK, etc.) can grep for the format. The format is `"phase1→snapshot {N:.1f}s (within {M:.1f}s); X/Y positions priced fresh; Z stale ({POS-A, POS-B, ...})"`. When `Z == 0`, the trailing parenthetical is omitted: `"...; 13/13 positions priced fresh"`.

`AssembledSnapshot` lives in `freshness.py` rather than `snapshot.py` because:
- The snapshot in isolation is a useful object (consumers that don't care about freshness can ignore the sidecar).
- Adding the freshness field to `PortfolioStateSnapshot` would require story 04a to depend on this story's types — circular dependency.

The convention is: callers receiving an `AssembledSnapshot` typically destructure: `result = await assemble_snapshot(...); snapshot, freshness = result.snapshot, result.freshness`. Tests can construct `AssembledSnapshot` directly with a known snapshot and freshness for downstream-consumer testing.

The fetch-outcomes / freshness path explicitly excludes options-contract pricing (per story 06 note about the option-pricing limitation). Option-contract price staleness, when introduced, will be a separate concern; the current `position_ids_priced_*` sets reflect only underlying-ticker pricing.

## Acceptance criteria

- [ ] `PriceFetchOutcomes` is a frozen Pydantic v2 model with the documented fields; the three sets are pairwise disjoint (validator enforced); `oldest_price_as_of` is tz-aware UTC when not `None`.
- [ ] `SnapshotFreshness` is a frozen Pydantic v2 model with all documented fields and validation invariants (count conservation, set disjointness, timestamp ordering).
- [ ] `SnapshotFreshness.is_position_price_stale` returns `True` for stale and unknown-ticker position IDs, `False` for fresh, `False` for unknown position IDs (no exception).
- [ ] `SnapshotFreshness.staleness_summary()` returns the documented format string; tested on at least one all-fresh and one mixed fixture.
- [ ] `compute_snapshot_freshness` produces the documented `SnapshotFreshness` from a happy-path snapshot + fetch-outcomes + config.
- [ ] `compute_snapshot_freshness` raises `ValueError` when fetch-outcomes references a position_id not in the snapshot.
- [ ] `compute_snapshot_freshness` raises `ValueError` when the snapshot has a position not classified in fetch-outcomes.
- [ ] `AssembledSnapshot` is a frozen Pydantic v2 model bundling `snapshot` and `freshness`; mutating either field raises.
- [ ] The assembler (`assembler.py`) is updated to return `AssembledSnapshot`; story 06's tests are updated to assert on the bundle shape.
- [ ] The assembler emits structured log warnings when `phase1_to_snapshot_seconds` exceeds threshold or when any position is stale / unknown-ticker.
- [ ] `phase1_to_snapshot_within_threshold` boundary test: exactly-at-threshold returns `True`; 1ms beyond returns `False`.
- [ ] `oldest_price_age_seconds is None` ⇔ no positions in snapshot.
- [ ] All freshness functions are deterministic across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
