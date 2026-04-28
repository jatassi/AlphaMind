---
status: in_progress
completed_date:
commit_id:
---

# 07 — Per-consumer views and reader protocols

## Goal

Provide typed per-consumer projections of `PortfolioStateSnapshot` (story 04a) and the reader protocols downstream agents wire against. Four consumers — synthesizer, analyst, strategist, portfolio manager — receive different slices of the master snapshot per the design's consumer map. This story declares the typed views, projection functions, reader protocols, and test stubs. Production wiring against the snapshot lands in each consumer's input-bundle assembly story; this work tree owns only the data-layer-side contract.

## Reading

- `../../../design/01-data-layer/internal/README.md` § Consumer map — table that names which categories each consumer receives and at what granularity (synthesizer summary-level, analyst summary-level, strategist full, PM full)
- `../../../design/01-data-layer/internal/portfolio-state.md` — section 3a explicitly enumerates the per-consumer thesis-delivery granularity (full components for strategist, summary for analyst / synthesizer / PM with on-demand component retrieval)
- `../../../design/03-analysis-layer/synthesizer.md` § Portfolio state tools — slim three-tool surface the synthesizer reads
- `../../../design/04-decision-layer/analyst.md` — context-package fields the analyst sees
- `../../../design/04-decision-layer/strategist.md` — full thesis records, P/L trajectories, activity log per portfolio-state.md
- `../../../design/04-decision-layer/portfolio-manager.md` — PM context with cross-constraint summary; the `get_thesis_components` retrieval tool described in § Retrieval tools
- `../../../design/06-risk-guardrails/state-delivery.md` — analyst / strategist / PM guardrail-state headers; this story does NOT render those headers (risk-guardrails owns the rendering); it provides only the typed records that feed them
- `02-package-skeleton-and-config.md` — package layout (`consumers/synthesizer.py`, `consumers/analyst.py`, `consumers/strategist.py`, `consumers/portfolio_manager.py` are this story's targets)
- `04a-master-snapshot.md` — `PortfolioStateSnapshot` is the source from which all views project
- `03b-thesis-records.md` — `ThesisRecord`, `RecentThesisResolution`
- `05e-activity-log-filtering.md` — projection helpers used by the strategist and PM views
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: the synthesizer's slim `PortfolioStateReader` Protocol with three async methods. This story's `SynthesizerView` is the canonical analogue; the synthesizer's existing 05b types can converge against it in a follow-up.

## Depends on

- 02
- 03a, 03b, 03c, 03d, 03e, 03f
- 04a (consumes `PortfolioStateSnapshot`, `PortfolioPnL`, `SectorExposureEntry`, `DirectionalExposure`)
- 05e (uses filtering helpers for strategist and PM views)

## Scope

In scope, all under `src/alphamind/portfolio_state/consumers/`. Tests at `tests/portfolio_state/consumers/test_<consumer>.py`.

### 1. Synthesizer view — `consumers/synthesizer.py`

Slimmest view. Mirrors the synthesizer's existing 05b/06b contract.

**Value objects** — Pydantic v2 frozen:

- `SynthesizerPositionSummary`
  - `ticker: str` — derived from the position's instrument detail
  - `direction: Direction` (from 03a)
  - `sector: str` — sector label as resolved during snapshot assembly
  - `size_pct: float` — `position_weight_pct` from the underlying `PositionRecord`; clamped to `[0, 100]`
  - `position_age_hours: float` — non-negative

- `SynthesizerThesisSummary`
  - `position_id: str`
  - `ticker: str`
  - `summary: str` — the one-liner from `ThesisRecord.summary`
  - `key_catalyst: str`
  - `time_expectation_hours: str` — preserved as prose

- `SynthesizerExposureSnapshot`
  - `sector_exposure_pct: dict[str, float]` — keyed by sector label; values are signed net exposure (long − short) per sector. Sectors with zero net exposure are omitted.
  - `net_directional_pct: float` (from `DirectionalExposure`)
  - `gross_exposure_pct: float` (from `DirectionalExposure`)

**Reader protocol** — `typing.Protocol`, `runtime_checkable`:

```python
class SynthesizerPortfolioStateReader(Protocol):
    async def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]: ...
    async def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]: ...
    async def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot: ...
```

**Projection function**:

```python
def project_synthesizer_view(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: SectorResolver,
) -> SynthesizerView
```

Where `SynthesizerView` is a frozen Pydantic model bundling the three projection results — positions, theses, exposure — for callers that want the full slice without three separate calls. Callers that prefer per-tool reads use `SnapshotBackedSynthesizerReader` (below) which adapts a snapshot to the protocol.

**Stub / adapter**:

- `class SnapshotBackedSynthesizerReader(SynthesizerPortfolioStateReader)` — concrete implementation backed by a `PortfolioStateSnapshot` and a `SectorResolver`; adapts the snapshot to the protocol. Constructor: `SnapshotBackedSynthesizerReader(snapshot: PortfolioStateSnapshot, sector_resolver: SectorResolver)`. Each protocol method returns the projected view's relevant slice.

### 2. Analyst view — `consumers/analyst.py`

Per `state-delivery.md` § Analyst guardrail state header and `analyst.md`'s context contract: held positions (thin — ticker + direction + size + sector), capital, pending orders summary, active thesis summaries (for awareness, not full components).

**Value objects**:

- `AnalystHeldPosition`
  - `position_id: str`
  - `ticker: str`
  - `direction: Direction`
  - `sector: str`
  - `size_pct: float` — position weight; `[0, 100]`
  - `instrument_type: InstrumentType` — to support feature-flag-aware filtering (analyst on primary portfolio omits options/short rows; that filtering happens in the analyst's context-bundle stage, not here)

- `AnalystAvailableCapital`
  - `available_for_new_positions_usd: float` — from `CashLedger.true_deployable_capital_usd`
  - `available_for_new_positions_pct: float` — `(available_for_new_positions_usd / total_portfolio_value_usd) * 100`
  - `per_position_max_size_usd: float` — pulled from `ActiveRiskParameterSet` by rule_id `"per_position_size"` (or whatever the canonical rule_id is); the projection function takes the rule_id as a config-injected string to avoid hardcoding
  - `per_position_max_size_pct: float`

- `AnalystAnalystAbandonedOpening` — see `state-delivery.md` § Abandoned openings section. Each entry:
  - `envelope_id: str`
  - `direction: Direction`
  - `ticker: str`
  - `instrument_type: InstrumentType`
  - `size_pct: float`
  - `abandoned_at: datetime`
  - `failure_reason: str`

**`AnalystView`** — frozen Pydantic v2 bundling:
- `held_positions: tuple[AnalystHeldPosition, ...]`
- `active_thesis_summaries: tuple[SynthesizerThesisSummary, ...]` — re-uses the synthesizer summary type (same shape; the analyst sees thesis summaries for dedup awareness)
- `available_capital: AnalystAvailableCapital`
- `pending_orders: tuple[OrderRecord, ...]` — full `OrderRecord` type from 03c (analyst doesn't need a thinner shape per the design; pending orders are part of the deduplication context)
- `abandoned_openings: tuple[AnalystAbandonedOpening, ...]`

**Projection function**:

```python
def project_analyst_view(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: SectorResolver,
    per_position_size_rule_id: str,
    total_portfolio_value_usd: float,
) -> AnalystView
```

Abandoned openings are sourced from `snapshot.recent_pm_decision_log` filtered to entries with `event_type == EventType.COMMAND_ABANDONED` whose detail's `originating_agent` matches the analyst (per state-delivery.md § Abandoned openings).

### 3. Strategist view — `consumers/strategist.py`

Heaviest view per the consumer map. Full thesis records (component-level), per-position constraint proximity, drawdown state, regime breach flags, full activity log views.

**Value objects** — most are direct re-uses of types from 03* / 04a; the only new types are projection wrappers:

- `StrategistPositionView`
  - `position: PositionRecord` — from `snapshot.open_positions`, fully enriched
  - `thesis: ThesisRecord | None` — the active thesis for this position; `None` if absent (e.g., spin-off orphan)
  - `bracket: BracketRecord | None`
  - `pending_orders: tuple[OrderRecord, ...]` — orders scoped to this position
  - `modification_trail: tuple[ActivityLogEntry, ...]` — per-position activity entries from `position_modification_trail`

- `StrategistAbandonedAction`
  - `envelope_id: str`
  - `command_type: Literal["ADD", "ADJUST", "CLOSE", "CANCEL"]` — from the abandoned command's details
  - `position_id: str | None` — present for ADD/ADJUST/CLOSE; null for CANCEL on an order
  - `order_id: str | None` — present for CANCEL on an order
  - `abandoned_at: datetime`
  - `failure_reason: str`

- `StrategistView`
  - `positions: tuple[StrategistPositionView, ...]` — one per open or pending position; ordered by position_id
  - `recent_thesis_resolutions: tuple[RecentThesisResolution, ...]`
  - `portfolio_pnl: PortfolioPnL`
  - `drawdown: DrawdownState`
  - `sector_exposure: tuple[SectorExposureEntry, ...]`
  - `directional_exposure: DirectionalExposure`
  - `risk_budget: RiskBudgetConsumption`
  - `active_risk_parameters: ActiveRiskParameterSet`
  - `intra_invocation_changelog: tuple[ActivityLogEntry, ...]`
  - `recent_pm_decision_log: tuple[ActivityLogEntry, ...]`
  - `abandoned_openings: tuple[AnalystAbandonedOpening, ...]` — strategist sees these for portfolio-awareness even though the analyst owns re-evaluation
  - `abandoned_actions: tuple[StrategistAbandonedAction, ...]`

**Projection function**:

```python
def project_strategist_view(snapshot: PortfolioStateSnapshot) -> StrategistView
```

The function consults `snapshot.position_modification_trail`, `snapshot.bracket_for_position(...)`, `snapshot.active_thesis_for_position(...)`, and `snapshot.pending_orders_for_position(...)` to build each `StrategistPositionView`. Activity-log filtering uses story 05e helpers (`filter_by_event_type`, etc.) to extract abandoned-action and abandoned-opening sub-tuples.

### 4. Portfolio manager view — `consumers/portfolio_manager.py`

Heaviest read surface. Includes everything the strategist sees plus thesis quality aggregates, plus an on-demand thesis-component retrieval handle (the design's "get_thesis_components retrieval tool").

**Value objects**:

- `PortfolioManagerView`
  - All fields from `StrategistView` (composition: this view contains the strategist view structurally; it does NOT inherit because Pydantic frozen models work cleanly with explicit field declaration). Re-declare each field; use a private helper to copy from a `StrategistView` if convenient.
  - `thesis_quality_aggregates: ThesisQualityAggregate`
  - `position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]` — full per-position trails (the strategist view embedded these per-position; the PM view also has the dict at top level for on-demand lookup)
  - The `cross_constraint_impact` field is omitted here — it is computed by the proposal pre-processor (a decision-layer module), not by portfolio state. The pre-processor builds it from this view; portfolio state delivers the inputs.

**Reader protocol** — `typing.Protocol`, `runtime_checkable`:

```python
class PortfolioManagerThesisComponentReader(Protocol):
    async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]: ...
```

This is the on-demand retrieval handle the PM uses per `portfolio-manager.md` § Retrieval tools — the PM normally sees thesis summaries and pulls component-level detail per position when needed.

**Stub**:

- `class SnapshotBackedThesisComponentReader(PortfolioManagerThesisComponentReader)` — adapts a `PortfolioStateSnapshot` to the protocol. `get_thesis_components(position_id)` returns the components from `snapshot.active_thesis_for_position(position_id).components`, or an empty tuple if no active thesis exists for that position. The stub never raises.

**Projection function**:

```python
def project_portfolio_manager_view(snapshot: PortfolioStateSnapshot) -> PortfolioManagerView
```

### 5. Tests

Per consumer file, a corresponding test file under `tests/portfolio_state/consumers/`.

For each view's projection function:
- Happy-path fixture: build a `PortfolioStateSnapshot` with multiple positions, theses, brackets, orders, and activity entries; project the view; verify each documented field carries the expected slice.
- Empty-portfolio fixture: empty snapshot projects to empty / zero-valued view fields without exception.
- Identity round-trip: identical snapshot input produces identical view output across repeated calls (determinism).

Synthesizer-specific:
- `SnapshotBackedSynthesizerReader.get_positions_summary()` returns the projected positions; `get_active_theses_summary()` returns thesis summaries; `get_exposure_snapshot()` returns the exposure snapshot. Empty snapshot → empty tuples / empty dict.
- `mypy` confirms `SnapshotBackedSynthesizerReader` satisfies `SynthesizerPortfolioStateReader`.
- `isinstance(reader, SynthesizerPortfolioStateReader)` returns `True`.

Analyst-specific:
- Held positions are filtered to ticker + direction + size + sector + instrument_type only (no thesis content, no P/L) — verified by introspecting the projected `AnalystHeldPosition` fields.
- Abandoned openings are correctly filtered from the PM decision log via `filter_by_event_type(..., EventType.COMMAND_ABANDONED)` plus the originating-agent check.
- `available_capital.available_for_new_positions_pct` correctly computes from `total_portfolio_value_usd`.

Strategist-specific:
- Each `StrategistPositionView.modification_trail` correctly slices the snapshot's `position_modification_trail[position_id]`.
- `StrategistPositionView.thesis` is `None` for a position with no active thesis (orphan-spin-off case).
- Abandoned actions and abandoned openings are correctly partitioned by event type and originating-agent.

PM-specific:
- `PortfolioManagerView` includes all `StrategistView` fields plus `thesis_quality_aggregates` and `position_modification_trail` dict.
- `SnapshotBackedThesisComponentReader.get_thesis_components(position_id)` returns the active thesis's components or an empty tuple for unknown positions.
- `mypy` confirms protocol satisfaction.

Out of scope:
- Rendering views as text for agent prompts (each consumer's input-bundle assembly story handles this).
- The guardrail state header (owned by risk-guardrails state-delivery; this story produces typed records, not headers).
- Per-consumer caching across invocations.
- Authentication / access-control on view projection — there is none; consumers are trusted callers within the same process.
- The PM's cross-constraint impact summary — owned by the proposal pre-processor (decision-layer); this view delivers the inputs.
- The synthesizer's tool-rendering layer (`tools/portfolio_state.py` from synthesizer story 06b) — that layer wraps the `SynthesizerPortfolioStateReader` Protocol from this story; its implementation lives in the synthesizer work tree.

## Notes

The four views are tightly bound to `PortfolioStateSnapshot`. Each projection is a pure function over the snapshot — no I/O, no side effects, deterministic.

Per the design (`docs/design/01-data-layer/internal/README.md` § Consumer map), the synthesizer view is the slimmest, the PM view is the heaviest, and the strategist view sits between (full per-position records but without thesis quality aggregates, which the PM owns for calibration).

Per `feedback_simplify_before_building.md`, the views are not subclasses of one another — Pydantic's frozen models work better with explicit field declarations than with inheritance. The PM view re-declares the strategist's fields; a small private helper consolidates the construction.

The synthesizer's existing 05b types (`PositionSummary`, `ThesisSummary`, `ExposureSnapshot`) are equivalent to this story's `SynthesizerPositionSummary` / `SynthesizerThesisSummary` / `SynthesizerExposureSnapshot`. The naming is intentionally namespaced (`Synthesizer*`) so this story's types do not collide with sibling consumer-namespaced types. A follow-up convergence story can migrate the synthesizer to import these canonical types and delete its locally-declared duplicates.

Per `feedback_no_inventing_component_names.md`, the consumer-view names mirror their downstream consumer literally. `SnapshotBackedSynthesizerReader` and `SnapshotBackedThesisComponentReader` describe the wiring shape (snapshot-backed adapter); the production-side implementation can use a different name if it reads from a different source.

The PM's `get_thesis_components` retrieval is exposed as a Protocol with a stub adapter rather than a direct method on `PortfolioStateSnapshot.active_thesis_for_position(...).components`, because the PM's design (`portfolio-manager.md` § Retrieval tools) declares this as a tool the LLM calls. Wrapping it as a Protocol enables the PM's tool-registration story to wire it as an MCP `@tool` callable.

Per `feedback_per_producer_schema.md`, each consumer's view is a separate file (`synthesizer.py`, `analyst.py`, `strategist.py`, `portfolio_manager.py`) rather than one big `views.py`. Each consumer has its own file declaring its own typed view, projection function, and (where applicable) reader protocol — making the per-consumer contract self-contained.

The analyst view's `pending_orders` re-uses `OrderRecord` directly (no thinner projection) because the design's analyst context shows full pending-order detail for deduplication awareness. A thinner shape would discard fields that the analyst's prompt actually consults.

The strategist's `StrategistPositionView` bundles position + thesis + bracket + pending orders + modification trail per position. This bundling matches the strategist's per-position evaluation loop (`strategist.md` § Per-position assessment), where each position is reasoned about as a unit. Pre-bundling at projection time avoids N lookups in the strategist's prompt-assembly code.

The PM's `cross_constraint_impact` field is *not* on this view because it is computed by the proposal pre-processor over the combined analyst+strategist proposal set — a decision-layer concern. The PM view delivers `risk_budget` and `active_risk_parameters` (which the pre-processor consults), but not the projected impact itself. Documented here to clarify the boundary.

## Acceptance criteria

- [ ] `SynthesizerPositionSummary`, `SynthesizerThesisSummary`, `SynthesizerExposureSnapshot`, `SynthesizerView` are frozen Pydantic v2 models with the documented fields.
- [ ] `SynthesizerPortfolioStateReader` is `typing.Protocol`, `runtime_checkable`, with the three documented async methods.
- [ ] `SnapshotBackedSynthesizerReader` implements `SynthesizerPortfolioStateReader`; `mypy` confirms; `isinstance` confirms at runtime.
- [ ] `project_synthesizer_view` produces the documented projection from a happy-path snapshot.
- [ ] `AnalystHeldPosition`, `AnalystAvailableCapital`, `AnalystAbandonedOpening`, `AnalystView` are frozen Pydantic v2 models with the documented fields.
- [ ] `project_analyst_view` produces held positions thin slices, available capital, pending orders, abandoned openings filtered from the PM decision log.
- [ ] `StrategistPositionView`, `StrategistAbandonedAction`, `StrategistView` are frozen Pydantic v2 models with the documented fields.
- [ ] `project_strategist_view` correctly bundles per-position thesis / bracket / pending-orders / modification-trail; orphan-spin-off positions get `thesis = None`.
- [ ] `PortfolioManagerView` includes all strategist fields plus `thesis_quality_aggregates` and `position_modification_trail` dict.
- [ ] `project_portfolio_manager_view` produces the documented projection.
- [ ] `PortfolioManagerThesisComponentReader` is `typing.Protocol`, `runtime_checkable`, with the documented async method.
- [ ] `SnapshotBackedThesisComponentReader` implements the protocol; `get_thesis_components(unknown_position_id)` returns `()`.
- [ ] All projection functions are deterministic across repeated calls.
- [ ] Empty-portfolio snapshot projects to empty-but-valid views without exception across all four consumers.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
