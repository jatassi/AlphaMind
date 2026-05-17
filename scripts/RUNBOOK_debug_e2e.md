# Debug-E2E Verification Runbook

Operator runbook for `scripts/verify_debug_e2e.py` — the verify harness
that wraps `python -m alphamind.scheduler run --debug-e2e` (parent issue
[ALP-493](https://linear.app/alphamind-jatassi/issue/ALP-493)). For the
broader e2e-gate context (when to run, cost expectations, surviving
standalone tools), see `scripts/RUNBOOK_end_to_end_verification.md`.

## Purpose

`verify_debug_e2e.py` runs one debug-e2e subprocess and asserts every
load-bearing invariant the prior per-feature verify suite collectively
covered (story [ALP-502](https://linear.app/alphamind-jatassi/issue/ALP-502)).
Six check helpers fire against the resulting archive + DB; exit 0 on
full pass.

## Prerequisites

1. **Working directory** — the AlphaMind repo root.
2. **`CLAUDE_CODE_OAUTH_TOKEN` exported.** Source `.env` inline:
   ```bash
   set -a && source .env && set +a
   ```
   Alpaca credentials are NOT required — debug-e2e replaces the broker
   adapter with the log-only stand-in.
3. **`uv sync` completed.**
4. **DB path ends with `-debug-e2e.db`.** The default is
   `data/alphamind-debug-e2e.db`; the seeder refuses to wipe any path
   that does not match the suffix (parent issue § C, decision 2b).

## Invocation

```bash
set -a && source .env && set +a && \
    uv run python scripts/verify_debug_e2e.py \
        --archive-root .archive/verify-debug-e2e \
        [--db-path data/alphamind-debug-e2e.db] \
        [--run-type market_hours_rolling] \
        [--reason "verify_debug_e2e"]
```

Argparse surface:

- `--archive-root DIR` (required) — root of the verification archive.
  The CLI writes the per-invocation directory under
  `<archive-root>/invocations/<invocation_id>/`.
- `--db-path PATH` (default `data/alphamind-debug-e2e.db`) — SQLite DB
  the debug-e2e mode targets. Must end with `-debug-e2e.db`.
- `--run-type {pre_open,market_hours_rolling,pre_close,off_hours_rolling,weekend_saturday,weekend_sunday,emergency}`
  (default `market_hours_rolling`) — firing run type for the manual
  invocation.
- `--reason TEXT` (default `verify_debug_e2e`) — free-form reason
  recorded on the invocation row.
- `--pipeline-log PATH` (default `~/AlphaMind/logs/pipeline.log`) — log
  path scanned by `check_no_alpaca`.

## Expected output

```
PASS: auth — all required env vars present (CLAUDE_CODE_OAUTH_TOKEN)
PASS: subprocess — debug-e2e subprocess exited 0
PASS: archive_directory — directory + resolved_config.json + progress.jsonl present at <archive>
PASS: jsonl_ordering — 13/13 phases with paired start/done in dependency order; 10/10 SDK call pairs matched
PASS: synthetic_portfolio — positions=8, theses=8, cash_ledger.current_cash_usd=24440.0
PASS: no_alpaca — no alpaca indicators in pipeline log at ~/AlphaMind/logs/pipeline.log
PASS: invocation_summary — staleness_flag=false, trigger_source='debug_e2e_cli', commands_submitted=N
=== DEBUG-E2E VERIFICATION === 7/7 checks passed
```

## Failure-mode triage

See `scripts/RUNBOOK_end_to_end_verification.md` § Failure-mode triage
for the full table. The short form:

- `auth` FAIL → source `.env`.
- `subprocess` FAIL → read the stderr tail in the message; the
  orchestrator's exception type + message names the failing layer.
- `archive_directory` FAIL → CLI died before the archive landed; check
  `~/AlphaMind/logs/pipeline.log`.
- `jsonl_ordering` FAIL → the message names the missing phase or
  unpaired SDK call; cross-reference
  `src/alphamind/pipeline/{analysis,decision}.py` for the expected
  emit sites.
- `synthetic_portfolio` FAIL → seeder bug or a downstream stage
  mutated the seeded rows; inspect
  `src/alphamind/scheduler/debug_e2e/seed.py` against
  `SYNTHETIC_PORTFOLIO`.
- `no_alpaca` FAIL → the log-only broker isn't wired; verify
  `context.debug_e2e` reaches `gather_phase1_inputs`.
- `invocation_summary` FAIL → `InvocationSummary` shape changed or
  the CLI dispatched to `_run_once` instead of `_run_debug_e2e`.

## References

- `scripts/RUNBOOK_end_to_end_verification.md` — central e2e runbook.
- `docs/design/debug-e2e-mode.md` — design doc.
- `scripts/build_e2e_report.py` — HTML report builder consuming the
  archive.
