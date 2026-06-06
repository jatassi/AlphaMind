# 05 — End-to-end verify + `RUNBOOK_pipeline_scheduler.md` + NSSM install scripts

## Goal

Ship the operator-runnable deliverables that close out the pipeline scheduler work tree: a per-feature verify script that drives a real `--once` invocation against the paper DB and asserts every artifact landed, a per-feature runbook the operator follows to validate the scheduler post-changes, the NSSM install/uninstall PowerShell scripts that register `alphamind-scheduler` as a Windows Service parallel to `alphamind-collector` and the future `alphamind-monitor`, and the cross-feature insertion into `scripts/RUNBOOK_end_to_end_verification.md` placing pipeline-scheduler verification in the dependency-ordered phase list. Per parent issue's load-bearing emphasis, the verify script is the acceptance gate for the whole work tree.

## Reading

* Parent issue `ALP-431` § Notes for the orchestrator — verify script is load-bearing; assert every artifact landed (22-field row + Phase 1/2 timestamps + activity-log entries + archive directory)
* `docs/architecture/infrastructure.md` § Deployment + Process supervision — NSSM service shape, log directory layout, service-account guidance
* `scripts/install_collector_service.ps1` + `scripts/uninstall_collector_service.ps1` — the precedent pair this story mirrors
* `scripts/verify_decision_pipeline.py` + `src/alphamind/scripts/verify_decision_pipeline.py` — verify-script shape this story mirrors (thin shim + canonical module)
* `scripts/RUNBOOK_decision_pipeline.md` — runbook shape this story mirrors
* `scripts/RUNBOOK_end_to_end_verification.md` — central runbook; insert pipeline-scheduler into the dependency-ordered phase list
* `src/alphamind/scheduler/__main__.py` (story 01) — `--once` CLI entry point the verify script invokes
* `src/alphamind/scheduler/orchestrator.py` (story 03b) — `run_invocation` semantics the verify script exercises
* `src/alphamind/scheduler/driver.py` (story 04a) + `src/alphamind/scheduler/emergency.py` (story 04b) — the two tasks the daemon runs

## Depends on

* `ALP-446` (this work tree, story 04a) — APScheduler driver must be wired so the daemon-mode validation in the runbook works.
* `ALP-447` (this work tree, story 04b) — emergency receiver + the EventType / RunType.emergency / cooldown additions, so the verify script can cover the emergency path.

## Scope

Verify script under `src/alphamind/scripts/verify_pipeline_scheduler.py` (canonical module) + `scripts/verify_pipeline_scheduler.py` (thin shim). Runbook at `scripts/RUNBOOK_pipeline_scheduler.md`. NSSM scripts at `scripts/install_pipeline_scheduler_service.ps1` and `scripts/uninstall_pipeline_scheduler_service.ps1`. Central runbook edit at `scripts/RUNBOOK_end_to_end_verification.md`. Tests at `tests/scripts/test_verify_pipeline_scheduler.py`.

### 1\. Canonical verify-script module

`src/alphamind/scripts/verify_pipeline_scheduler.py`:

```python
def main(argv: list[str] | None = None) -> int:
    """Verify the pipeline scheduler end-to-end against the paper DB.

    Drives one manual --once invocation through run_invocation, asserts every
    artifact landed, and prints a per-check PASS/FAIL summary an operator can
    read. Exit 0 on all-pass; non-zero on any FAIL.
    """
```

Argparse surface:

* `--archive-root DIR` (required)
* `--run-type {pre_open|market_hours_rolling|pre_close|off_hours_rolling|weekend_saturday|weekend_sunday|emergency}` (default `market_hours_rolling`)
* `--mode {paper|live}` (default `paper`)

Procedure (each step prints `PASS:` or `FAIL: <reason>`):

