# Continuous Monitor End-to-End Verification Runbook

Operator workflow for the ALP-441 verification artifact that proves the
continuous monitor's five responsibilities (Alpaca fill-stream consumption,
guardrail breach detection + protective response, emergency invocation
triggering, options-greeks refresh orchestration, options bracket-stop
firing) compose cleanly against the production-shape database. The script
runs every scenario against in-memory fixtures — no live websocket calls,
no live broker submissions, no live vendor calls — so it is safe to run in
CI alongside the operator-facing path.

## Purpose

The continuous monitor is the first-class persistent runtime peer of the
OMS, broker adapter, and guardrail layer per
[architecture.md § 4](../docs/design/05-execution-layer/architecture.md). It
runs as a parallel NSSM service (`alphamind-monitor`) and owns five
responsibilities spanning paper and live trading: subscribing to Alpaca's
`trade_updates` websocket and persisting fills, evaluating portfolio state
against guardrail limits on a 60-second cadence, triggering emergency
pipeline invocations on regime jumps / multi-rule breaches / drawdown
velocity / margin calls, refreshing options greeks on a scheduled and
move-based cadence, and firing options bracket stops directly via the
broker adapter.

This runbook validates the monitor post-changes — run it after any edit
to `src/alphamind/execution/continuous_monitor/` (supervisor, session,
logging, breach-loop, cascade-dispatch, emergency-trigger, greeks-refresh,
fill-stream-consumer, underlying-stream, bracket-stops) and after any
cross-layer change that touches the breach-behavior primitives, the OMS
engine-envelope path, the strategist's `between_invocation_closures`
projection, or the activity-log event vocabulary the monitor writes.

## Prerequisites

1. **`uv sync` completed** — the script runs under `uv run`. No other
   environment configuration is required: the verify script does not
   touch the network and does not touch the production DB.
2. **`.env` with `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET`** — required
   for the live monitor service (the NSSM-installed
   `alphamind-monitor`), not for the verify script. The verify script
   itself uses fakes for every broker / websocket surface, so missing
   credentials do not fail the verify script. The credentials must be
   present in `%USERPROFILE%\.env` on the production Windows host before
   `nssm start alphamind-monitor` succeeds — the daemon's
   `load_dotenv()` call resolves them at startup.
3. **Production-shape DB at `data/alphamind.db`** — only required for
   live operation, not for the verify script. The verify script
   constructs its own in-memory SQLite engine for scenario (b) and uses
   fakes for every other scenario.
4. **Collector populating `options_chains` (cron alive)** — only
   required for live operation. The greeks-refresh task reads the
   collector's `options_contract_snapshots` table when it fetches IV
   per parent issue ALP-123 § Pre-resolved decision (C). The verify
   script seeds IV quotes directly into a fake IV-fetch callable, so
   collector state does not affect the verify result.

## Invocation

```bash
uv run python scripts/verify_continuous_monitor.py
```

CLI flags:

- `--output {text|json}` — output format (default `text`). The `json`
  form is convenient for `jq`-driven post-processing in automation
  pipelines; the `text` form is the canonical operator output.

Exit-code semantics:

- `0` — every scenario passed (and the optional scenario (k) either
  passed or was skipped with documented `[SKIP]` rationale).
- non-zero — at least one scenario FAILed. The failing scenario(s) are
  named in the `RESULT: FAIL` summary line and in the corresponding
  per-scenario `[FAIL]` lines.

Runtime: sub-30s on a development machine. No SDK calls, no live
broker contact, no LLM cost.

## Expected output

A passing run produces 11 `[PASS]` lines followed by the summary:

```
==============================================================================
AlphaMind Continuous Monitor Verification
==============================================================================
  [PASS] (a) Stream connects + caches a quote
  [PASS] (b) Fill persists
  [PASS] (c) Greeks refresh on schedule
  [PASS] (d) Greeks refresh on underlying move
  [PASS] (e) Greeks refresh failure preserves prior + emits log entry
  [PASS] (f) Halt onset emits HALT_ACTIVATED
  [PASS] (g) Immediate-action breach dispatch
  [PASS] (h) Deferred-rule breach is NOT dispatched
  [PASS] (i) Options bracket-stop fires
  [PASS] (j) Strategist visibility — between_invocation_closures
  [PASS] (k) Emergency request — regime jump fires, second suppressed
==============================================================================
RESULT: PASS (11/11 scenarios)
==============================================================================
```

