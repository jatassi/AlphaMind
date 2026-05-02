---
status: not_started
completed_date:
commit_id:
---

# 11 — Parallel domain-researcher orchestrator

## Goal

Implement `run_domain_researchers` — the entry point the pipeline's analysis-layer calls during the analysis phase to dispatch all three domain researchers in parallel. Returns three `DomainResearcherResult` instances, one per sector, in a deterministic order.

## Reading

- `docs/architecture/llm-integration.md` § Pipeline execution flow — `asyncio.gather` parallel dispatch pattern
- `docs/design/03-analysis-layer/README.md` — sequencing context (domain researchers run in parallel; adaptive research follows; synthesizer runs last)
- `docs/design/llm-agent-failure-handling.md` — fail-closed; any single-agent failure aborts the invocation
- `docs/architecture/component-boundaries.md` § Pipeline process — analysis is an in-process phase

## Depends on

- 10 (`run_domain_researcher`, `DomainResearcherResult`)

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `orchestrator.py` defining:
  - `class DomainResearchersOutput(BaseModel, frozen=True)` — the orchestrator's return type:
    - `tech_semis: DomainResearcherResult`
    - `financials: DomainResearcherResult`
    - `energy: DomainResearcherResult`
    - `total_wall_clock_seconds: float` — wall-clock from dispatch to last-finishing sector (not the sum of per-sector wall clocks)
    - `total_tokens_used: TokensUsed` — sum across all three sectors
    Indexable by sector via a helper: `output.by_sector(sector) -> DomainResearcherResult`.
  - `async def run_domain_researchers(invocation_id: str, as_of: datetime, distillation_outputs: DistillationOutputs, session: Session, agents_config: AgentRegistry, sectors_config: SectorRegistry) -> DomainResearchersOutput` — the entry point. Composition:
    1. Extract per-sector distillation slices from `distillation_outputs.sector_outputs[OutputAudience.SECTOR_*]`. Each `SectorOutput` has a `text` (or equivalent) attribute carrying the rendered sector slice — the field name comes from distillation story 11a; this story aligns to whatever is published.
    2. Build the `RegimeLabel` from `distillation_outputs.universal_regime_label` (the convenience accessor from distillation story 12).
    3. Dispatch the three sectors in parallel: `tech_result, fin_result, energy_result = await asyncio.gather(run_domain_researcher(TECH_SEMIS, ...), run_domain_researcher(FINANCIALS, ...), run_domain_researcher(ENERGY, ...))`.
    4. Compose `DomainResearchersOutput` and return.
- Failure semantics:
  - `asyncio.gather(...)` with default `return_exceptions=False` propagates the first failing coroutine's exception. Use this default — fail-closed means the first failure aborts everything; we do not need to wait for siblings.
  - When a sector raises `HarnessFailure` (or any exception), the orchestrator wraps it with sector context: `raise OrchestratorFailure(sector=sector, cause=e) from e`. The wrapper preserves the original cause and is `BaseException`-derived per the `HarnessFailure` hierarchy from story 07.
  - `class OrchestratorFailure(Exception)` carries the sector that failed plus the underlying `HarnessFailure`. The pipeline's analysis-phase entry point catches at this boundary and aborts the invocation per `mid-pipeline-failure-handling.md`.
- Cancellation discipline:
  - When one sector raises, `asyncio.gather` cancels the others. The harness (story 07) is responsible for handling cancellation cleanly (its diagnostic-write step should still run on cancellation; verify story 07's behavior aligns).
- Logging:
  - `logger.info` line at dispatch: `"Dispatching domain researchers (3 sectors in parallel)"`.
  - `logger.info` line at completion: `"Domain researchers complete: tech_semis={...}s {...}tokens, financials=..., energy=..., total_wall_clock={...}s, total_tokens={...}"`.
  - On orchestrator failure: `logger.error` with sector + underlying error class name.
- Unit tests:
  - Happy path: three sector runners (stubbed) return three `DomainResearcherResult` instances → orchestrator returns `DomainResearchersOutput` with all three populated. `total_wall_clock_seconds` is approximately the maximum of the three (not the sum).
  - Single-sector failure: one runner raises `HarnessFailure`, the others succeed → orchestrator raises `OrchestratorFailure` carrying the failing sector and the original cause.
  - Cancellation: when one runner raises, the other two are cancelled (verify via per-runner cancellation flags in the test).
  - Determinism of output ordering: even if energy completes before tech-semis on the wall clock, the `DomainResearchersOutput` exposes them under their per-sector named fields (no ordering ambiguity).
  - The `by_sector(sector)` accessor returns the correct result.
  - `total_tokens_used` is the per-component sum.

Out of scope:
- The pipeline-process analysis-phase entry point that calls this orchestrator (lives in `src/alphamind/pipeline/`).
- Cross-agent orchestration (qualitative researcher, adaptive researcher, synthesizer) — separate work trees.
- Retry / re-dispatch on failure — fail-closed means we don't retry; the next scheduled invocation produces a fresh attempt.

## Notes

The `asyncio.gather` choice over `TaskGroup` / `wait_for`: `gather` matches the Agent SDK's documented usage in `llm-integration.md` and is the simpler API for this fixed-fan-out case. `TaskGroup` (Python 3.11+) is a reasonable upgrade later if cancellation semantics need finer-grained control; defer.

The wrapper `OrchestratorFailure` exists so the pipeline-process layer has a single exception class to catch at the analysis-phase boundary, regardless of which sector failed. Without the wrapper, the caller would catch `HarnessFailure` and lose the sector tag.

`distillation_outputs: DistillationOutputs` is the type from distillation story 12. If that work tree has not yet landed, this story can declare a structural protocol (`class _DistillationOutputs(Protocol)`) carrying the documented fields and the integration wires them in once available.

The per-sector wall-clock and token-count summaries are passthrough from the harness — the orchestrator does not re-time or re-count. The aggregate `total_wall_clock_seconds` is computed from the orchestrator's own `time.monotonic()` markers around the `asyncio.gather` call.

Determinism note: the orchestrator output exposes the three sectors as named fields, so there is no ordering ambiguity. The wall-clock variation across runs is intrinsic — `asyncio.gather` schedules concurrently, and the order of completion depends on the SDK's per-call latency.

## Acceptance criteria

- [ ] `run_domain_researchers(...)` returns a `DomainResearchersOutput` populated with three `DomainResearcherResult`s on the happy path.
- [ ] The three sector runners are dispatched in parallel via `asyncio.gather` (not sequentially).
- [ ] A single-sector failure raises `OrchestratorFailure` with the sector tag and the underlying `HarnessFailure` as `__cause__`.
- [ ] When one sector fails, the other two are cancelled cleanly (no orphan SDK calls).
- [ ] `total_wall_clock_seconds` is approximately `max` across the three sectors, not `sum`.
- [ ] `total_tokens_used` is the per-component sum across the three.
- [ ] `output.by_sector(Sector.FINANCIALS)` returns the financials result.
- [ ] Tests cover happy path, single-sector failure, cancellation behavior, and determinism of the output's named-field structure.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
