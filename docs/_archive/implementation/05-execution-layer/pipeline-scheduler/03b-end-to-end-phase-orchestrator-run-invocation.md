# 03b — End-to-end phase orchestrator (`run_invocation`)

## Goal

Ship `run_invocation(...)` — the single async entrypoint that drives one pipeline invocation end-to-end through all five phases: **collect** (Phase 1 fill integration) → **distill** + **analyze** (run_analysis_pipeline) → **decide** (run_decision_pipeline) → **execute** (Phase 2 envelope submission). The function opens the `InvocationContext` from story 03a, threads the existing layer-composition primitives, stamps `phase1_completed_at` and `phase2_completed_at` at the correct points, and updates the row's summary JSON fields. This is the load-bearing entry point the APScheduler driver (story 04a) and the emergency receiver (story 04b) both call; story 01's `--once` CLI path also routes here.

## Reading

* Parent issue `ALP-431` § Notes for the orchestrator — fail-closed propagation, Phase 1 / Phase 2 transaction isolation, never-bypass invariants
* `docs/design/05-execution-layer/architecture.md` § Two-phase invocation model — the contract this orchestrator implements
* `docs/design/mid-pipeline-failure-handling.md` — no checkpoint, no resume; any exception aborts cleanly via the surrounding `InvocationContext` rollback
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 / Phase 2 isolation — the single-transaction discipline
* `src/alphamind/scheduler/invocation.py` (story 03a) — `open_invocation` context manager this story consumes
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — `process_unprocessed_fills(handle, ca_activities, alpaca_positions, alpaca_account, *, market_inputs, config) -> Phase1Summary`
* `src/alphamind/execution/state_persistence/invocation_context/context.py` — `stamp_phase_completion(handle, column=...)` helper
* `src/alphamind/pipeline/analysis.py` — `run_analysis_pipeline(...)` signature; threads `session`, `invocation_id`, `as_of`, distillation config, ticker scope, universe, agents config, sectors config, portfolio reader, archive root
* `src/alphamind/pipeline/decision.py` — `run_decision_pipeline(...)` signature; threads repository, price provider, configs, synthesizer text, retrieval store, mode, halt state, agents config, sector / borrow-cost resolvers, library configs, feature flags, etc.
* `src/alphamind/execution/oms/submit_envelope_mcp.py` and `src/alphamind/execution/oms/submit_engine_envelope.py` — Phase 2 envelope dispatch surface
* `src/alphamind/execution/broker_adapter/queries.py` — `AccountStateQueries.get_positions()` / `get_account()` for Phase 1 inputs
* `src/alphamind/execution/corporate_actions/` — CA activity fetcher for Phase 1 inputs
* `src/alphamind/execution/regt_margin_attribution/` — `MarketInputs` construction for Phase 1 attribution
* `src/alphamind/portfolio_state/repository.py` — `SqlPortfolioStateRepository` construction for `run_decision_pipeline`
* `src/alphamind/portfolio_state/pricing.py` — `CurrentPriceProvider` construction
* `src/alphamind/risk_guardrails/state_delivery/config.py` — `StateDeliveryConfig` loader for `run_decision_pipeline`
* `src/alphamind/execution/guardrail_enforcement/` — `compose_phase_1_enforcement` for the active risk parameter set
* `scripts/verify_decision_pipeline.py` and `src/alphamind/scripts/verify_decision_pipeline.py` — the existing fixture-based composition example this orchestrator translates to production wiring

## Depends on

* `ALP-444` (this work tree, story 03a) — `open_invocation` and `build_invocation_record` are the entry into the per-invocation transaction.

## Scope

In scope under `src/alphamind/scheduler/orchestrator.py` + Phase 1 input gatherer at `src/alphamind/scheduler/phase1_inputs.py` + Phase 2 dispatcher at `src/alphamind/scheduler/phase2_dispatch.py`. CLI wiring in `src/alphamind/scheduler/__main__.py` (replaces the `NotImplementedError` placeholder from story 01). Tests at `tests/scheduler/test_orchestrator.py` and `tests/scheduler/test_phase1_inputs.py` and `tests/scheduler/test_phase2_dispatch.py`. No new persistence schema.

### 1\. `InvocationSummary` typed return value

```python
@dataclass(frozen=True, slots=True)
class InvocationSummary:
    invocation_id: str
    trigger_type: TriggerType
    firing_run_type: RunType
    phase1_summary: Phase1Summary  # from process_unprocessed_fills
    commands_submitted: int
    commands_rejected: int
    staleness_flag: bool
    duration_seconds: float
```

Returned on success. On exception the function does not return — the caller observes the raised exception.

### 2\. Phase 1 input gatherer