The header / footer banner is fixed width. Each scenario prints exactly
one `[PASS]` / `[FAIL]` / `[SKIP]` line; on failure a second line
prefixed with six spaces carries the diagnostic detail (e.g.
`      cache.get('SPY').price=520.49, expected 520.50`).

### Per-scenario semantics

Mirrors the labeled paragraphs in the story body verbatim — each
scenario corresponds to one paragraph in
[ALP-441](https://linear.app/alphamind-jatassi/issue/ALP-441):

- **(a) Stream connects + caches a quote** — injects a synthetic SPY
  quote into `UnderlyingPriceCache`; asserts `cache.get("SPY")`
  returns the quote within one second.
- **(b) Fill persists** — appends a fake equity `FillRecord` to a fresh
  in-memory SQLite engine via `append_fill_record`; asserts exactly
  one `fill_records` row lands.
- **(c) Greeks refresh on schedule** — advances the per-position state
  anchor past `greeks_refresh_interval_minutes`; asserts the writer's
  `update_options_greeks` is called and the new greeks carry the
  current timestamp + the fetched IV.
- **(d) Greeks refresh on underlying move** — anchors the position at a
  recent timestamp (inside the scheduled interval) but a stale spot
  exceeding `greeks_refresh_underlying_move_threshold_pct` of the live
  spot; asserts the refresh still fires.
- **(e) Greeks refresh failure** — forces the injected IV-fetch
  callable to raise; asserts `OptionGreeks.refresh_failed=True`,
  prior `delta/gamma/theta/vega/as_of/iv_used` preserved verbatim,
  and one `GREEKS_REFRESH_FAILED` activity-log entry written.
- **(f) Halt onset emits HALT_ACTIVATED** — drives the
  `HaltTransitionTracker` from inactive to active daily halt; asserts
  exactly one `HALT_ACTIVATED` entry with `halt_type="daily_drawdown"`.
- **(g) Immediate-action breach dispatch** — feeds a
  `per_position_max_loss` HARD_BLOCK breach to the `CascadeDispatcher`;
  asserts exactly one engine envelope is submitted with
  `risk_management_subtype="engine_guardrail"` and
  `close_rationale_type="risk_management"`.
- **(h) Deferred-rule breach is NOT dispatched** — synthesises a
  `sector_concentration` HARD_BLOCK with
  `classification=deferred_to_pm`; asserts the loop's
  `immediate_action_breaches` is empty and the rule still appears in
  `rule_evaluations` with the deferred classification.
- **(i) Options bracket-stop fires** — injects a spot crossing the
  price-based stop on a fixture options position; asserts the broker
  adapter's `submit_options_close` is called once and one
  `POSITION_CLOSED` activity-log entry lands with
  `source=BRACKET_MANAGER` and `exit_method=STOP_TRIGGERED`.
- **(j) Strategist visibility** — composes the
  `between_invocation_closures` tuple a strategist invocation would
  consume after (g) and (i); asserts both the engine-guardrail and
  bracket-manager closures are present, ordered chronologically.
- **(k) Emergency request** — feeds a NORMAL → CRISIS regime jump to
  the `EmergencyTriggerEvaluator`; asserts one
  `EMERGENCY_INVOCATION_REQUESTED` entry with
  `trigger_type="regime_jump"` and that a second jump inside the
  30-minute cooldown is suppressed.

  **Skip path:** when `EventType.EMERGENCY_INVOCATION_REQUESTED` is not
  part of the activity-log vocabulary (ALP-439 not yet landed), the
  scenario reports `[SKIP]` with the rationale message and counts as
  passed for the runner-aggregation step.

## Failure-mode triage

| Failure output (excerpt) | Likely root cause | Remediation |
|--------------------------|-------------------|-------------|
| `(a) ... cache.update did not complete within 1s` | `UnderlyingPriceCache.update`'s asyncio lock acquisition stalled — a global event-loop hang. | Investigate any blocking call newly added to `cache.py`; the writer-lock contract requires that the body of `update` never awaits non-cache primitives. |
| `(b) ... expected 1 fill_records row, got 0` | `append_fill_record` returned without inserting — either the dedupe-key check matched against a phantom row or the `INSERT ... ON CONFLICT DO NOTHING` clause is mis-routed. | Read `src/alphamind/execution/state_persistence/write_paths/fill_persistence.py` and re-confirm the dedupe-key columns mirror the `uq_fill_records_dedupe` index. |
| `(c) ... expected 1 writer.update_options_greeks call, got 0` | Scheduled-trigger predicate did not fire — either `_is_due` rejected the per-position state, or the OCC-symbol lookup missed against the IV-quote map. | Inspect `_is_due` in `greeks_refresh/task.py`; verify the position's OCC symbol matches `occ_symbol_for_options(details)`. |
| `(d) ... expected 1 update_options_greeks call from move trigger, got 0` | Move predicate did not fire — anchor spot too close to current spot, or the move-threshold computation regressed. | Confirm `greeks_refresh_underlying_move_threshold_pct` is consumed by `_is_due` and the percentage uses anchor spot as the denominator. |
| `(e) ... refresh_failed=False, expected True` | The failure-envelope branch in `_refresh_options_position` did not flip the flag — typically a refactor moved the `_build_failed_greeks` call out of the error path. | Re-read `greeks_refresh/task.py` and check that `_build_failed_greeks` is unconditionally invoked when `failure_reason is not None`. |
| `(e) ... prior greeks not preserved on failure` | The failure path is mutating one of `delta/gamma/theta/vega/as_of/iv_used` instead of cloning. | `_build_failed_greeks` must copy the prior `OptionGreeks` verbatim and only flip `refresh_failed`. |
| `(f) ... expected 1 HALT_ACTIVATED entry on transition` | `HaltTransitionTracker.observe` did not yield the activation entry — typically a regression in the inactive→active edge-detection. | Read `halt_tracker.py`; confirm `_derive_entries` yields the entry on the `not daily_was and daily_now` branch. |
| `(g) ... expected 1 submit_engine_envelope call, got 0` | `CascadeDispatcher.handle_immediate_breach` short-circuited — either the selector returned no candidate or the secondary-breach check classified the envelope as deferred. | Check `tests/execution/continuous_monitor/cascade_dispatch/test_dispatcher.py::test_happy_path_submits_one_envelope_for_per_position_max_loss`; the verify scenario mirrors that fixture. |
| `(g) ... envelope.commands[0].risk_management_subtype=...` | The cascade dispatcher produced an envelope but the embedded CLOSE carries the wrong provenance — usually a refactor in `compose_engine_envelope`. | Confirm `EngineEnvelope` Pydantic validator enforces the `engine_guardrail` subtype on CLOSE commands. |
| `(h) ... deferred-classification rule reached submit_engine_envelope` | The breach loop did not filter `deferred_to_pm` rules out of `immediate_action_breaches`. | `BreachLoopResult.immediate_action_breaches` must be `tuple(e for e in rule_evaluations if e.classification is BreachResponse.immediate_engine)` — check the loop's projection. |
| `(i) ... expected 1 submitter.submit_options_close call` | The bracket watcher did not fire — typically a regression in `evaluate_price_based_trigger` (e.g. wrong comparator for the leg's `direction`). | Check `triggers.py`; `direction="LTE"` must fire on `spot <= threshold`. |
| `(i) ... POSITION_CLOSED.source=...` | The closer wrote the activity-log entry with the wrong `EventSource` — typically a copy-paste from the engine-envelope path. | The bracket close path emits `EventSource.BRACKET_MANAGER`, NOT `BRACKET_MANAGER` from the engine-envelope `EngineGuardrailSource`. |
| `(j) ... closures[0].origin=...` | Sort ordering regression — the projection must sort by `(closed_at, position_id)` ascending. | Check `_project_between_invocation_closures` in `consumers/strategist.py`. |
| `(k) ... expected exactly 1 EMERGENCY_INVOCATION_REQUESTED entry` | Cooldown not enforced or evaluator re-fired on the second jump. | Read `cooldown.py`; the `may_fire` method should return `False` for non-margin triggers within the configured window. |
| `(k) ... skipped — EMERGENCY_INVOCATION_REQUESTED not in EventType` | Story ALP-439 not landed; the activity-log vocabulary lacks the emergency event type. This is a documented skip and the scenario does NOT count as a failure. | No action — the verify script returns exit 0 with this scenario marked `[SKIP]`. The skip is expected when running against the pre-ALP-439 codebase. |
| (any scenario) `unhandled exception in scenario: ...` | The scenario's kernel raised before it could produce a `ScenarioResult` — a structural error in the fixture builder or a regression in the kernel itself. | Re-run with `--output json` to see the exception repr in the structured output, then read the offending scenario's source in `src/alphamind/scripts/verify_continuous_monitor.py`. |
| `Pipeline scheduler missing (the 04b branch is skipped — not a failure)` | The `EMERGENCY_INVOCATION_REQUESTED` vocabulary is present (ALP-439 landed) but the pipeline scheduler (ALP-431) is not — the verify script does NOT require the scheduler. | No action — scenario (k) exercises the monitor's emit path; the receiver is the scheduler's concern, not the monitor's. The monitor-side test passes as long as the entry is written. |

For Alpaca-subscription failures (which surface only in live operation,
not in the verify script), see [`RUNBOOK_broker_adapter.md` § Failure-mode
triage](RUNBOOK_broker_adapter.md). The verify script cannot reach
Alpaca, so credentials missing from `.env` are silently ignored — the
script's fakes do not consult them.

## Live operation

For the production NSSM service:

```powershell
# Install (Administrator PowerShell)
.\scripts\install_monitor_service.ps1

# Configure the service account (one-time; do NOT commit the password)
nssm set alphamind-monitor ObjectName .\YourUsername YourPassword

# Start the service
nssm start alphamind-monitor

# Confirm running
Get-Service alphamind-monitor
```

The daemon writes to `%USERPROFILE%\AlphaMind\logs\monitor.log` (the
canonical Python logger output configured by
`alphamind.execution.continuous_monitor.logging_setup.configure_monitor_logging`)
plus the NSSM-managed `monitor.out.log` / `monitor.err.log` stdio
redirects. The three log files share the same `~/AlphaMind/logs/`
directory as the collector and pipeline-scheduler services for uniform
operator tooling.

To debug live-mode behavior:

```powershell
# Tail the structured log (everything written by `log.<level>(...)`)
Get-Content -Path $env:USERPROFILE\AlphaMind\logs\monitor.log -Tail 100 -Wait

# Tail stdout / stderr (raw print statements + uncaught traceback)
Get-Content -Path $env:USERPROFILE\AlphaMind\logs\monitor.out.log -Tail 100 -Wait
Get-Content -Path $env:USERPROFILE\AlphaMind\logs\monitor.err.log -Tail 100 -Wait
```

To stop and uninstall:

```powershell
# Stop and remove
.\scripts\uninstall_monitor_service.ps1
```

See `install_monitor_service.ps1` for the full NSSM command sequence
(`AppExit Default Restart` with 60s throttle, `Start Automatic
(Delayed)`, stdout / stderr redirect, `AppDirectory` set to the project
root). The script mirrors `install_collector_service.ps1` and
`install_pipeline_scheduler_service.ps1` in shape.

## References

- Story: [ALP-441 — 05 — End-to-end verify + runbook + NSSM install scripts](https://linear.app/alphamind-jatassi/issue/ALP-441)
- Parent issue: [ALP-123 — Continuous monitor](https://linear.app/alphamind-jatassi/issue/ALP-123)
- Architecture: [docs/design/05-execution-layer/architecture.md § 4](../docs/design/05-execution-layer/architecture.md)
- Runtime design: [docs/design/05-execution-layer/continuous-monitor-runtime.md](../docs/design/05-execution-layer/continuous-monitor-runtime.md)
- Breach behavior: [docs/design/06-risk-guardrails/breach-behavior.md](../docs/design/06-risk-guardrails/breach-behavior.md)
- Central e2e runbook: [scripts/RUNBOOK_end_to_end_verification.md](RUNBOOK_end_to_end_verification.md)
- Sibling NSSM scripts: [scripts/install_collector_service.ps1](install_collector_service.ps1), [scripts/install_pipeline_scheduler_service.ps1](install_pipeline_scheduler_service.ps1)