1. **Auth check.** `CLAUDE_CODE_OAUTH_TOKEN` is in env; `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET` are in env.
2. **DB schema check.** Open the configured DB and confirm `invocations`, `process_lifetimes`, `activity_log` tables exist with the expected columns.
3. **Process lifetime row write.** Record a fresh `process_lifetimes` row via `record_process_lifetime(role="pipeline", ...)` and assert it landed.
4. `--once` manual invocation. Drive `run_invocation(trigger_type="manual", trigger_source="verify_pipeline_scheduler", trigger_reason="e2e verify", firing_run_type=<run_type>, ...)` end-to-end. Assert it returns an `InvocationSummary`.
5. **Row population check.** Query the freshly-written `invocations` row. Assert: all 22 fields are populated (16 non-NULL at insert + Phase 1/2 timestamps + 2 summary JSONs + staleness_flag + snapshot_metadata_json); `phase1_completed_at` and `phase2_completed_at` are both populated; `trigger_type='manual'`; `active_overlays_json` and `feature_flags_snapshot_json` parse as valid JSON; `resolved_config_snapshot_path` points to an existing file with content; `data_calibration_state_snapshot_path` points to an existing file (possibly `{}`).
6. **Activity-log check.** Query `activity_log` for entries with this invocation's ID. Assert at least one entry exists (any subsystem may have emitted one — `FILL_PROCESSOR` if fills landed, `COMMAND_EXECUTOR` if commands landed, `CONFIG_RELOAD` always on first invocation per config change). Print the count and event-type breakdown.
7. **Archive directory check.** Assert `<archive_root>/invocations/<invocation_id>/` exists and contains at least `resolved_config.json` (and `data_calibration_state.json`).
8. **Vocabulary check (story 04b additions).** Assert `EventType.EMERGENCY_INVOCATION_REQUESTED` is importable from `alphamind.portfolio_state.events.activity_log`; assert `RunType.emergency` is a member of `RunType`; assert `config/run_types/emergency.yaml` parses cleanly via the resolver; assert `BreachBehaviorConfig.emergency_invocation_cooldown_minutes == 30` after loading `config/breach_behavior.yaml`.
9. **Summary.** Print `=== PIPELINE SCHEDULER VERIFICATION === N/N checks passed` and exit 0 on full pass, non-zero on any FAIL.

Per the verify-script convention, the testable predicates are factored into helpers under the canonical module so `tests/scripts/test_verify_pipeline_scheduler.py` can exercise them without touching the real LLM SDK or broker. The end-to-end `--once` invocation IS exercised in the verify script itself (not in unit tests).

### 2\. Thin shim

`scripts/verify_pipeline_scheduler.py`:

```python
"""Thin shim — defer to :mod:`alphamind.scripts.verify_pipeline_scheduler`."""
from __future__ import annotations
import sys
from alphamind.scripts.verify_pipeline_scheduler import main
if __name__ == "__main__":
    sys.exit(main())
```

### 3\. Per-feature runbook

`scripts/RUNBOOK_pipeline_scheduler.md`. Sections:

* **Purpose.** The scheduler is the main entrypoint; this runbook validates it post-changes.
* **Prerequisites.** `uv sync` complete; `.env` populated; paper DB migrated; `CLAUDE_CODE_OAUTH_TOKEN`, `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET` set; `~/AlphaMind/logs/` and `~/AlphaMind/archive/` writable.
* **Invocation.** `uv run python scripts/verify_pipeline_scheduler.py --archive-root ~/AlphaMind/archive`.
* **Expected output.** Sample stdout showing 9 PASS lines + summary line. Include the per-step semantics.
* **Failure-mode triage table.** Rows for: auth failures; DB schema mismatches; process_lifetimes write failure (git not available); orchestrator dispatch exception; row-population mismatch; activity-log empty; archive directory missing; vocabulary additions missing.
* **Daemon-mode sanity check.** Procedure to start `python -m alphamind.scheduler run --mode paper` in a foreground shell, confirm `pipeline.log` shows the next-fire schedule + the emergency-receiver task, then Ctrl-C cleanly and confirm the shutdown line.
* **NSSM service registration.** Pointer to `scripts/install_pipeline_scheduler_service.ps1` and the post-install operator-account configuration step.