`src/alphamind/scheduler/phase1_inputs.py`:

```python
async def gather_phase1_inputs(
    *,
    session: AsyncSession,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    as_of: datetime,
) -> Phase1Inputs:
    """Build the typed input bundle for process_unprocessed_fills."""
```

`Phase1Inputs` is a frozen dataclass carrying `ca_activities: tuple[CorporateActionActivity, ...]`, `alpaca_positions: tuple[PositionSnapshot, ...]`, `alpaca_account: TradeAccountSnapshot | None`, `market_inputs: MarketInputs`. Implementation:

* `alpaca_positions` and `alpaca_account` — call `AccountStateQueries.get_positions()` and `.get_account()` against the broker adapter constructed from `venue_config` + `execution_mode`.
* `ca_activities` — call the existing `fetch_corporate_action_activities(...)` helper in `alphamind.execution.corporate_actions`.
* `market_inputs` — build via the existing helpers in `alphamind.execution.regt_margin_attribution`: `underlying_prices` from alpaca quotes for every open-position underlying; `iv_provider` from the latest options-chain snapshot; `risk_free_rate` from the macro_observations table; `as_of` from the caller.

Each call may fail (broker auth, network, etc.). On any failure: return a `Phase1Inputs` with the failed field as a no-op default (`()` or `None`), set `staleness_flag=True` on the InvocationSummary the orchestrator returns, and emit a log warning. Per parent decision (H), failures here do NOT abort the invocation; they degrade it.

### 3\. Phase 2 envelope dispatcher

`src/alphamind/scheduler/phase2_dispatch.py`:

```python
async def dispatch_phase2(
    *,
    handle: InvocationHandle,
    pm_result: PMResult,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
) -> Phase2Summary:
    """Submit every command the PM emitted via the appropriate envelope path."""
```

`Phase2Summary` is a frozen dataclass: `commands_submitted: int`, `commands_rejected: int`. Implementation:

