# 04 — CLI integration + orchestrator branch + import-linter contract

## Goal

Wire `--debug-e2e` into `scheduler/__main__.py`'s `run` subcommand; gate it mutually exclusive with `--mode live` at argparse level; build a new `_run_debug_e2e` helper that (a) calls `configure_debug_e2e`, (b) emits the `seed` phase event + invokes `wipe_and_seed`, (c) records `process_lifetimes`, (d) constructs `RunInvocationContext` with the populated `debug_e2e` field, (e) calls `run_invocation` with `trigger_source="debug_e2e_cli"`. The orchestrator (already extended in story 02a to read `context.debug_e2e.emitter_factory`) automatically threads the emitter through the analysis + decision composition runners. `phase1_inputs` (already factory-injectable from story 01a) receives the log-only queries via the orchestrator's branch. Adds the import-linter contract forbidding production scheduler modules from importing `debug_e2e/`.

## Reading

* `docs/design/debug-e2e-mode.md` §§ 1 (intent), 3 (import-linter contract), 6.3 (DB-path safety guard at seeder), 7 (`--debug-e2e` interaction with `--once` resolved per parent decision (A) — require)
* `src/alphamind/scheduler/__main__.py` — argparse surface + `_run_once` for the shape to mirror
* `src/alphamind/scheduler/orchestrator.py` — `run_invocation` integration site (already gains the emitter source in story 02a; this story wires the `phase1_inputs` query-factory thread)
* `src/alphamind/scheduler/phase1_inputs.py` — `gather_phase1_inputs` factory kwargs (story 01a); this story passes them from `context.debug_e2e`
* `.importlinter` — existing pattern for `forbidden` contracts (kernel-leaf, commands-leaf, etc.)
* `ALP-494` (01a), `ALP-496` (01c), `ALP-497` (02a), `ALP-500` (03) — provide every piece this story integrates
* Parent issue `ALP-493` Pre-resolved decisions §§ (A), (I) — CLI shape, separate `_run_debug_e2e` helper

## Depends on

* `ALP-494` (01a — Broker-adapter Protocols + phase1_inputs refactor)
* `ALP-496` (01c — Synthetic portfolio seeder)
* `ALP-497` (02a — Harness + composition-runner emitter wiring)
* `ALP-500` (03 — DebugE2ESettings + configure_debug_e2e bundle)

## Scope

In scope: `src/alphamind/scheduler/__main__.py` (extend `_parse_args` + add `_run_debug_e2e`), `src/alphamind/scheduler/orchestrator.py` (wire `context.debug_e2e.account_queries` / `ca_queries` into `gather_phase1_inputs`), `.importlinter` (new contract). Tests at `tests/scheduler/test_main_debug_e2e.py`.

### 1\. `--debug-e2e` flag on the `run` subcommand

Extend `_parse_args` in `scheduler/__main__.py`:

```python
run_p.add_argument(
    "--debug-e2e",
    action="store_true",
    help=(
        "Drive one --once invocation with the synthetic portfolio + "
        "log-only broker + progress.jsonl emit. Requires --once and "
        "is mutually exclusive with --mode live."
    ),
)
```

Add argparse validation (after `args = parser.parse_args(argv)`):

* If `args.debug_e2e and args.mode == "live"`: `parser.error("--debug-e2e is incompatible with --mode live")`.
* If `args.debug_e2e and args.once is None`: `parser.error("--debug-e2e requires --once <run_type> --reason <text>")`.

### 2\. `_run_debug_e2e(args)` helper

A new async function alongside `_run_once`:

