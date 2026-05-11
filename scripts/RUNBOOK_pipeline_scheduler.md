# Pipeline Scheduler End-to-End Verification Runbook

Operator workflow for the ALP-448 verification artifact that proves the
pipeline scheduler (ALP-431 work tree) drives one ``--once`` invocation
end-to-end through the orchestrator and lands every artifact: the
22-field ``invocations`` row, the Phase 1 / Phase 2 completion
timestamps, the per-invocation activity-log entries, and the archive
directory under ``<archive_root>/invocations/<invocation_id>/``.

## Purpose

The pipeline scheduler is the main entrypoint that wakes the AlphaMind
pipeline on a cron schedule (story 04a) and on emergency-trigger
activity-log writes (story 04b). It owns the per-invocation
``InvocationContext`` and threads Phase 1 → analysis pipeline → decision
pipeline → Phase 2 envelope dispatch through one transaction
(``run_invocation`` in story 03b).

This runbook validates the scheduler post-changes — run it after any
edit to ``src/alphamind/scheduler/`` (driver, emergency, orchestrator,
invocation context assembly, runtime resolver, supervisor, or
``__main__``) and after any cross-layer change that touches the
five-phase compose surface ``run_invocation`` integrates.

## When to run

After every meaningful edit to:

- ``src/alphamind/scheduler/`` (any module).
- The state-persistence write paths the orchestrator consumes
  (``alphamind.execution.state_persistence.write_paths.phase1``,
  ``alphamind.execution.state_persistence.write_paths.phase2``).
- The analysis-pipeline composition runner
  (``alphamind.pipeline.analysis.run_analysis_pipeline``).
- The decision-pipeline composition runner
  (``alphamind.pipeline.decision.run_decision_pipeline``).