* PM-originated envelopes route through `submit_envelope` (the existing MCP-stub or its non-MCP equivalent — read what's present and use the direct write function).
* Engine-originated envelopes (none expected from the PM, but the orchestrator's surface is uniform) would route through `submit_engine_envelope`; for PM-driven invocations this branch is never taken.
* For each command in the PM's envelope, await the submission; aggregate `commands_submitted` and `commands_rejected` from the per-command result.
* Any submission exception propagates — the surrounding `InvocationContext` rolls back; per `mid-pipeline-failure-handling.md` the failed invocation aborts cleanly and partial submissions remain committed at the broker level (per the OMS's idempotent semantics).

### 4\. `run_invocation` orchestrator

```python
async def run_invocation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
    now: datetime,
) -> InvocationSummary:
    """Drive one pipeline invocation end-to-end through all five phases."""
```

Threading:

1. Open a short read-only session; call `resolve_runtime_dimensions(short_session, firing_trigger=firing_run_type, now=now)` from story 02.
2. Enter the `open_invocation(...)` context manager from story 03a. Inside the `async with`:

   **a. Phase 1 — collect.**
   * Call `phase1_inputs = await gather_phase1_inputs(...)`.
   * Call `phase1_summary = await process_unprocessed_fills(handle, ca_activities=..., alpaca_positions=..., alpaca_account=..., market_inputs=phase1_inputs.market_inputs, config=...)`.
   * Update the invocation row's `fill_collection_summary_json` with `phase1_summary` serialized; update `staleness_flag` if Phase 1 inputs were degraded.
   * Call `await stamp_phase_completion(handle, column="phase1_completed_at")`.

   **b. Phase 2 — distill + analyze.**
   * Build the inputs for `run_analysis_pipeline`: distillation config from resolved config, ticker scope + universe from resolved config, sectors config from resolved config, agents config with overrides applied via `apply_agent_overrides`, portfolio reader from a `SynthesizerPortfolioStateReader` constructed against the open session.
   * Call `analysis_result = await run_analysis_pipeline(session=handle.session, invocation_id=handle.invocation_id, as_of=now, ...)`. The function internally drives distillation → 3 domain researchers + qualitative (parallel) → adaptive → synthesizer.

   **c. Phase 3 — decide.**
   * Build the inputs for `run_decision_pipeline`: `SqlPortfolioStateRepository` from the open session, `CurrentPriceProvider` from the broker adapter, agents config, agent overrides, sector resolver, borrow-cost resolver, library config + market inputs, profile feature flags, state-delivery config, options/short flags, active sectors, halt state from breach-behavior, regime transition breaches.
   * Compute `mode: Literal["normal", "halt"]` from `runtime.active_mode` (HALTED → "halt"; others → "normal").
   * Call `decision_result = await run_decision_pipeline(repository=..., synthesizer_text=analysis_result.synthesizer_result.brief_text, retrieval_store=analysis_result.synthesizer_result.retrieval_store, mode=mode, halt_state=..., ..., timestamp=now, now=now, ...)`.

   **d. Phase 4 — execute.**
   * Call `phase2_summary = await dispatch_phase2(handle=handle, pm_result=decision_result.pm_result, venue_config=..., execution_mode=...)`.
   * Update the invocation row's `command_execution_summary_json` with `phase2_summary` serialized.
   * Call `await stamp_phase_completion(handle, column="phase2_completed_at")`.
3. Exit the `async with` — the InvocationContext commits the transaction.
4. Return the `InvocationSummary`.

Any exception raised at any step propagates out; the `async with` rolls back; the function re-raises. Per parent decision (H), the long-running caller catches and continues; this function does not catch.

### 5\. CLI wiring (replace story 01's NotImplementedError)

In `src/alphamind/scheduler/__main__.py`, replace the `NotImplementedError("run_invocation not yet wired; ships in story 03b")` in the `--once` branch with the actual call:

```python
summary = await run_invocation(
    session_factory=...,
    process_lifetime_id=...,
    trigger_type="manual",
    trigger_source="cli",
    trigger_reason=reason,
    firing_run_type=parsed_run_type,
    archive_root=archive_root,
    config_dir=config_dir,
    env_path=env_path,
    venue_config=...,
    execution_mode=...,
    now=datetime.now(UTC),
)
print(json.dumps(asdict(summary), default=str, indent=2))
```

### Out of scope

* APScheduler-driven scheduling — story 04a.
* Emergency-invocation reception — story 04b.
* `scripts/verify_pipeline_scheduler.py` — story 05.
* NSSM service install — story 05.
* Any test that exercises real LLM agents — orchestrator tests use the existing fixture builders from `tests/scheduler/` (or `tests/pipeline/` precedent) to stub the LLM-dependent paths.

## Acceptance criteria

- [ ] `run_invocation(...)` is importable from `alphamind.scheduler.orchestrator` and accepts the documented kwargs.
- [ ] `gather_phase1_inputs(...)` returns a `Phase1Inputs` with `alpaca_positions`, `alpaca_account`, `ca_activities`, and `market_inputs` populated from the broker adapter and existing helpers; a broker-side `RuntimeError` produces a degraded bundle (no-op defaults) without raising.
- [ ] `dispatch_phase2(...)` submits every command in the PM result and returns `Phase2Summary(commands_submitted=N, commands_rejected=M)`; a broker submission exception propagates.
- [ ] A successful `run_invocation` call against an end-to-end fixture (no-op portfolio, no fills, no commands) inserts exactly one `invocations` row with both `phase1_completed_at` and `phase2_completed_at` populated and the four summary JSONs (`fill_collection_summary_json`, `command_execution_summary_json`, `active_overlays_json`, `feature_flags_snapshot_json`) all non-NULL.
- [ ] A Phase 1 exception raised by `process_unprocessed_fills` propagates out of `run_invocation` and the row is rolled back (no `invocations` row visible to a fresh session).
- [ ] A Phase 3 (decision) exception raised by `run_decision_pipeline` propagates out of `run_invocation` and the row is rolled back; Phase 2 envelope submissions are not attempted.
- [ ] `staleness_flag` is set to `True` on the `InvocationSummary` (and persisted to the row) when Phase 1 inputs were degraded due to broker-side failures.
- [ ] `mode="halt"` flows correctly to `run_decision_pipeline` when `runtime.active_mode == Mode.halted`.
- [ ] `python -m alphamind.scheduler run --once market_hours_rolling --reason "test"` succeeds against the fixture DB and prints the `InvocationSummary` as JSON.
- [ ] `tests/scheduler/test_orchestrator.py`, `tests/scheduler/test_phase1_inputs.py`, and `tests/scheduler/test_phase2_dispatch.py` cover the criteria above and pass under `uv run pytest tests/scheduler/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in existing pipeline / decision / state-persistence tests.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/test_orchestrator.py tests/scheduler/test_phase1_inputs.py tests/scheduler/test_phase2_dispatch.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: `python -m alphamind.scheduler run --once market_hours_rolling --reason "smoke test"` against the paper DB completes; `sqlite3 alphamind.db "SELECT invocation_id, trigger_type, phase1_completed_at, phase2_completed_at, length(fill_collection_summary_json), length(command_execution_summary_json) FROM invocations ORDER BY start_at DESC LIMIT 1"` shows all six fields populated.
* Lint clean per CLAUDE.md.