```python
async def _run_debug_e2e(args: argparse.Namespace) -> None:
    """Drive a single --once invocation under debug-e2e mode."""
    configure_pipeline_logging()
    archive_root = _DEFAULT_ARCHIVE_ROOT
    archive_root.mkdir(parents=True, exist_ok=True)

    # Lazy imports keep production callers from carrying debug_e2e at
    # module-load time (.importlinter contract enforces this).
    from alphamind.scheduler.debug_e2e import configure_debug_e2e
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.seed import wipe_and_seed

    debug_settings = configure_debug_e2e(archive_root=archive_root)
    venue_config = _load_venue_config(_CONFIG_DIR)
    execution_mode = ExecutionMode.paper  # --mode live rejected upstream
    now = datetime.now(UTC)

    async with engine_pair_context() as engines:
        # Seed step: wipe + reseed in its own session.
        # Emits the "seed" phase event around the wipe+seed via a one-off
        # emitter (the invocation_id isn't known yet — uses a sentinel id
        # for the pre-invocation seed event).
        emitter = debug_settings.emitter_factory("_pre_invocation")
        emitter.phase_start("seed")
        async with engines.async_session_factory() as session:
            await wipe_and_seed(
                session=session, now=now,
                db_path=str(engines.db_path),  # confirm available; if not, surface to operator (see Surfacing conditions)
                portfolio=SYNTHETIC_PORTFOLIO,
            )
        emitter.phase_done("seed")

        # Record process_lifetime AFTER wipe so the row survives.
        process_lifetime_id = await record_process_lifetime(
            session_factory=engines.async_session_factory,
            process_role="pipeline",
            archive_root=archive_root,
        )
        context = RunInvocationContext(
            session_factory=engines.async_session_factory,
            sync_session_factory=engines.sync_session_factory,
            process_lifetime_id=process_lifetime_id,
            archive_root=archive_root,
            config_dir=_CONFIG_DIR,
            env_path=_DEFAULT_ENV_PATH,
            venue_config=venue_config,
            execution_mode=execution_mode,
            debug_e2e=debug_settings,
        )
        summary = await run_invocation(
            context=context,
            trigger_type="manual",
            trigger_source="debug_e2e_cli",
            trigger_reason=args.reason,
            firing_run_type=RunType(args.once),
            now=now,
        )

    print(json.dumps(asdict(summary), default=str, indent=2))
```

`main()` dispatches to `_run_debug_e2e` when `args.debug_e2e`; else falls through to `_run_once` / `_run_daemon` unchanged.

If `engines.db_path` is not exposed on the `engine_pair_context` return, derive the resolved DB path from the engine's URL (`engines.async_engine.url.database`) or from `config/main.yaml` directly — see Surfacing conditions.

### 3\. Orchestrator wiring

In `scheduler/orchestrator.py`'s `run_invocation`, when `context.debug_e2e is not None`, pass query factories to `gather_phase1_inputs`:

```python
phase1_inputs = await gather_phase1_inputs(
    handle=phase1_handle,
    venue_config=venue_config,
    execution_mode=execution_mode,
    as_of=now,
    account_queries_factory=(
        (lambda v, m: context.debug_e2e.account_queries)
        if context.debug_e2e is not None
        else None
    ),
    ca_queries_factory=(
        (lambda v, m: context.debug_e2e.ca_queries)
        if context.debug_e2e is not None
        else None
    ),
)
```

Story 02a already added the emitter source `context.debug_e2e.emitter_factory(invocation_id)` at the top of `run_invocation` — this story does NOT duplicate that.

### 4\. Import-linter contract

Add to `.importlinter`:

```toml
[importlinter:contract:debug-e2e-forbidden-in-production]
name = Production scheduler code must not import debug_e2e
type = forbidden
source_modules =
    alphamind.scheduler.__main__
    alphamind.scheduler.orchestrator
    alphamind.scheduler.phase1_inputs
    alphamind.scheduler.phase2_dispatch
    alphamind.scheduler.run_context
    alphamind.scheduler.driver
    alphamind.scheduler.emergency
    alphamind.scheduler.invocation
    alphamind.scheduler.runtime
    alphamind.scheduler.supervisor
    alphamind.scheduler.session
    alphamind.scheduler.logging_setup
    alphamind.pipeline
forbidden_modules =
    alphamind.scheduler.debug_e2e
```