### 4\. NSSM install script

`scripts/install_pipeline_scheduler_service.ps1`. Mirror `install_collector_service.ps1` with these adjustments:

* `$ServiceName = 'alphamind-scheduler'`
* `nssm install $ServiceName $PythonExe '-m' 'alphamind.scheduler' 'run'` (no positional subcommand args beyond `run`)
* `$StdoutLog = Join-Path $LogDir 'pipeline.out.log'`
* `$StderrLog = Join-Path $LogDir 'pipeline.err.log'`
* `AppRestartDelay 60000`, `Start SERVICE_DELAYED_AUTO_START`, `AppExit Default Restart`, `AppStdoutCreationDisposition 4`, `AppStderrCreationDisposition 4` — all matching the collector

Operator-account configuration step in the post-install printout is the same shape as the collector's.

### 5\. NSSM uninstall script

`scripts/uninstall_pipeline_scheduler_service.ps1`. Mirror `uninstall_collector_service.ps1` with:

* `$ServiceName = 'alphamind-scheduler'`
* Standard `nssm stop`, `nssm remove ... confirm` sequence

### 6\. Central runbook insertion

Edit `scripts/RUNBOOK_end_to_end_verification.md`. Insert a new phase for the pipeline scheduler in the dependency-ordered phase list:

* After the existing decision-pipeline / OMS / state-persistence phases (since the scheduler depends on every layer)
* Before any later consumer phases (continuous monitor, command center)
* The phase entry follows the existing template: prerequisite list, invocation command, expected output, downstream phase pointer (which is the continuous-monitor phase added by [ALP-441](https://linear.app/alphamind-jatassi/issue/ALP-441/05-end-to-end-verify-runbook-nssm-install-scripts))

### Out of scope

* Continuous monitor's verify / runbook / NSSM install — owned by `ALP-441` (story 05 of [ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor)).
* Command-center verify / runbook — owned by the future command-center work tree.
* Live-mode verification — paper mode only in this work tree.

## Acceptance criteria

- [ ] `uv run python scripts/verify_pipeline_scheduler.py --archive-root <DIR>` against the paper DB exits 0 and prints 9 PASS lines + summary.
- [ ] The verify script's per-check helpers are importable from `alphamind.scripts.verify_pipeline_scheduler` and unit-testable.
- [ ] On a missing `CLAUDE_CODE_OAUTH_TOKEN`, the auth check fails with a clear message and the script exits non-zero before any DB write.
- [ ] On a missing `~/AlphaMind/archive/invocations/<id>/resolved_config.json` post-invocation, the archive check fails with a clear message.
- [ ] `scripts/RUNBOOK_pipeline_scheduler.md` exists with all sections from § 3 of this story; lint-clean (no broken links, no missing-prereq mismatches against scripts).
- [ ] `scripts/install_pipeline_scheduler_service.ps1` mirrors the collector pair and lints clean under PowerShell `Set-StrictMode -Version Latest`.
- [ ] `scripts/uninstall_pipeline_scheduler_service.ps1` mirrors the collector pair.
- [ ] `scripts/RUNBOOK_end_to_end_verification.md` gains a new pipeline-scheduler phase placed correctly in the dependency-ordered list.
- [ ] `tests/scripts/test_verify_pipeline_scheduler.py` covers the per-check predicates and passes under `uv run pytest tests/scripts/test_verify_pipeline_scheduler.py -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run python scripts/verify_pipeline_scheduler.py --archive-root ~/AlphaMind/archive` — exits 0 with 9/9 PASS.
* `uv run pytest tests/scripts/test_verify_pipeline_scheduler.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: read through `scripts/RUNBOOK_pipeline_scheduler.md` end-to-end as a fresh operator; every step is reproducible.
* Manual: read the central-runbook insertion as a fresh operator following the end-to-end phase order; the pipeline-scheduler phase fits naturally.
* Lint clean per CLAUDE.md.