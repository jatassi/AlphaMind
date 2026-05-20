# 05 — End-to-end verify + runbook + NSSM install scripts

## Goal

Ship the operator-runnable deliverables for the continuous monitor feature:

1. `scripts/verify_continuous_monitor.py` — exercises the golden paths against the production-shape database (paper mode) and prints a pass/fail summary an operator can read.
2. `scripts/RUNBOOK_continuous_monitor.md` — operator prerequisites, the verify-script invocation, expected output shape, failure-mode triage table.
3. Central runbook insertion — update `scripts/RUNBOOK_end_to_end_verification.md` so the continuous monitor appears as a phase in the dependency-ordered list with the right stage-artifact handoff.
4. NSSM install / uninstall scripts — `scripts/install_monitor_service.ps1`, `scripts/uninstall_monitor_service.ps1` mirroring the collector's pair.

Story 04b's `EMERGENCY_INVOCATION_REQUESTED` path is exercised iff [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) has landed by the time this story runs; the verify script skips that branch otherwise and logs the skip explicitly.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4 — the five responsibilities the verify script must exercise.
* `docs/design/05-execution-layer/continuous-monitor-runtime.md` (story 01) — process-model expectations the runbook documents.
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook this story extends. Read the existing per-feature insertion pattern.
* `scripts/install_collector_service.ps1`, `scripts/uninstall_collector_service.ps1` — NSSM install pattern to mirror.
* `scripts/RUNBOOK_broker_adapter.md`, `scripts/RUNBOOK_state_persistence.md`, `scripts/RUNBOOK_guardrail_enforcement.md`, `scripts/RUNBOOK_oms_commands.md` — per-feature runbook style (sections: Prerequisites, Invocation, Expected output, Triage table).
* `src/alphamind/scripts/verify_decision_pipeline.py` — verify-script pattern this story mirrors (run-once-and-summarize).
* Parent issue `ALP-123` § Notes for the orchestrator — invariants the verify script reinforces.

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + entry point + config
* [ALP-433](https://linear.app/alphamind-jatassi/issue/ALP-433/02a-wire-compose-phase-1-enforcement-into-the-decision-pipeline) (02a) — Phase 1 wiring
* [ALP-434](https://linear.app/alphamind-jatassi/issue/ALP-434/02b-live-underlying-price-stream-task-alpaca-stockdatastream-iex) (02b) — underlying-price stream
* [ALP-435](https://linear.app/alphamind-jatassi/issue/ALP-435/02c-fill-stream-consumer-task) (02c) — fill stream consumer
* [ALP-436](https://linear.app/alphamind-jatassi/issue/ALP-436/03a-greeks-refresh-task-scheduled-move-triggered) (03a) — greeks refresh
* [ALP-437](https://linear.app/alphamind-jatassi/issue/ALP-437/03b-breach-evaluation-loop) (03b) — breach evaluation loop
* [ALP-438](https://linear.app/alphamind-jatassi/issue/ALP-438/04a-engine-envelope-cascade-dispatcher) (04a) — cascade dispatcher
* [ALP-440](https://linear.app/alphamind-jatassi/issue/ALP-440/04c-options-bracket-stop-firing-strategist-visibility-for-between) (04c) — options bracket-stop + strategist visibility

Story 04b ([ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger)) is intentionally NOT in `blockedBy` — it is gated on the external Pipeline scheduler ([ALP-431](https://linear.app/alphamind-jatassi/issue/ALP-431/pipeline-scheduler)) and may land after 05. The verify script handles its presence / absence gracefully.

## Scope

Source under `src/alphamind/scripts/verify_continuous_monitor.py` (new). Runbook + install scripts at `scripts/`. Test scaffolding at `tests/scripts/test_verify_continuous_monitor.py`.

### 1\. `verify_continuous_monitor.py`

A standalone async script that:

* Loads `.env` and resolves the production config.
* Builds an in-memory `PortfolioStateRepository` populated with a fixture portfolio (one equity position, one options position with a price-based stop, one strategy position with a P/L target).
* Constructs a `MonitorSupervisor` and registers each of the five tasks (underlying stream, fill consumer, greeks refresh, breach loop, cascade dispatcher, bracket-stop watcher) using **fakes** for the alpaca-py streams (synthetic quote / trade-update injection) and the broker adapter.

Drive the following scenarios in sequence; each is one labeled paragraph with what drives it and the expected observable.

**(a) Stream connects + caches a quote.** Drive: inject a synthetic `SPY` quote through the fake stream. Expect: `UnderlyingPriceCache.get("SPY")` returns the quote within one second.

**(b) Fill persists.** Drive: inject a fake equity-fill `FillReport`. Expect: a row appears in `fill_records` via `append_fill_record`.

**(c) Greeks refresh on schedule.** Drive: advance the fixture clock past `greeks_refresh_interval_minutes`. Expect: the option position's `OptionGreeks.as_of_timestamp` updates and `iv_used` is populated.

**(d) Greeks refresh on underlying move.** Drive: inject an underlying-price move exceeding `greeks_refresh_underlying_move_threshold_pct`. Expect: greeks refresh fires before the scheduled interval.

**(e) Greeks refresh failure.** Drive: force `fetch_iv_from_options_chains` to raise after exhausting retries. Expect: `OptionGreeks.refresh_failed=True`, prior `delta / gamma / theta / vega / as_of` preserved, one `GREEKS_REFRESH_FAILED` activity-log entry written.

**(f) Halt onset.** Drive: flip the fixture's `DrawdownState.current_drawdown_pct` past the daily limit. Expect: one `HALT_ACTIVATED` activity-log entry.

**(g) Immediate-action breach dispatch.** Drive: flip a fixture position into a `per_position_max_loss` HARD_BLOCK. Expect: `submit_engine_envelope` is called with a single-CLOSE engine envelope; a `POSITION_CLOSED` activity-log entry with `engine_guardrail` provenance is persisted.

**(h) Deferred-rule breach is NOT dispatched.** Drive: flip `sector_concentration` into a HARD_BLOCK. Expect: no envelope is submitted; the rule appears in `BreachLoopResult.rule_evaluations` with classification deferred.

**(i) Options bracket-stop fires.** Drive: inject a quote crossing the price-based-invalidation stop level for the fixture options position. Expect: the direct broker-adapter close path is called once; a `POSITION_CLOSED` activity-log entry with `event_source=BRACKET_MANAGER`, `exit_method=STOP_TRIGGERED` is persisted.

**(j) Strategist visibility.** Drive: render the next-invocation `StrategistView` from the post-scenario snapshot. Expect: `between_invocation_closures` contains both the engine-envelope cascade closure (from g) and the bracket-stop closure (from i), ordered chronologically.

**(k) Emergency request (optional).** Run only if [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) is landed (detected via `EventType.EMERGENCY_INVOCATION_REQUESTED in EventType`). Drive: flip a regime-jump fixture. Expect: one `EMERGENCY_INVOCATION_REQUESTED` activity-log entry; a second jump within `emergency_invocation_cooldown_minutes` is suppressed.

* Cleanly shut the supervisor down via `request_stop`.
* Print a per-scenario pass/fail summary plus an overall PASS / FAIL with a non-zero exit code on any FAIL.

The script must NOT make live websocket calls or live broker submissions — every external surface is a fake. The script is safe to run in CI.

### 2\. `scripts/RUNBOOK_continuous_monitor.md`

Sections, in order:

* **Purpose** — one paragraph linking back to `architecture.md § 4`.
* **Prerequisites** — `.env` with `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET`; production-shape DB at `data/alphamind.db`; collector populating `options_chains` (cron alive).
* **Invocation** — `uv run python scripts/verify_continuous_monitor.py`. Document exit-code semantics: 0 = all scenarios pass, non-zero = ≥ 1 failure.
* **Expected output shape** — sample first-and-last lines of the pass case; the per-scenario format.
* **Failure-mode triage** — table mapping the most likely failure outputs to root causes + remediation. Cover at minimum: missing creds, missing DB, collector offline (no IV in `options_chains`), Alpaca subscription rejection, pipeline scheduler missing (the 04b branch is skipped — not a failure).
* **Live operation** — how to start the actual NSSM service via `install_monitor_service.ps1`; how to read `~/AlphaMind/logs/monitor.log` for live-mode debugging.

### 3\. Central runbook insertion

Edit `scripts/RUNBOOK_end_to_end_verification.md`. Insert the continuous monitor as a phase in the dependency-ordered list, between the OMS commands phase and the Reg T margin attribution phase (or wherever the topological order places it). Include the stage-artifact handoff:

* **Inputs (consumes):** `fill_records` writes ← broker_adapter ([ALP-121](https://linear.app/alphamind-jatassi/issue/ALP-121/broker-adapter)); `options_chains` reads ← collector ([ALP-30](https://linear.app/alphamind-jatassi/issue/ALP-30/collector)); `activity_log` writes ← state_persistence ([ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence)); `submit_engine_envelope` ← OMS ([ALP-375](https://linear.app/alphamind-jatassi/issue/ALP-375/04-engine-envelope-submission-path-monitor-facing-write-function)); `compose_phase_1_enforcement` ← guardrail_enforcement ([ALP-125](https://linear.app/alphamind-jatassi/issue/ALP-125/guardrail-enforcement-layer)).
* **Outputs (produces):** `POSITION_CLOSED` (engine_guardrail provenance) for cascade closures; `POSITION_CLOSED` (BRACKET_MANAGER source, STOP_TRIGGERED / TARGET_REACHED exit_method) for bracket-stop fires; `GREEKS_REFRESH_FAILED` events; `HALT_ACTIVATED` / `HALT_LIFTED` events; `EMERGENCY_INVOCATION_REQUESTED` events (read by [ALP-431](https://linear.app/alphamind-jatassi/issue/ALP-431/pipeline-scheduler) when that lands).

### 4\. NSSM install / uninstall scripts

`scripts/install_monitor_service.ps1`:

* Mirror `install_collector_service.ps1` for shape — `nssm install alphamind-monitor`, configure `AppDirectory`, `AppExit Default Restart` with 60 s throttle, `Start Automatic (delayed)`, `AppStdout` / `AppStderr` redirect to `%USERPROFILE%\AlphaMind\logs\monitor.out.log` / `monitor.err.log`.
* Application: `python -m alphamind.execution.continuous_monitor run`.
* Run under the operator's user account so `%USERPROFILE%` resolves.

`scripts/uninstall_monitor_service.ps1`:

* `nssm stop alphamind-monitor`, `nssm remove alphamind-monitor confirm`.

### 5\. Tests at `tests/scripts/test_verify_continuous_monitor.py`

* Smoke: import the script, invoke its `main` function under a synthetic config, assert exit code 0 and PASS in stdout.
* Failure-injection smoke: force one scenario to fail (e.g., disable the cascade-dispatcher fake), assert non-zero exit code and FAIL in stdout listing the failed scenario.
* The script's per-scenario assertions are tested inline by the per-story unit tests; this test only confirms the runner-level pass/fail aggregation.

## Acceptance criteria

- [ ] `scripts/verify_continuous_monitor.py` exists and runs under `uv run python scripts/verify_continuous_monitor.py` against a production-shape DB.
- [ ] The script exercises scenarios (a) through (j) in the documented list, and exercises (k) when [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) is landed.
- [ ] Exit code is 0 on all-pass; non-zero on any failure; the failed scenario(s) are named in stdout.
- [ ] No live websocket calls or live broker submissions — every external surface is a fake.
- [ ] `scripts/RUNBOOK_continuous_monitor.md` exists with all six documented sections.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` is updated with the continuous-monitor phase, including documented inputs / outputs.
- [ ] `scripts/install_monitor_service.ps1` and `scripts/uninstall_monitor_service.ps1` exist and mirror the collector's pair in shape.
- [ ] `tests/scripts/test_verify_continuous_monitor.py` covers the smoke path and one failure-injection path; both pass under `uv run pytest tests/scripts/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run python scripts/verify_continuous_monitor.py` against a production-shape DB returns exit code 0 with PASS on every documented scenario (skipping (k) when [ALP-439](https://linear.app/alphamind-jatassi/issue/ALP-439/04b-emergency-invocation-trigger) is unlanded).
* `uv run pytest tests/scripts/test_verify_continuous_monitor.py -n auto -v` — both tests pass.
* `uv run pytest -n auto` — full suite green.
* Manual on a Windows VM: `scripts/install_monitor_service.ps1` registers the `alphamind-monitor` service; `nssm status alphamind-monitor` reports running; `nssm stop alphamind-monitor` + `scripts/uninstall_monitor_service.ps1` cleanly removes the service.
* Lint clean per CLAUDE.md.