The `_run_debug_e2e` helper imports `debug_e2e` via lazy imports inside the function body — those don't register as module-load-time edges, so import-linter doesn't flag them. `__main__.py` carrying `_run_debug_e2e` is itself a source module in the contract; the lazy imports inside the function pass.

### 5\. Tests

`tests/scheduler/test_main_debug_e2e.py`:

* `_parse_args(["run", "--debug-e2e", "--once", "market_hours_rolling", "--reason", "test"])` accepts; returns the expected namespace.
* `_parse_args(["run", "--debug-e2e", "--mode", "live"])` raises `SystemExit` with the mutual-exclusion message.
* `_parse_args(["run", "--debug-e2e"])` raises `SystemExit` with the `--once` requirement message.
* `_parse_args(["run", "--debug-e2e", "--once", "market_hours_rolling"])` raises `SystemExit` (still need `--reason`).
* A focused unit test on `_run_debug_e2e` against an in-memory SQLite + a substitute `DebugE2ESettings` bearing a `RecordingProgressEmitter` asserts: the `seed` phase event fires before `run_invocation`; `record_process_lifetime` is invoked after the seed; `run_invocation` is called once with `trigger_source="debug_e2e_cli"` and `context.debug_e2e` set.

### Out of scope

* `verify_debug_e2e.py` script + RUNBOOK + central runbook insert (story 05).
* Changing the existing `_run_once` / `_run_daemon` paths.

## Acceptance criteria

- [ ] `--debug-e2e` flag is accepted on the `run` subcommand; help text matches the documented intent.
- [ ] `--debug-e2e --mode live` raises an argparse error before any code runs.
- [ ] `--debug-e2e` without `--once <run_type>` raises an argparse error before any code runs.
- [ ] `_run_debug_e2e` (a) calls `configure_debug_e2e`, (b) invokes `wipe_and_seed` against the debug DB before `record_process_lifetime`, (c) emits `phase_start("seed")` / `phase_done("seed")` around the wipe+seed via a `_pre_invocation` emitter, (d) constructs `RunInvocationContext` with `debug_e2e=settings`, (e) calls `run_invocation` with `trigger_source="debug_e2e_cli"`.
- [ ] `run_invocation` passes `account_queries_factory` / `ca_queries_factory` derived from `context.debug_e2e.account_queries` / `ca_queries` to `gather_phase1_inputs` when `context.debug_e2e is not None`; otherwise passes `None` (default).
- [ ] `.importlinter` carries the `debug-e2e-forbidden-in-production` contract enumerating every production scheduler module + `alphamind.pipeline` as source modules.
- [ ] `uv run lint-imports` passes — the lazy-import pattern in `_run_debug_e2e` does not trigger the contract because import-linter only inspects module-load-time edges.
- [ ] `tests/scheduler/test_main_debug_e2e.py` covers the argparse validation cases + the `_run_debug_e2e` execution shape.
- [ ] `uv run pytest tests/scheduler/ -n auto` passes; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/test_main_debug_e2e.py -n auto` passes. `uv run lint-imports` confirms the new contract holds. Manual smoke (operator-runnable): `uv run python -m alphamind.scheduler run --debug-e2e --once market_hours_rolling --reason 'manual smoke'` runs without hitting Alpaca, prints an `InvocationSummary`, and creates `<archive>/invocations/<id>/progress.jsonl` with the 13 phase events + 9 agent_request/response pairs.

## Surfacing conditions

* If `engines.db_path` is not directly available on the `engine_pair_context` return value, the implementer must derive the resolved DB path from the engine's URL or `config/main.yaml` to pass to `wipe_and_seed`. If both sources are unreliable, pause and surface — the seeder's safety guard depends on a deterministic resolved path. Do NOT short-circuit the guard.