- ``config/scheduler.yaml``, ``config/run_types/*.yaml``, or
  ``config/breach_behavior.yaml`` (any operator-tunable scheduler knob).

The unit test suite (``tests/scheduler/``, ``tests/scripts/test_verify_pipeline_scheduler.py``)
covers the testable helpers in isolation; this script covers the
end-to-end ``--once`` invocation those helpers wrap.

## Cost

One ``--once`` invocation spans the full analysis + decision pipelines
— roughly **$1–$5 per run** at current Anthropic pricing across the
domain researchers (Sonnet), synthesizer (Sonnet), and four
decision-layer agents (Opus: analyst + strategist + PM, plus the pure
proposal pre-processor). The cost varies with the active config (which
agents are enabled in the firing run type's ``agents.yaml`` snapshot).
Re-run after meaningful scheduler-layer changes; do not re-run
gratuitously.

## Prerequisites

1. **``CLAUDE_CODE_OAUTH_TOKEN`` set** — generate via ``claude
   setup-token`` per ``docs/architecture/llm-integration.md`` §
   Authentication.
2. **``ALPACA_PAPER_KEY`` / ``ALPACA_PAPER_SECRET`` set** — Phase 1
   reads broker-side fills via the Alpaca paper-broker adapter; without
   the credentials the broker-side fetch raises before the row is
   committed.
3. **``.env`` loading** — if all three live in ``.env`` (the
   dev-machine convention), source it inline before the command:
   ```bash
   set -a && source .env && set +a && \
       uv run python scripts/verify_pipeline_scheduler.py \
           --archive-root ~/AlphaMind/archive
   ```
   Production NSSM services receive the env vars via the service
   account's profile, not via auto-loading ``.env``.
4. **``uv sync`` completed** — the script runs under ``uv run``.
5. **Paper DB migrated to the current head** — the schema check
   verifies the three required tables (``invocations``,
   ``process_lifetimes``, ``activity_log``) exist with all expected
   columns. A pre-migration DB surfaces as a clear ``FAIL: schema``
   block.
6. **``~/AlphaMind/logs/`` and ``~/AlphaMind/archive/`` writable** —
   the script writes the per-invocation archive directory under the
   ``--archive-root``; the scheduler logs to ``pipeline.log`` in the
   shared log directory.

## Invocation

```bash
uv run python scripts/verify_pipeline_scheduler.py \
    --archive-root ~/AlphaMind/archive
```

CLI flags:

- ``--archive-root DIR`` (required) — root of the verification archive.
  The orchestrator writes ``<archive-root>/invocations/<invocation_id>/``
  per the state-persistence design.
- ``--run-type {pre_open|market_hours_rolling|pre_close|off_hours_rolling|weekend_saturday|weekend_sunday|emergency}``
  — firing trigger for the manual invocation (default
  ``market_hours_rolling``). Drives the per-run-type ``agents.yaml`` /
  ``budgets.yaml`` overlay the scheduler resolves to.
- ``--mode {paper|live}`` — trading mode (default ``paper``). Always
  ``paper`` in this work tree; ``live`` is reserved for the future
  go-live runbook.

## Expected output

A passing run produces nine ``PASS`` lines followed by the summary:

```
PASS: auth — all required env vars present (CLAUDE_CODE_OAUTH_TOKEN, ALPACA_PAPER_KEY, ALPACA_PAPER_SECRET)
PASS: schema — tables present: invocations, process_lifetimes, activity_log; invocations carries all 22 expected columns
PASS: process_lifetime — row landed: process_lifetime_id=plt-pipeline-20260511T143000Z-<rand>
PASS: once_invocation — run_invocation returned InvocationSummary (id=<inv-id>)
PASS: row_population — all 22 invocation-row columns populated; trigger_type=manual; JSON columns parse; resolved_config + data_calibration paths exist
PASS: activity_log — N entry/entries: <breakdown>
PASS: archive — directory + resolved_config.json present at <archive>/invocations/<inv-id>
PASS: vocabulary — EventType.EMERGENCY_INVOCATION_REQUESTED ok; RunType.emergency ok; emergency.yaml parses; cooldown=30
=== PIPELINE SCHEDULER VERIFICATION === 9/9 checks passed
```

Exit code: ``0`` on full PASS; non-zero on any FAIL.

### Per-step semantics

1. **auth** — pre-flight check of all three required env vars before
   any DB touch. A missing var fails fast with the missing name in the
   message.
2. **schema** — the three required tables (``invocations``,
   ``process_lifetimes``, ``activity_log``) exist; the ``invocations``
   table carries all 22 expected columns.
3. **process_lifetime** — drives one ``record_process_lifetime`` call
   so the per-invocation row's FK is satisfiable. Returns the row id.
4. **once_invocation** — drives ``run_invocation`` end-to-end with
   ``trigger_type=manual``, ``trigger_source=verify_pipeline_scheduler``,
   ``trigger_reason="e2e verify"``. Returns an ``InvocationSummary``.
5. **row_population** — queries the freshly-written ``invocations`` row
   and asserts all 22 fields are populated, ``trigger_type='manual'``,
   the two JSON columns parse cleanly, and both snapshot paths exist
   on disk.
6. **activity_log** — counts entries for this invocation and emits a
   per-event-type breakdown. At least one entry must exist.
7. **archive** — asserts
   ``<archive_root>/invocations/<invocation_id>/resolved_config.json``
   exists.
8. **vocabulary** — asserts the story-04b additions are wired
   (``EventType.EMERGENCY_INVOCATION_REQUESTED``, ``RunType.emergency``,
   ``config/run_types/emergency.yaml`` parses,
   ``BreachBehaviorConfig.emergency_invocation_cooldown_minutes == 30``).

## Failure-mode triage

Bullet-list format (Linear's renderer truncates table cells under
nested-list contexts):

- **``FAIL: auth — missing required env var(s): ...``.** Likely cause —
  the operator forgot to source ``.env`` before invoking the script, or
  one of the three required env vars is genuinely absent. Triage —
  follow the message; source ``.env`` per the prerequisites section,
  or set the missing var directly via ``export``. No DB writes happen
  before this check.

- **``FAIL: schema — missing tables: ...``.** Likely cause — the script
  is pointed at a pre-migration DB (the most common case is a freshly
  created SQLite file with no tables) or an empty path. Triage — run
  ``uv run alembic upgrade head`` against the configured DB to apply
  the migration head, then re-run.

- **``FAIL: schema — invocations missing columns: ...``.** Likely cause
  — the DB is at an older migration revision than the current code
  (some pipeline-scheduler stories add new columns). Triage — same as
  above; run the migration.

- **``FAIL: process_lifetime — record_process_lifetime raised: ...``.**
  Likely cause — ``git rev-parse HEAD`` failed (no git binary in PATH,
  detached worktree, repository corruption) or the configured DB is
  not writable. Triage — verify ``git rev-parse HEAD`` works in the
  working directory, then check the DB path and permissions.

- **``FAIL: once_invocation — run_invocation raised: <exc>: <msg>``.**
  Likely cause — the orchestrator hit a layer-internal failure. Common
  cases: the analysis-pipeline composition raised because the
  configured run-type lacks an enabled agent; the decision-pipeline
  composition raised a ``HarnessFailure`` from one of its four agents;
  Phase 1 raised because the Alpaca paper adapter rejected the
  credentials. **Re-running is expensive** ($1-$5 per invocation per
  the Cost section). Triage from ``pipeline.log`` +
  ``~/AlphaMind/archive/invocations/<latest>/`` before re-invoking. The
  exception's type + message names the failing layer; consult the
  per-layer runbook (``RUNBOOK_decision_pipeline.md``,
  ``RUNBOOK_synthesizer.md``, ``RUNBOOK_broker_adapter.md``,
  ``RUNBOOK_state_persistence.md``) for failure-mode triage.

- **``FAIL: row_population — columns NULL on invocation row: ...``.**
  Likely cause — the orchestrator returned early (before
  ``stamp_phase_completion`` for Phase 1 or Phase 2) without raising,
  leaving the row in a partial state. Triage — inspect ``pipeline.log``
  for the orchestrator's last log line; the row carries the
  ``invocation_id`` from the FAIL message so the SQL trail is
  reproducible.

- **``FAIL: row_population — trigger_type expected 'manual', got ...``.**
  Likely cause — the verify script's invocation got mixed up with a
  concurrent scheduled or emergency invocation (e.g. the daemon is
  also running). Triage — stop the daemon before running the verify
  script.

- **``FAIL: row_population — resolved_config_snapshot_path not on disk:
  ...``.** Likely cause — the archive directory was deleted or the
  ``--archive-root`` doesn't match what the orchestrator wrote to.
  Triage — re-run with a consistent ``--archive-root``; check
  filesystem permissions on the directory tree.

- **``FAIL: activity_log — no activity_log entries found for
  invocation_id=...``.** Likely cause — the orchestrator's baseline
  ``DISTILLATION_CONFIG_CHANGE`` emission was suppressed (the
  ``read_most_recent_config_change_new_hash`` de-dup matched the prior
  invocation's hash) and Phase 1 / Phase 2 emitted no fill / command
  entries. The baseline emission writes one entry per config-version on
  first invocation, then suppresses on subsequent invocations with
  byte-identical config; a fresh DB always emits at least the baseline.
  Triage — verify the DB is at the expected migration head and that
  ``read_most_recent_config_change_new_hash`` is reading the same
  ``config_file`` key (``"config/distillation.yaml"``). For a
  guaranteed re-emission, make a trivial change to
  ``config/distillation.yaml`` (e.g. bump a budget by one).

- **``FAIL: archive — archive directory missing: ...``.** Likely cause
  — the orchestrator returned without committing the per-invocation
  transaction (e.g. via a halt-state early-exit not yet wired in story
  03b). Triage — inspect ``pipeline.log`` for the orchestrator's last
  state; the archive directory is created on ``open_invocation``
  enter, so its absence means the context manager never entered.

- **``FAIL: vocabulary — ...``.** Likely cause — a regression in story
  04b's additions (someone removed ``RunType.emergency`` or changed
  the cooldown default). Triage — git log
  ``src/alphamind/config/models/run_types.py`` /
  ``src/alphamind/portfolio_state/events/activity_log.py`` /
  ``config/breach_behavior.yaml`` to find the offending commit.

## Daemon-mode sanity check

After a green PASS run, optionally validate the daemon-mode start path
in a foreground shell:

```bash
set -a && source .env && set +a
uv run python -m alphamind.scheduler run --mode paper
```

Expected behavior:

1. The script logs an opening line at ``INFO`` level: ``pipeline
   scheduler session start: process_lifetime_id=plt-pipeline-... mode=paper``.
2. ``pipeline.log`` (under ``~/AlphaMind/logs/``) shows the next-fire
   schedule for each registered cron task per ``config/scheduler.yaml``
   (story 04a).
3. The emergency-receiver task appears in the supervisor's task
   registry; if you write a manual
   ``EMERGENCY_INVOCATION_REQUESTED`` activity-log entry, the next
   poll cycle (per ``emergency_poll_interval_seconds`` in
   ``scheduler.yaml``) drives one emergency invocation.
4. Press Ctrl-C: the supervisor calls ``shutdown_event.set()``,
   cancels both tasks within the
   ``supervisor_shutdown_timeout_seconds`` budget, and the script logs
   ``pipeline scheduler session end: process_lifetime_id=...`` before
   exiting cleanly.

Logs land at:

- ``~/AlphaMind/logs/pipeline.log`` — the structured scheduler log.
- ``~/AlphaMind/logs/pipeline.out.log`` / ``~/AlphaMind/logs/pipeline.err.log``
  — NSSM stdout/stderr redirects (production only).

## NSSM service registration

For production deployment, register the scheduler as the
``alphamind-scheduler`` Windows Service via the install script:

```powershell
# As Administrator, from the project root:
.\scripts\install_pipeline_scheduler_service.ps1
```

The script mirrors ``install_collector_service.ps1`` adapted for the
scheduler entry point (``python -m alphamind.scheduler run``). After
install, the operator must set the service account so that
``%USERPROFILE%`` resolves to the right archive + log directories:

```powershell
nssm set alphamind-scheduler ObjectName <DOMAIN>\<USER> <PASSWORD>
nssm start alphamind-scheduler
Get-Service alphamind-scheduler
```

To unregister, use the matching uninstall script:

```powershell
.\scripts\uninstall_pipeline_scheduler_service.ps1
```

Service name: ``alphamind-scheduler`` (parallel to ``alphamind-collector``
and the future ``alphamind-monitor``).

## Where artifacts land

- ``<archive_root>/invocations/<invocation_id>/resolved_config.json`` —
  the resolved pipeline-config snapshot atomically written by
  ``open_invocation`` on enter.
- ``<archive_root>/invocations/<invocation_id>/data_calibration_state.json``
  — the data-calibration state snapshot (possibly ``{}`` for the
  first-ever invocation).
- ``<archive_root>/process_lifetimes/<process_lifetime_id>/pip_freeze.txt``
  — the per-process-start ``pip freeze`` snapshot.
- DB rows under ``invocations`` and ``activity_log`` keyed by
  ``invocation_id``; the row writer threads the same id into the
  archive directory layout above.

## Known scope boundaries

1. **Paper mode only.** ``--mode live`` is reserved for the future
   go-live runbook. The verify script accepts ``--mode live`` but the
   work tree's verification gate is paper-only.
2. **The verify script drives one invocation per run.** It does not
   exercise the cron firing path (story 04a) or the emergency-trigger
   poll path (story 04b); those run inside the long-running daemon.
   The runbook's daemon-mode sanity check covers the wiring; a true
   end-to-end production validation lives under the central runbook's
   pipeline-scheduler phase plus the operator's monitoring.
3. **Known orchestrator stub: ``_EmptySynthesizerReader``.** The
   orchestrator currently wires an empty synthesizer reader (returns
   no positions / theses / exposure) at
   ``src/alphamind/scheduler/orchestrator.py``'s ``_EmptySynthesizerReader``.
   The natural production reader is
   :class:`SnapshotBackedSynthesizerReader`
   (``src/alphamind/portfolio_state/consumers/synthesizer.py:175``)
   backed by ``assemble_snapshot``. Wiring it requires a snapshot
   that reads Phase-1-committed data, but the orchestrator's
   ``InvocationContext`` transaction does not commit between Phase 1
   and the synthesizer call — a separate session opened by the
   repository factory cannot see the open transaction's uncommitted
   row. Deferred to a follow-up story scoped to cross-transaction
   snapshot visibility. Tests stub ``run_analysis_pipeline`` so the
   empty reader is never invoked under unit tests; the ``--once``
   verify path may exhibit reduced synthesizer fidelity until the
   deferred wiring lands.

## References

- ``src/alphamind/scripts/verify_pipeline_scheduler.py`` — the
  canonical verify-script module with the testable helpers.
- ``tests/scripts/test_verify_pipeline_scheduler.py`` — unit tests for
  the testable helpers (auth, schema, row population, activity log,
  archive directory, vocabulary).
- ``src/alphamind/scheduler/orchestrator.py`` — the ``run_invocation``
  function the verify script drives.
- ``src/alphamind/scheduler/__main__.py`` — the ``python -m
  alphamind.scheduler`` entry point; the verify script mirrors its
  ``_run_once`` body.
- ``scripts/install_pipeline_scheduler_service.ps1`` /
  ``scripts/uninstall_pipeline_scheduler_service.ps1`` — NSSM
  registration helpers for production.
- ``scripts/RUNBOOK_end_to_end_verification.md`` § pipeline-scheduler
  phase — cross-feature sequencing.
- ``docs/design/05-execution-layer/state-persistence.md`` § Invocation
  records — the 22-column row contract.
- ``docs/architecture/infrastructure.md`` § Deployment + Process
  supervision — NSSM service shape, log directory layout, service
  account guidance.
- ``docs/architecture/llm-integration.md`` § Authentication —
  ``CLAUDE_CODE_OAUTH_TOKEN`` setup.
