---
status: not_started
completed_date:
commit_id:
---

# 12 — Distillation orchestrator entry point

## Goal

Implement `run_external_distillation()` — the single entry point the pipeline process calls during its distillation phase. The orchestrator sequences refresh of Class B state → per-category indicator computations → regime classification → anomaly aggregation → per-consumer assembly → return of the assembled outputs (sector documents, correlation/regime brief, universal regime label). Fail-closed throughout: any failure aborts the invocation per the mid-pipeline-failure policy.

## Reading

- `docs/design/02-distillation-layer/external.md` — the specification this orchestrator implements
- `docs/design/02-distillation-layer/threshold-calibration.md` § Class B refresh — "Refresh at the start of each invocation's distillation phase, before anomaly detection runs"
- `docs/design/mid-pipeline-failure-handling.md` — fail-closed semantics
- `docs/architecture/component-boundaries.md` § Pipeline process — distillation is an in-process phase of the pipeline; this orchestrator is what the pipeline's distillation-phase entry point calls
- `docs/architecture/data-and-state.md` § Distillation state — the state tables this orchestrator coordinates
- `docs/architecture/infrastructure.md` § Layer 2 invocation archive — the per-invocation file tree this orchestrator writes into
- All prior stories (02–11b) — every primitive this orchestrator composes

## Depends on

- 02, 03, 04, 05, 06, 07, 08a, 08b, 08c, 08d, 08e, 08f, 09, 10, 11a, 11b.

## Scope

In scope: under `src/alphamind/distillation/orchestrator.py` —

- **`DistillationOutputs`** dataclass (the return type):
  - `sector_outputs: dict[OutputAudience, SectorOutput]` — three entries (`SECTOR_TECH_SEMIS`, `SECTOR_FINANCIALS`, `SECTOR_ENERGY`).
  - `correlation_regime_brief: CorrelationRegimeBrief` — the synthesizer's `CR` brief.
  - `universal_regime_label: dict` — the `regime.label` block's payload (the universal-broadcast contract data; convenience accessor for callers that only want the regime).
  - `invocation_id: str` — passed through.
  - `as_of: datetime` — the invocation's effective UTC timestamp.
  - `total_blocks: int`, `total_anomalies: int`, `bootstrap_block_count: int` — diagnostic counts.
