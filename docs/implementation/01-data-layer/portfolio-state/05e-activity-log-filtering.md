---
status: done
completed_date: 2026-04-27
commit_id: 820e3ed
---

# 05e — Activity log filtering

## Goal

Implement pure-function filtering and projection helpers over `tuple[ActivityLogEntry, ...]`. Consumers (the assembler when sealing the snapshot, the per-consumer view projections in story 07, and any downstream analysis-layer reader of the snapshot) call these to slice the activity log along the dimensions raw state category 5 names: by invocation, by event type, by event group, by position ID, by source, and by chronological window. Pure functions; no I/O; no shared state.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 5 — three category-5 views: 5a intra-invocation changelog, 5b PM decision log over sliding window, 5c position modification trail. The design notes 5c is "a filtered view, not a separate structure" — this story owns the filtering primitives that produce such views.
- `../../../design/05-execution-layer/state-persistence.md` § Activity log entries — event type catalog and detail-payload structure (modeled in 03e); filtering operates over the typed entries
- `02-package-skeleton-and-config.md` — package layout (`computations/activity_log.py` is this story's target)
- `03e-activity-log-records.md` — `ActivityLogEntry`, `EventType`, `EventGroup`, `EventSource`, `EVENT_TYPE_TO_GROUP`
- `04b-repository-protocol.md` — repository methods for category-5 reads
- `05a-position-computations.md`, `05b-portfolio-pnl-and-drawdown-rollups.md`, `05c-sector-and-directional-exposure-rollup.md` — sibling pure-function patterns

## Depends on

- 02
- 03e (consumes `ActivityLogEntry`, `EventType`, `EventGroup`, `EventSource`)

## Scope

In scope, all under `src/alphamind/portfolio_state/computations/activity_log.py` — pure functions with no I/O. Tests at `tests/portfolio_state/computations/test_activity_log.py`.

### 1. Single-dimension filters

Each takes a `tuple[ActivityLogEntry, ...]` and returns a filtered `tuple[ActivityLogEntry, ...]` preserving the input's relative order. None mutate the input; none raise on empty input (empty input → empty output).

- `filter_by_invocation_id(entries: tuple[ActivityLogEntry, ...], invocation_id: str) -> tuple[ActivityLogEntry, ...]` — entries whose `invocation_id` exactly matches.
- `filter_by_event_type(entries: tuple[ActivityLogEntry, ...], event_type: EventType) -> tuple[ActivityLogEntry, ...]`.
- `filter_by_event_types(entries: tuple[ActivityLogEntry, ...], event_types: frozenset[EventType]) -> tuple[ActivityLogEntry, ...]` — multi-type variant; faster than chaining single-type filters when the consumer wants several types in one pass.
- `filter_by_event_group(entries: tuple[ActivityLogEntry, ...], event_group: EventGroup) -> tuple[ActivityLogEntry, ...]`.
- `filter_by_source(entries: tuple[ActivityLogEntry, ...], source: EventSource) -> tuple[ActivityLogEntry, ...]`.
- `filter_by_position_id(entries: tuple[ActivityLogEntry, ...], position_id: str) -> tuple[ActivityLogEntry, ...]` — entries whose `position_id` field exactly matches; entries with `position_id is None` are excluded.
- `filter_by_order_id(entries: tuple[ActivityLogEntry, ...], order_id: str) -> tuple[ActivityLogEntry, ...]`.
- `filter_by_thesis_id(entries: tuple[ActivityLogEntry, ...], thesis_id: str) -> tuple[ActivityLogEntry, ...]`.
- `filter_by_timestamp_range(entries: tuple[ActivityLogEntry, ...], *, start: datetime | None, end: datetime | None) -> tuple[ActivityLogEntry, ...]` — `start` and `end` are tz-aware UTC; `None` means unbounded on that side; the range is inclusive at `start` and exclusive at `end` (standard half-open interval). Mixed tz-aware/tz-naive raises `ValueError`.

### 2. Category-5 projection helpers

Higher-level helpers built atop the single-dimension filters; the names match the design's category-5 views.

- `intra_invocation_changelog(entries: tuple[ActivityLogEntry, ...], invocation_id: str) -> tuple[ActivityLogEntry, ...]` — equivalent to `filter_by_invocation_id`. Provided as a named alias so the snapshot's category-5a field can be populated with a self-documenting call site. Returns entries in their input order; the caller is responsible for ensuring the input was already chronological (the repository documents this in its `get_intra_invocation_changelog` contract per 04b).

- `pm_decision_log(entries: tuple[ActivityLogEntry, ...]) -> tuple[ActivityLogEntry, ...]` — filters to entries with `event_type == EventType.PM_DECISION`. Built atop `filter_by_event_type`; provided as a named alias for category-5b assembly.

- `position_modification_trail(entries: tuple[ActivityLogEntry, ...], position_ids: tuple[str, ...]) -> dict[str, tuple[ActivityLogEntry, ...]]` — for each `position_id` in `position_ids`, returns the entries scoped to that position (via `filter_by_position_id`). Position IDs with no matching entries are absent from the result dict (consistent with `04b.get_position_modification_trail`'s contract). Empty `position_ids` returns `{}`.

- `recent_pm_decisions_for_position(entries: tuple[ActivityLogEntry, ...], position_id: str, limit: int) -> tuple[ActivityLogEntry, ...]` — combines `pm_decision_log` with `filter_by_position_id`, then takes the *last* `limit` entries (most recent assuming chronological input order). Useful for the strategist's "what did the PM most recently decide on this position?" projection. `limit` must be positive; zero or negative raises `ValueError`. Empty matching set returns `()`.

### 3. Composition helpers

- `partition_by_event_group(entries: tuple[ActivityLogEntry, ...]) -> dict[EventGroup, tuple[ActivityLogEntry, ...]]` — single pass over entries, grouped by `event_group`. Groups with no entries are absent from the result. Useful for the strategist's per-group summarization without N filter passes.

- `chronological_sort(entries: tuple[ActivityLogEntry, ...]) -> tuple[ActivityLogEntry, ...]` — returns entries sorted by `timestamp` ascending; ties broken by `entry_id` ascending. The repository documents that its category-5 reads return chronologically-ordered entries; this helper exists for consumers that have merged entries from multiple sources or applied filters in unknown order.

### 4. Tests

Each helper has a happy-path fixture and an empty-input test. Specific tests:

- `filter_by_invocation_id` returns only matching entries; empty input → `()`.
- `filter_by_event_type` returns only matching entries; case-sensitive on enum value.
- `filter_by_event_types(entries, frozenset({EventType.POSITION_OPENED, EventType.ORDER_FILLED}))` returns the union.
- `filter_by_event_group` happy path; empty group → `()`.
- `filter_by_source` happy path.
- `filter_by_position_id` excludes entries with `position_id is None`.
- `filter_by_order_id` and `filter_by_thesis_id` analogous.
- `filter_by_timestamp_range` inclusive `start`, exclusive `end`; `None` means unbounded; mixed tz-aware/tz-naive raises `ValueError`; reversed range (`start > end`) returns `()`.
- `intra_invocation_changelog` equivalence with `filter_by_invocation_id`.
- `pm_decision_log` returns only `PM_DECISION` entries.
- `position_modification_trail` for a known position_id returns the matching tuple; for an unknown position_id, the key is absent from the result dict.
- `position_modification_trail` with empty `position_ids` returns `{}`.
- `recent_pm_decisions_for_position` happy path: entries chronologically ordered, limit=2 returns the two most-recent matches.
- `recent_pm_decisions_for_position` with `limit <= 0` raises `ValueError`.
- `partition_by_event_group` correctly groups; no entries with `position_lifecycle` group when none present.
- `chronological_sort` orders correctly; ties broken by `entry_id`.
- All functions: identical input → identical output across repeated calls (parametrized determinism test).
- All functions: input tuple unchanged after the call (immutability of input).

Out of scope:
- Activity log persistence (state-persistence story set).
- Repository read methods that produce category-5 views (story 04b owns those; this story's helpers operate on already-fetched tuples for in-memory transformation).
- Rendering activity log entries as human-readable text (consumer concern; the strategist's prompt does this, not portfolio state).
- Cross-invocation aggregation (counting events over a multi-day window) — feedback-loop concern.
- Activity log entry construction (the repository or write-side logic produces them; this story consumes them).
- Compaction / retention policies — `state-persistence.md` § Historical retention covers this; not a portfolio-state concern.

## Notes

The single-dimension filters are simple list-comprehension wrappers over the typed tuple. They exist as named helpers (rather than inline list comprehensions in the assembler / consumer code) because the activity log surface is the most filter-heavy region of the snapshot, and named filters keep the call sites self-documenting and testable.

Per `feedback_simplify_before_building.md`, this module is a function library. No `ActivityLogQuery` builder, no `ActivityLogFilter` chain — straight-line filters and projections.

Per `feedback_no_inventing_component_names.md`, the projection-helper names (`intra_invocation_changelog`, `pm_decision_log`, `position_modification_trail`) match the design's category-5 view names literally. Single-dimension filters are descriptively named (`filter_by_X`).

The choice of `tuple[ActivityLogEntry, ...]` over `list` for both inputs and outputs preserves immutability and matches the typed-records convention across the records modules. Constructing a tuple from a generator is a `tuple(...)` call — same cost as constructing a list.

`partition_by_event_group` exists because the strategist's per-event-group rendering can call it once and avoid eight filter passes. The eight-group enumeration is fixed; the dict has at most eight keys.

`recent_pm_decisions_for_position(limit=N)` is positional-stable: it slices the *last* N entries from the chronologically-ordered match set. The function does not re-sort; it expects chronological input. If the input ordering is uncertain, the caller can pre-apply `chronological_sort`.

`filter_by_timestamp_range` uses a half-open `[start, end)` interval per the standard convention. This makes range chaining seamless: `[t1, t2)` followed by `[t2, t3)` covers `[t1, t3)` without overlap.

The `frozenset[EventType]` parameter on `filter_by_event_types` reflects that callers already know the type set ahead of time. Using `frozenset` over `set` lets the caller pass the type set as a default argument or a module-level constant safely.

## Acceptance criteria

- [ ] All single-dimension filter functions return a filtered tuple preserving input order; empty input returns `()`; input tuple unchanged after the call.
- [ ] `filter_by_invocation_id`, `filter_by_event_type`, `filter_by_event_types`, `filter_by_event_group`, `filter_by_source`, `filter_by_position_id`, `filter_by_order_id`, `filter_by_thesis_id` each pass happy-path and empty-input tests.
- [ ] `filter_by_position_id` excludes entries with `position_id is None`.
- [ ] `filter_by_timestamp_range` enforces tz-aware datetimes; raises `ValueError` on mixed tz-aware/tz-naive; uses inclusive `start`, exclusive `end`; reversed range returns `()`.
- [ ] `intra_invocation_changelog`, `pm_decision_log`, `position_modification_trail`, `recent_pm_decisions_for_position` return the documented projections.
- [ ] `position_modification_trail` omits keys with no matching entries; empty `position_ids` → `{}`.
- [ ] `recent_pm_decisions_for_position` raises `ValueError` for `limit <= 0`.
- [ ] `partition_by_event_group` correctly groups; absent groups have no key in the result.
- [ ] `chronological_sort` orders by timestamp ascending; ties broken by `entry_id` ascending.
- [ ] All functions are deterministic across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
