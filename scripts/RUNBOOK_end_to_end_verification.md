# End-to-End Pipeline Verification Runbook

Operator workflow for verifying the AlphaMind pipeline end-to-end. The
canonical e2e surface is `python -m alphamind.scheduler run --debug-e2e`
(parent issue [ALP-493](https://linear.app/alphamind-jatassi/issue/ALP-493)) —
one production-faithful pass through the full pipeline against a synthetic
portfolio + log-only broker, streaming per-phase + per-agent-call progress
events to a JSONL archive so an agent operator can verify behavior end-to-end
without touching Alpaca and without depending on whatever happens to be in
the paper DB. ALP-502 retired the legacy per-feature pipeline verify scripts;
the debug-e2e CLI replaces them as the single e2e gate.

`scripts/verify_debug_e2e.py` wraps the CLI in a verify harness that runs six
check helpers against the resulting archive. Operator-facing details for the
verify script live in `scripts/RUNBOOK_debug_e2e.md`; this runbook documents
the operator workflow for the full e2e gate plus pointers to the surviving
standalone scripts.

## Purpose

A green `verify_debug_e2e.py` run proves the pipeline composes end-to-end:
Phase 1 ingest → snapshot assembly → `run_analysis_pipeline` (distillation +
3 sector researchers + qualitative + adaptive + synthesizer) →
`run_decision_pipeline` (analyst + strategist + proposal pre-processor + PM)
→ Phase 2 envelope dispatch. The check helpers assert every load-bearing
invariant the prior per-feature verify suite collectively covered:

- All 12 in-invocation phases produced both `phase_start` + `phase_done`
  events in dependency-respecting order (parent issue § D). The
  pre-invocation `seed` event lands under
  `<archive>/invocations/_pre_invocation/progress.jsonl` and is
  intentionally NOT part of the real-invocation stream the verify
  script inspects — operators can read that file directly for
  seed-step debugging.
- All 10 SDK call pairs landed with `agent_request`/`agent_response` pairs
  carrying `duration_s` / `input_tokens` / `output_tokens` / `tool_calls` /
  `stop_reason` (parent issue § E). The 10 are: distillation, 3 domain
  researchers (tech_semis, financials, energy), qualitative, adaptive,
  synthesizer, analyst, strategist, pm.
- The synthetic portfolio seeded cleanly (8 positions, 8 theses, cash
  ledger at $24,440).
- No Alpaca HTTP traffic leaked into the subprocess's captured
  stderr stream.
- The orchestrator's `InvocationSummary` reports
  `trigger_source="debug_e2e_cli"`, `staleness_flag=false`,
  `commands_submitted >= 0`.

## Prerequisites

1. **Working directory** — the AlphaMind repo root.
2. **`CLAUDE_CODE_OAUTH_TOKEN` exported.** Generate with
   `claude setup-token` if missing. Source `.env` inline before each
   debug-e2e invocation:
   `set -a && source .env && set +a && uv run python scripts/...`.
   Alpaca credentials are **deliberately not required** — the debug-e2e
   mode replaces the broker adapter with a log-only stand-in.
3. **`uv sync` completed.**
4. **Dedicated debug DB.** Debug-e2e wipes its DB on every invocation
   (parent issue § C, decision 2b — full reproducibility). The seeder
   refuses to wipe any path whose basename does not end with
   `-debug-e2e.db`. The canonical path is `data/alphamind-debug-e2e.db`;
   any sibling path (e.g. `data/scratch-debug-e2e.db`) is also accepted.
   The orchestrator creates the file on first invocation; no separate
   `alembic upgrade head` is needed because the same migration chain
   the production DB runs on is applied.

## Invocation

The wrapped verify harness is the standard entry point:

```bash
set -a && source .env && set +a && \
    uv run python scripts/verify_debug_e2e.py \
        --archive-root .archive/verify-debug-e2e \
        [--db-path data/alphamind-debug-e2e.db] \
        [--run-type market_hours_rolling] \
        [--reason "verify_debug_e2e"]
```

It subprocesses one `python -m alphamind.scheduler run --debug-e2e --once
<run_type> --reason <text>`, parses the resulting archive, and prints one
PASS/FAIL line per check. Exit code is 0 on full pass.

To drive the CLI directly without the wrapper (e.g., when iterating on
the underlying mode rather than the check semantics):

```bash
set -a && source .env && set +a && \
    DATABASE_PATH=data/alphamind-debug-e2e.db \
        uv run python -m alphamind.scheduler run \
            --debug-e2e --once market_hours_rolling \
            --reason "debug iteration"
```

## Expected output

Seven PASS lines on a clean run, in order:

```
PASS: auth — all required env vars present (CLAUDE_CODE_OAUTH_TOKEN)
PASS: subprocess — debug-e2e subprocess exited 0
PASS: archive_directory — directory + resolved_config.json + progress.jsonl present at <archive>/invocations/<id>
PASS: jsonl_ordering — 12/12 phases with paired start/done in dependency order; 10/10 SDK call pairs matched
PASS: synthetic_portfolio — positions=8, theses=8, cash_ledger.current_cash_usd=24440.0
PASS: no_alpaca — no alpaca indicators in captured stream
PASS: invocation_summary — staleness_flag=false, trigger_source='debug_e2e_cli', commands_submitted=N
=== DEBUG-E2E VERIFICATION === 7/7 checks passed
```

The archive directory carries:

```
<archive-root>/invocations/<invocation_id>/
├── resolved_config.json
├── progress.jsonl                  # append-only event log (fsync per write)
├── data_calibration_state.json
└── (per-agent diagnostic subdirs   — analysis/<agent>/, decision/<agent>/ —
    populated by the agent harnesses)
```

`scripts/build_e2e_report.py` renders the archive into a single dark-mode
HTML page summarizing the run:

```bash
uv run python scripts/build_e2e_report.py \
    --archive-root .archive/verify-debug-e2e
# auto-discovers --invocation-id when exactly one invocation directory exists
```

The report shows a 12-phase verdict ribbon, the SDK-call table with each
call's `agent_response` fields, the `resolved_config.json` payload, an
incomplete-phase failure section, and a collapsible `pipeline.log`
excerpt. Open the file in a browser — it carries its own CSS via the
sidecar at `scripts/_e2e_report_assets/style.css`.

## Wall-clock + cost

A clean debug-e2e run wires one Sonnet pass through the analysis layer
(distillation + 3 sector researchers + qualitative + adaptive +
synthesizer = 7 Sonnet calls) and one Opus pass through the four
decision agents (analyst + strategist + PM = 3 Opus calls; the proposal
pre-processor is deterministic and emits no SDK call). Approximate
cost:

| Layer    | Model  | Input tokens | Output tokens |
|----------|--------|--------------|---------------|
| analysis | Sonnet | ~54K–70K     | ~7.6K–13.6K   |
| decision | Opus   | ~68K–102K    | ~57K–100K     |

Roughly 10–15% of the nominal weekly Sonnet cap and a smaller slice of
the Opus cap per `docs/design/cost-and-rate-limit-modeling.md`. Don't
re-run gratuitously.

Wall-clock: ~5–15 minutes end-to-end; the strategist scenario is the
typical long pole. See the archive's `progress.jsonl` for per-phase
timings.

## Failure-mode triage

| `verify_debug_e2e.py` FAIL line                          | Likely cause | First fix to try |
|----------------------------------------------------------|--------------|------------------|
| `FAIL: auth — missing required env var(s)`               | `.env` not sourced | Re-run with `set -a && source .env && set +a && uv run …` |
| `FAIL: subprocess — debug-e2e subprocess exited N`       | CLI raised internally | Read `stderr` tail in the FAIL message; the orchestrator names the failing layer |
| `FAIL: archive_directory — archive directory missing`    | CLI never wrote the archive | The orchestrator died before `insert_invocation_record` committed — check `~/AlphaMind/logs/pipeline.log` |
| `FAIL: archive_directory — resolved_config.json missing` | Phase 1 prep crashed | Same as above — orchestrator died before the resolved-config writer fired |
| `FAIL: archive_directory — progress.jsonl missing`       | JSONL emitter never wired | Re-verify `DebugE2ESettings.emitter_factory` populated (story ALP-500) |
| `FAIL: jsonl_ordering — missing phase_start for <name>`  | A pipeline stage never emitted its `phase_start` | Cross-check `src/alphamind/pipeline/{analysis,decision}.py` against the 12-in-invocation-phase ladder |
| `FAIL: jsonl_ordering — phase_done for <name> precedes its own phase_start` | An emitter mis-emits | Same as above |
| `FAIL: jsonl_ordering — non-monotonic timestamp`         | Clock skew or out-of-order write | Inspect the JSONL line referenced in the message; the fsync per write should have prevented this |
| `FAIL: jsonl_ordering — missing agent_request/response pair(s)` | An SDK call site bypassed `_harness_core.invoke_sdk` | Cross-check the failing agent's harness against the 10 SDK pairs in parent § E |
| `FAIL: synthetic_portfolio — required table(s) missing`  | DB not migrated to head | Delete the debug DB; the CLI re-migrates on next run |
| `FAIL: synthetic_portfolio — positions count expected 8` | Seeder didn't run | Either the seeder raised (check stderr) or a downstream stage truncated `positions` |
| `FAIL: synthetic_portfolio — cash_ledger.current_cash_usd expected 24440.0` | Seeder bug or a downstream write mutated the row | Cross-check `wipe_and_seed` against `SYNTHETIC_PORTFOLIO.starting_cash_usd` |
| `FAIL: no_alpaca — captured stream carries alpaca indicator(s)` | `LogOnlyAccountStateQueries` not wired | Verify `context.debug_e2e is not None` reaches `phase1_inputs.gather_phase1_inputs`; the import-linter contract should have caught this at lint time |
| `FAIL: invocation_summary — staleness_flag expected false` | Phase 1 saw a stale data source | Inspect the staleness logger output in the pipeline log; debug-e2e seeds fresh state so this is a real regression |
| `FAIL: invocation_summary — trigger_source expected 'debug_e2e_cli'` | CLI dispatch routed to `_run_once` instead of `_run_debug_e2e` | The `--debug-e2e` argparse branch in `__main__.py` regressed |
| `FAIL: invocation_summary — commands_submitted` | The orchestrator's typed return shape changed | Inspect `InvocationSummary` against `scheduler/orchestrator.py` |

For deeper investigation: every agent harness writes its own diagnostic
archive under `<archive>/invocations/<id>/analysis/<agent>/` (and
`decision/<agent>/`) — the same per-agent shape the retired verify
suite produced. Each carries the assembled input bundle, the prompt,
the full SDK response, and a `metadata.json` with token + wall-clock +
tool-call counts.

## Supporting standalone tools

Four standalone scripts survived ALP-502; they serve roles orthogonal
to the pipeline e2e gate and remain operator-runnable:

- `scripts/verify_bootstrap.py` — bootstrap DB invariant check (18
  tables present, row counts within tolerance). Operator-monitoring
  surface. Operates against the snapshotted production DB, not the
  debug DB.
- `scripts/verify_ongoing_collection.py` — collector freshness check
  (14 active collectors producing rows within 2× their configured
  cadence). Operator-monitoring surface. Same DB as above.
- `scripts/verify_bootstrap_calibration_mix.py` — calibration-state
  distribution check on `distillation_ticker_baseline` /
  `distillation_pair_lag`. Operator-monitoring surface.
- `scripts/verify_position_thesis_model.py` — offline pure-function
  verifier for the position-thesis-model type layer (ALP-122). Sub-10s
  runtime; no SDK calls, no DB reads. See
  `scripts/RUNBOOK_position_thesis_model.md` for the dedicated runbook.

## References

- `scripts/RUNBOOK_debug_e2e.md` — operator runbook for the
  `verify_debug_e2e.py` harness.
- `scripts/RUNBOOK_position_thesis_model.md` — failure-mode triage for
  the offline position-thesis-model verifier.
- `docs/design/debug-e2e-mode.md` — design doc for the `--debug-e2e`
  flag (parent issue ALP-493).
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` provenance.
- `docs/design/cost-and-rate-limit-modeling.md` — per-agent token
  budget expectations.
- `src/alphamind/pipeline/analysis.py` — `run_analysis_pipeline`
  composition runner.
- `src/alphamind/pipeline/decision.py` — `run_decision_pipeline`
  composition runner.
- `src/alphamind/scheduler/orchestrator.py` — `run_invocation`
  end-to-end orchestrator.
- `src/alphamind/scheduler/debug_e2e/` — debug-e2e package
  (`configure_debug_e2e`, `DebugE2ESettings`, `wipe_and_seed`,
  `LogOnly*Queries`, `JsonlProgressEmitter`).
- `docs/project-tracker.md` — current build status.