- **`run_external_distillation(session, config, ticker_scope, as_of, invocation_id) -> DistillationOutputs`** — the main entry point:

  Phase 1 — Class B state refresh (per `threshold-calibration.md § Class B refresh`):
  1. `refresh_ticker_baselines` for every active baseline kind (volume, ATR, spread, sentiment).
  2. `refresh_pair_lag` for every named lead-lag pair.
  3. `refresh_contract_history` for every tracked prediction-market contract.
  4. `refresh_event_history` for both event kinds (gap, extended-hours).
  5. `refresh_composite_state` for both composites (funding stress, market liquidity).

  Each refresh runs in its own SQLAlchemy transaction. Any failure raises and the orchestrator propagates per the fail-closed policy.

  Phase 2 — per-category indicator computations + anomaly detection:
  - Run the six per-category modules in parallel (`asyncio.gather` or `concurrent.futures` — distillation is I/O-bound on database reads and pure-CPU otherwise; the right concurrency primitive depends on the surrounding pipeline framework. Use `asyncio` to match the pipeline process's existing async runtime). Each returns its list of `OutputBlock` instances.
  - Order: 08a, 08b, 08c, 08d, 08e, 08f. The parallelism is across these; within each, the module is sequential.

  Phase 3 — regime classification (per story 09):
  - Compute the new regime label, the transition state, the indicator agreement, the regime-skip emergency flag.
  - Persist to `distillation_regime_state`.
  - Emit the `regime.label` `OutputBlock`.

  Phase 4 — aggregation:
  - Concatenate all blocks from phases 2 and 3.
  - Run `partition_blocks`, `collect_anomalies`, `group_anomalies_by_audience` per story 10.

  Phase 5 — assembly:
  - For each of the three sector audiences, call `assemble_sector_output` (story 11a).
  - Call `assemble_correlation_brief` (story 11b).
  - Build `DistillationOutputs`.

  Phase 6 — invocation-archive write:
  - Write each sector output to `%USERPROFILE%\AlphaMind\archive\<date>\<invocation>\distillation\<sector>_sector.md` (cross-platform: `~/AlphaMind/archive/...` on macOS / Linux; resolve via the existing path-resolution helper from the collector if present, else implement a small one).
  - Write the correlation/regime brief to `.../distillation/correlation_regime_brief.md`.
  - Write the regime label to `.../distillation/regime.md`.

  Phase 7 — brief-store population (per [`data-and-state.md § Brief store`](../../../architecture/data-and-state.md#3-brief-store-per-invocation-read-heavy)):
  - Insert the `CR-N` entries from `correlation_regime_brief.reference_index` into the `briefs` table per the existing brief-store schema. (If the brief-store schema is not yet implemented, document the gap and emit a follow-up TODO; the orchestrator returns the brief as part of `DistillationOutputs` either way so an in-process consumer can use it without the persistent store.)

- Logging: per phase, emit a `logger.info` line with timing and counts (blocks emitted, anomalies detected, baseline rows refreshed). Use the existing `alphamind`-module logger conventions.
- Fail-closed propagation: any phase raising aborts the invocation. The orchestrator does not catch and continue. The caller (the pipeline phase entry point) handles the abort per `mid-pipeline-failure-handling.md`.
- Unit tests:
  - End-to-end test against a fixture database with seeded `ohlcv_bars`, `macro_observations`, `news_articles`, `prediction_market_snapshots`, `event_calendar`, `corporate_actions`, `sector_classification`, `asset_universe` covering ~5 fixture tickers per sector and a few weeks of synthetic data. Run `run_external_distillation` and verify:
    - `DistillationOutputs.sector_outputs` has three entries with non-empty text.
    - `DistillationOutputs.correlation_regime_brief.text` is non-empty.
    - `DistillationOutputs.universal_regime_label` carries the regime label and transition state.
    - The invocation-archive files are written to the expected paths.
    - The `briefs` table receives the `CR-N` entries (or the documented TODO is in place).
  - Fault injection: a deliberate exception in one of the per-category modules propagates to the caller; the orchestrator does not swallow it.
  - Determinism: running the orchestrator twice on the same fixture inputs produces byte-identical outputs.
  - Refresh ordering: the test verifies refresh phase completes before any computation phase reads from state (a fault-injected refresh failure prevents downstream computation).

Out of scope:
- The pipeline phase wrapper that calls this orchestrator (lives in the pipeline package — `src/alphamind/pipeline/`).
- Internal distillation orchestration (separate work tree under `internal/`).
- The retrospective backfill of state for first-deploy bootstrap (handled implicitly: refresh primitives produce `bootstrap` calibration tags rather than failing when state is missing).
- LLM-side downstream consumption (analysis layer).

## Notes

The orchestrator is the integration point — the place where every prior story's code converges. Keep it thin: each phase is a few lines that delegate to the primitive owning the work. Resist the temptation to inline business logic; if a phase needs more than ~30 lines, push the logic back into the corresponding primitive.

The asyncio choice for phase 2 is deliberate: the pipeline process is asyncio per [`infrastructure.md § APScheduler`](../../../architecture/infrastructure.md#scheduling) (`AsyncIOScheduler`). Using `asyncio.gather` for the parallel module dispatch keeps the runtime model consistent. If a per-category module needs synchronous DB calls (most do), wrap them via `asyncio.to_thread` so the event loop isn't blocked.

The invocation-archive path resolution must work cross-platform (development on macOS, production on Windows). The collector implementation already has the `%USERPROFILE%\AlphaMind\` convention working — read its path helper and reuse it. Document the path source in source comments.

The brief-store population step assumes the brief-store schema (`briefs` table) is implemented elsewhere. If it isn't, return early from phase 7 with a logged warning and a TODO comment naming the dependency. The integration test for that path remains a separate follow-up.

The `DistillationOutputs.universal_regime_label` field is a convenience for callers that only want the regime — primarily the analysis-layer agents that receive the regime as universal context but don't need the full sector / `CR` brief. The orchestrator can extract it from the `regime.label` block and surface it directly without forcing every caller to parse the structured-text output.

Determinism across runs is critical for the invocation archive's diffability — the same fixture inputs must produce byte-identical archive files. The downstream stories (05, 10, 11a, 11b) all guarantee deterministic rendering; this story just composes them. Test it.

## Acceptance criteria

- [ ] `DistillationOutputs` dataclass exists with the documented fields.
- [ ] `run_external_distillation` executes the seven phases in the documented order.
- [ ] Class B refresh runs before any per-category computation.
- [ ] All six per-category modules are dispatched (parallel or sequential).
- [ ] Regime classification persists to `distillation_regime_state` and emits the `regime.label` block.
- [ ] Aggregation produces partitioned blocks and grouped anomaly summaries.
- [ ] Three sector outputs and one correlation/regime brief are assembled.
- [ ] Invocation archive files are written to the correct paths (cross-platform).
- [ ] Brief-store entries are populated (or the documented TODO is in place if the schema isn't yet implemented).
- [ ] Per-phase logging produces timing and count lines.
- [ ] Any phase exception propagates up; the orchestrator does not swallow it.
- [ ] End-to-end fixture test produces non-empty outputs across all three sectors and the brief.
- [ ] Determinism: repeated calls on the same fixtures produce byte-identical outputs and archive files.
- [ ] Fault-injection test: a per-category module exception propagates without partial state corruption.
- [ ] Refresh-before-compute ordering enforced (a refresh failure prevents downstream computation).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
