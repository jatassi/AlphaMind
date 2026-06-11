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

`scripts/verify/verify_debug_e2e.py` wraps the CLI in a verify harness that runs six
check helpers against the resulting archive. This runbook is the single
operator-facing entry point — argparse surface, expected output, failure
triage, and pointers to the surviving standalone scripts all live below.

## Purpose

A green `verify_debug_e2e.py` run proves the pipeline composes end-to-end:
fill collection → snapshot assembly → `run_analysis_pipeline` (distillation +
3 sector researchers + qualitative + adaptive + synthesizer) →
`run_decision_pipeline` (analyst + strategist + proposal pre-processor + PM)
→ command execution envelope dispatch. The check helpers assert every load-bearing
invariant the prior per-feature verify suite collectively covered:

- All 12 in-invocation phases produced both `phase_start` + `phase_done`
  events in dependency-respecting order (parent issue § D). The
  pre-invocation `seed` event lands under
  `<archive>/<YYYY-MM-DD>/_pre_invocation/progress.jsonl` and is
  intentionally NOT part of the real-invocation stream the verify
  script inspects — operators can read that file directly for
  seed-step debugging.
- All 9 SDK call pairs landed with `agent_request`/`agent_response` pairs
  carrying `duration_s` / `input_tokens` / `cache_read_tokens` /
  `cache_write_tokens` / `output_tokens` / `tool_calls` / `stop_reason`
  (parent issue § E; cache split added per ALP-701 so the operator can
  tell apart a cache-hit prompt from a broken context-assembly path).
  The 9 are: 3 domain researchers (tech_semis, financials, energy),
  qualitative, adaptive, synthesizer, analyst, strategist, pm.
  Distillation is the deterministic 7-phase numerical orchestrator and
  emits no SDK call.
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
   `set -a && source <(tr -d '\r' < .env) && set +a && uv run python scripts/...`.
   Alpaca credentials are **deliberately not required** — the debug-e2e
   mode replaces the broker adapter with a log-only stand-in.

   **Strip CR while sourcing `.env`.** On the Windows production
   server `.env` carries CRLF line endings. A bare `source .env` under
   Git Bash leaves a trailing `\r` on every value — including
   `CLAUDE_CODE_OAUTH_TOKEN`. The token still reads as non-empty, so
   the `check_auth` pre-flight passes (`PASS: auth`), but the `\r`
   corrupts the bearer header and SDK authentication fails several
   minutes into the run — at the first agent call, after distillation
   — wasting the whole pass. `source <(tr -d '\r' < .env)` strips the
   CR and is a harmless no-op on an LF-only `.env`, so it is the
   canonical form in every command block below. To confirm a clean
   load before committing to a long run:

   ```bash
   set -a && source <(tr -d '\r' < .env) && set +a && \
       uv run python -c "import os; t=os.environ['CLAUDE_CODE_OAUTH_TOKEN']; \
           print('len', len(t), 'clean', t == t.strip())"
   ```

   should print the token length and `clean True`.
3. **`uv sync` completed.**
4. **Dedicated debug DB seeded from a prod snapshot.** Debug-e2e wipes
   the state-persistence subset on every invocation (parent issue § C,
   decision 2b) but assumes the *data layer* — `asset_universe`,
   `sector_classification`, distillation calibration baselines
   (`distillation_ticker_baseline`, `distillation_pair_lag`), news
   clusters, the event calendar, and the rest of the collector-populated
   tables — is already present. The canonical bootstrap is a file copy
   of the production DB:

   ```bash
   uv run python scripts/verify/snapshot_prod_for_debug_e2e.py
   # add --force to overwrite an existing target
   ```

   This runs from a machine with `data/alphamind.db` accessible (the
   production server, or a Mac dev box with the volume mounted) and
   produces `data/alphamind-debug-e2e.db` — a ~6 GB file with the full
   migrated schema and live data-layer state. On Mac, copy this file
   from the production server (`scp` / SMB share) into the local
   `data/` directory before running the verify.

   The seeder refuses any path whose basename does not end with
   `-debug-e2e.db`. The canonical target is
   `data/alphamind-debug-e2e.db`; any sibling path (e.g.
   `data/scratch-debug-e2e.db`) is also accepted.

   Re-snapshot when (a) alembic migrations land that change the schema,
   (b) you want the latest collector state baked into the verify, or
   (c) you suspect the snapshot has drifted materially from prod.
   `wipe_and_seed` runs over the snapshot on every invocation, so
   per-invocation state-persistence rows never leak between runs.

   **Pre-flight: confirm the snapshot is at alembic head.** Schema drift
   surfaces as a mid-pipeline `OperationalError: no such table: <X>` —
   noisy to triage from the captured stderr tail. Cheap to rule out up
   front:

   ```bash
   DATABASE_PATH=data/alphamind-debug-e2e.db uv run alembic current \
       | tail -1
   uv run alembic heads | tail -1
   ```

   If the two revisions differ, re-snapshot (`scripts/verify/snapshot_prod_for_debug_e2e.py
   --force`) or `alembic upgrade head` against the debug DB before
   invoking the verify. Re-snapshot is the canonical fix because it
   also refreshes the data layer; `upgrade head` is a faster
   schema-only patch when you don't care about collector freshness.

## Invocation

Right after launching the verify, **arm the progress monitor in a
second shell** — see [Monitoring progress mid-run](#monitoring-progress-mid-run)
below. The verify wrapper is silent during the 5–15 minute run, so
without the monitor a stalled SDK call or empty agent response stays
invisible until the wrapper finally exits. Arm AFTER launching (not
before), so the monitor's `ls -td` selector locks onto the new
invocation directory rather than a stale prior run.

The wrapped verify harness is the standard entry point:

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify/verify_debug_e2e.py \
        --archive-root .archive/verify-debug-e2e \
        [--db-path data/alphamind-debug-e2e.db] \
        [--run-type market_hours_rolling] \
        [--reason "verify_debug_e2e"]
```

It subprocesses one `python -m alphamind.scheduler run --debug-e2e --once
<run_type> --reason <text>`, parses the resulting archive, and prints one
PASS/FAIL line per check. Exit code is 0 on full pass.

Argparse surface:

- `--archive-root DIR` (required) — root of the verification archive. The
  CLI writes the per-invocation directory under
  `<archive-root>/<YYYY-MM-DD>/<invocation_id>/` (date-bucketed layout
  introduced by ALP-689 / PR #204; see
  `src/alphamind/_kernel/archive_layout.py`).
- `--db-path PATH` (default `data/alphamind-debug-e2e.db`) — SQLite DB the
  debug-e2e mode targets. Must end with `-debug-e2e.db`.
- `--run-type {market_open,market_hours_rolling,pre_close,off_hours_rolling,weekend_saturday,weekend_sunday,emergency}`
  (default `market_hours_rolling`) — firing run type for the manual
  invocation.
- `--reason TEXT` (default `verify_debug_e2e`) — free-form reason recorded
  on the invocation row.
- `--fresh-start` (default off) — swap the managed `SYNTHETIC_PORTFOLIO`
  fixture (8 positions / 8 theses / $24,440 cash) for the clean-slate
  `FRESH_START_PORTFOLIO` fixture (0 positions / 0 theses / $100,000
  cash). Forwards to the scheduler subprocess and reparameterizes the
  `synthetic_portfolio` check (ALP-618). See the subsection below.
- `--resume-from INVOCATION_ID:PHASE` (default unset) — forward
  `--resume-from` to the scheduler subprocess so it hydrates SDK-phase
  outputs from the named source invocation and re-runs from `<phase>`
  onward (ALP-693 / ALP-696). The wrapper does no validation — the
  scheduler CLI is the source of truth on validity. Mutually exclusive
  with `--fresh-start` (enforced at the scheduler CLI's argparse
  layer). When set, the wrapper also runs the post-resume
  `check_deterministic_prefix` check that hashes the distillation
  outputs against the source archive. See the "Resuming a failed run"
  section below.

`check_no_alpaca` scans the captured subprocess stderr stream directly — no
separate `--pipeline-log` flag is needed.

## `--fresh-start` — clean-slate portfolio (ALP-618)

The default fixture (`SYNTHETIC_PORTFOLIO`) only exercises the
managed-portfolio code path: every analyst run sees 8 pre-existing
positions, every strategist run has 8 theses to assess, and the cash
ledger sits at $24,440. The `--fresh-start` flag flips to the
clean-slate `FRESH_START_PORTFOLIO` fixture so an operator can
characterize the initial-state edge cases — analyst's OPEN
recommendations, strategist's empty-input behavior, PM's dispatch of
newly-opened positions through the log-only broker — synthetically,
in advance of the production cold-start path described in the next
section (ALP-620).

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify/verify_debug_e2e.py \
        --archive-root .archive/verify-debug-e2e \
        --fresh-start
```

What to watch for in the archive after a green run:

- **Analyst** — `decision/analyst/sdk_response.json` (or the equivalent
  per-agent diagnostics under `<archive>/<YYYY-MM-DD>/<id>/decision/analyst/`)
  should carry OPEN recommendations sized against the $100,000 cash
  budget. The analyst is the "new trade opportunities" agent; with no
  pre-existing positions to manage, it has the full book to fill.
- **Strategist** — becomes a no-op with zero position assessments. The
  `agent_response` still fires (the SDK call is unconditional) but the
  output content is empty/trivial; the assessor has nothing to assess.
- **PM** — dispatches the analyst's recommendations through the
  log-only broker; expect `agent_response.tool_calls` reflecting the
  OPEN command pipeline.
- **`synthetic_portfolio` check** — reports
  `positions=0, theses=0, cash_ledger.current_cash_usd=100000.0`. The
  positions / theses counts filter on `entry_timestamp IS NOT NULL` so
  PENDING skeletons written by PM dispatches (which the log-only broker
  never fills) are excluded — only the seeded fixture's rows count
  toward the assertion. `current_cash_usd` stays exact because PM
  dispatch reserves capital under `reserved_capital_usd`, not out of
  `current_cash_usd`.

Cost expectation is unchanged: same 9 SDK calls (3 domain researchers +
qualitative + adaptive + synthesizer + analyst + strategist + PM)
regardless of portfolio shape. The pipeline composition runs end-to-end
either way; only the seeded state differs.

## `--fresh-start` — production cold-start bootstrap (ALP-620)

Without `--debug-e2e`, the `--fresh-start` flag carries different
semantics: it is the one-time production bootstrap. On a freshly-reset
Alpaca paper account the local DB has no `cash_ledger` row and no
`drawdown_state` row; downstream consumers (the strategist's portfolio
bundle, the breach evaluator's HWM check, the synthesizer's exposure
summary) read empty results and produce malformed output. The flag
fetches Alpaca's reported cash and writes both singletons before the
first invocation runs.

**When to use.** Exactly once, before the daemon is started for the
first time against a fresh paper or live account. After this bootstrap,
the daemon takes over: the Projection is rebuilt each invocation by
folding the broker-event log onto the live broker snapshot (ADR-0001),
so `cash_ledger.current_cash_usd` stays aligned without a separate
auto-correct path.

**Prerequisites.**

1. Alpaca paper account has been reset to its starting cash and holds
   zero positions (confirm via the Alpaca dashboard).
2. The local DB at the configured `DATABASE_PATH` has no `cash_ledger`
   row. On a fresh install of AlphaMind this is the default state; a
   prior `--fresh-start` invocation will populate the row and the next
   one will hard-fail.
3. `.env` is sourced so the Alpaca paper credentials are available to
   the broker adapter (`ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET`).

**Invocation.**

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python -m alphamind.scheduler run \
        --fresh-start \
        --once market_open \
        --reason "first-run bootstrap"
```

The bootstrap commits the two singleton rows in their own transaction;
the same process then runs one `market_open` invocation through to command execution
so the operator immediately sees the pipeline complete against the
freshly-bootstrapped state. After it returns, start the daemon
normally (no `--fresh-start`).

**Hard-fail paths.** The flag refuses to run when:

- Alpaca reports any open positions or open orders. The error names the
  offending symbol(s). Reset the Alpaca account first — the genesis
  cutover procedure (`docs/runbooks/genesis-cutover.md`) is the supported
  path when starting from a flat new account.
- `cash_ledger` already has a row. The error includes the existing
  `current_cash_usd`. The Projection rebuilds from the broker-event log on
  every invocation (ADR-0001); a re-bootstrap is never the correct path
  once the singleton is populated.
- `drawdown_state` already has a row. The error includes the existing
  `equity_high_water_mark_usd`. Same recovery as above.

`FreshStartPreconditionError` exits the CLI with code 2 (distinct from
the generic scheduler-error code 1) and prints the message verbatim to
stderr — no traceback. Operators can match on the prefix
`--fresh-start:` to filter precondition failures from scheduler crashes.

**Mode restriction.** `--fresh-start` (production bootstrap) is blocked
against `--mode live` at argparse — only `--mode paper` (the default)
is accepted. Operators who genuinely need a live-mode bootstrap must
edit the guard; the runbook does not document that path.

**Recovery if the first invocation fails.** The bootstrap commits the
two singleton rows in their own transaction *before* the first
invocation runs. If the invocation itself fails (fill collection error, missing
collector data, transient external dependency), the singletons remain
committed and a retry of `--fresh-start` will hard-fail. Recovery
options, in order of preference:

1. **Re-run without `--fresh-start`.** The singletons are already
   populated correctly; the bootstrap step is no longer needed. Drop
   the flag:
   ```bash
   uv run python -m alphamind.scheduler run \
       --once market_open \
       --reason "retry after bootstrap"
   ```
2. **Wipe the singletons and re-bootstrap** (only if the persisted
   values are wrong, e.g., the Alpaca fetch returned a transient
   zero-cash mid-reset). Connect to the prod DB on the prod machine
   (the dev Mac SMB mount is read-only — see operator memory) and:
   ```sql
   DELETE FROM drawdown_state;
   DELETE FROM cash_ledger;
   ```
   Then re-run `--fresh-start --once market_open --reason ...`.

**Verification.** After the command returns, confirm against the DB:

```bash
uv run python -c "
import sqlite3, os
db = sqlite3.connect(os.environ['DATABASE_PATH'])
print(db.execute('SELECT current_cash_usd FROM cash_ledger').fetchone())
print(db.execute('SELECT equity_high_water_mark_usd FROM drawdown_state').fetchone())
"
```

Both values should match Alpaca's reported cash on the freshly-reset
account to the cent.

## Monitoring progress mid-run

**Arm the monitor below in a second shell right after launching
`verify_debug_e2e.py`** — the script waits up to a few seconds for the
new invocation's `progress.jsonl` to land, then tails it.
`verify_debug_e2e.py` is silent during the 5–15 minute run and only
prints PASS/FAIL lines after the subprocess exits. Without a monitor
an operator stares at silence for the full run and only learns about a
stalled SDK call, an empty agent response (`stop_reason: null`,
`output_tokens: 0`), or a mid-pipeline crash when the wrapper finally
exits — by which point the run is already spent. With the monitor,
every phase transition and every SDK call boundary lands in the
operator's shell in real time, and unexplained silence between events
is itself a signal.

This is the authoritative implementation. Copy it verbatim into a
second shell. Claude Code operators driving the verify via the harness
should pass the same shell command to the `Monitor` tool — each
emitted line becomes one notification.

```bash
# === AUTHORITATIVE e2e progress monitor — arm right after launching verify ===
# Waits for an invocation's progress.jsonl to appear under the date-bucketed
# archive layout (`<archive>/<YYYY-MM-DD>/inv-*/`), then polls it and emits
# every phase_start / phase_done / agent_request / agent_response event as
# it is written. WATERMARK_EPOCH gates against stale prior-run dirs.

set -u
ARCHIVE_ROOT="${ARCHIVE_ROOT:-.archive/verify-debug-e2e}"
PATTERN='"(phase_start|phase_done|agent_request|agent_response)"'
# Default watermark: 5 minutes ago. Override if launching the verify well
# before arming. Stale prior-run dirs (often hours old) are skipped because
# their mtime falls below the watermark.
WATERMARK_EPOCH="${WATERMARK_EPOCH:-$(($(date -u +%s) - 300))}"

echo "waiting for inv dir under $ARCHIVE_ROOT with mtime >= $WATERMARK_EPOCH ($(date -u -d "@$WATERMARK_EPOCH" +%Y-%m-%dT%H:%M:%SZ)) ..."
f=""
while true; do
  # New layout: <archive>/<YYYY-MM-DD>/inv-*/progress.jsonl
  # The intermediate '*' is the date bucket. The 'inv-*' glob filters out
  # the sibling '_pre_invocation' directory that holds the seed event.
  for d in $(ls -td "$ARCHIVE_ROOT"/*/inv-*/ 2>/dev/null); do
    mt=$(stat -c %Y "$d" 2>/dev/null || echo 0)
    if [ "$mt" -ge "$WATERMARK_EPOCH" ] && [ -f "$d/progress.jsonl" ]; then
      f="$d/progress.jsonl"
      break
    fi
  done
  if [ -n "$f" ]; then break; fi
  sleep 3
done
matched=$(grep -cE "$PATTERN" "$f" 2>/dev/null || echo 0)
echo "armed: $matched backlog matches in $f"

prev=0
while true; do
  cur=$(wc -l < "$f" 2>/dev/null | tr -d ' '); cur=${cur:-0}
  if [ "$cur" -gt "$prev" ]; then
    sed -n "$((prev+1)),${cur}p" "$f" | grep -E "$PATTERN" || true
    prev=$cur
  fi
  sleep 3
done
```

**Why a `WATERMARK_EPOCH` and not just `ls -td`.** A naive `ls -td`
picks the most recently modified `inv-*/progress.jsonl` *anywhere*
under the archive root. Two failure modes that bit operators before
the watermark was added:

1. **Stale prior runs locked the tail.** The wait loop matches the
   newest `inv-*/progress.jsonl` instantly — including yesterday's
   completed run. The monitor tails a fixed-size file forever, emitting
   silence. The watermark filters by mtime so only post-launch dirs
   qualify.
2. **Legacy flat-layout dirs.** PR #204 (ALP-689) moved the layout
   from `<archive>/invocations/inv-*/` to
   `<archive>/<YYYY-MM-DD>/inv-*/`. Old runs may still sit under the
   former `invocations/` subtree. The new glob (`*/inv-*/`) targets the
   date-bucketed layout; the watermark filter handles the rest.

To clear all stale archives before a run (the nuclear option, only
when you are certain no prior diagnostic data is needed):

```bash
rm -rf "$ARCHIVE_ROOT"/[0-9]*-[0-9]*-[0-9]* "$ARCHIVE_ROOT"/invocations
```

What to watch for as events arrive:

- **`phase_start` / `phase_done` pairs** — the 12 in-invocation phases
  (`fill_collection`, `snapshot_assembly`, `distillation`, `domain_researchers`,
  `qualitative`, `adaptive`, `synthesizer`, `analyst`, `strategist`,
  `pre_processor`, `pm`, `command_execution`) fire in dependency-respecting order;
  `domain_researchers`+`qualitative` and `analyst`+`strategist` overlap
  under their respective TaskGroups.
- **`agent_response` with `stop_reason: null` and `output_tokens: 0`** —
  the SDK call returned an empty stream without raising. The harness
  records `success: false` in `analysis/<agent>/metadata.json`; the
  pipeline typically aborts shortly after on a downstream consumer.
- **Long silence between events** — distillation is ~3–5 min (no SDK
  events, only `phase_start`/`phase_done`); each Sonnet researcher is
  ~5–6 min; `adaptive` is the most variable Sonnet phase and can stretch
  to ~8 min on cold-cache runs. In default-fixture mode the strategist
  is the typical long pole at decision time; under `--fresh-start` the
  strategist degenerates to a ~30–60 s no-op (empty theses) and `adaptive`
  becomes the dominant phase. Silence beyond ~10 min during an active
  phase usually means the SDK call has stalled — check the verify
  wrapper's captured stderr
  for `TimeoutFailure` from `_harness_core.invoke_sdk`.

Three things worth doing this way rather than the more obvious `tail -F`:

- **Polling beats `tail -F` on Windows Git Bash.** `tail -F | grep` has
  intermittent pipe-buffering quirks that swallow output even with
  `--line-buffered`. The polling loop has fewer moving parts (no FS
  notifications, no long-lived pipe) and just works.
- **The `armed:` line is a regex self-test.** If you see `armed: 0` when
  the file already has phase events, your pattern is wrong (or matches
  the wrong whitespace) — fix it before relying on the monitor instead
  of staring at silence for 10 minutes wondering whether the pipeline
  is dead. The original bug that motivated this section: a regex written
  as `"event":"phase_start"` (no space) never matched the JSONL events,
  which `json.dumps` formats as `"event": "phase_start"` (default
  `": "` separator).
- **Match the value, not the `"event":` prefix.** Patterns like
  `'"(phase_start|phase_done|agent_request|agent_response)"'` are robust
  to either formatter spacing.

To see every event including intra-phase progress, widen `PATTERN` to
`.` (drop the grep). The `_pre_invocation` directory carries only the
pre-invocation seed event and is filtered out by the `inv-*` glob above.

To drive the CLI directly without the wrapper (e.g., when iterating on
the underlying mode rather than the check semantics):

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    DATABASE_PATH=data/alphamind-debug-e2e.db \
        uv run python -m alphamind.scheduler run \
            --debug-e2e --once market_hours_rolling \
            --reason "debug iteration"
```

## Expected output

Eight PASS lines on a clean run, in order:

```
PASS: auth — all required env vars present (CLAUDE_CODE_OAUTH_TOKEN)
PASS: subprocess — debug-e2e subprocess exited 0
PASS: archive_directory — directory + resolved_config.json + progress.jsonl present at <archive>/<YYYY-MM-DD>/<id>
PASS: jsonl_ordering — 12/12 phases with paired start/done in dependency order; 9/9 SDK call pairs matched
PASS: synthetic_portfolio — positions=8, theses=8, cash_ledger.current_cash_usd=24440.0
PASS: no_alpaca — no alpaca indicators in captured stream
PASS: invocation_summary — staleness_flag=false, trigger_source='debug_e2e_cli', commands_submitted=N
PASS: tool_layer_health — N/M tool calls complete, unavailable=K, other=L[; degraded: <tool>=K/M unavailable, …]
=== DEBUG-E2E VERIFICATION === 8/8 checks passed
```

**Agent operators driving the verify via tool calls must include the
invocation ID in their final summary to the operator.** The ID is the
last path segment of the `PASS: archive_directory` line (e.g.,
`inv-20260526T210153Z-d34fb51d`). It lets the operator navigate
directly to `<archive-root>/<YYYY-MM-DD>/<invocation_id>/` to inspect
per-agent diagnostics, the `progress.jsonl` event stream, or
`verify_summary.txt` — without having to re-derive the path from the
wrapper's output or hunt through the archive root. Surface it whether
the run passed or failed; on FAIL, it is the entry point for triage.

A `=== TOOL LAYER HEALTH ===` block (ALP-703) lands below `=== DATA HEALTH ===` carrying per-tool counts so a "8/8 checks passed" verdict cannot hide a half-dark enrichment layer.

The archive directory carries:

```
<archive-root>/<YYYY-MM-DD>/<invocation_id>/
├── resolved_config.json
├── progress.jsonl                  # append-only event log (fsync per write)
├── data_calibration_state.json
├── verify_summary.txt              # PASS/FAIL lines + summary + DATA HEALTH
└── (per-agent diagnostic subdirs   — analysis/<agent>/, decision/<agent>/ —
    populated by the agent harnesses)
```

`scripts/verify/build_e2e_report.py` renders the archive into a single dark-mode
HTML page summarizing the run:

```bash
uv run python scripts/verify/build_e2e_report.py \
    --archive-root .archive/verify-debug-e2e
# auto-discovers --invocation-id when exactly one invocation directory exists
```

The report shows a 12-phase verdict ribbon, the SDK-call table with each
call's `agent_response` fields, the `resolved_config.json` payload, an
incomplete-phase failure section, and a collapsible `pipeline.log`
excerpt. Open the file in a browser — it carries its own CSS via the
sidecar at `scripts/verify/_e2e_report_assets/style.css`.

## Wall-clock + cost

A clean debug-e2e run wires one Sonnet pass through the analysis layer
(3 sector researchers + qualitative + adaptive + synthesizer = 6 Sonnet
calls; distillation is deterministic and emits no SDK call) and one
Opus pass through the four decision agents (analyst + strategist + PM
= 3 Opus calls; the proposal pre-processor is deterministic and emits
no SDK call). Approximate cost — the *Total input* column below is the
sum `input_tokens + cache_read_tokens + cache_write_tokens` across all
SDK calls in the layer, NOT the value of any single `agent_response`
field. The HTML report's "Input tokens" column is the bare
`input_tokens` (non-cached delta) only — see disambiguation below.

| Layer    | Model  | Total input | Output tokens |
|----------|--------|-------------|---------------|
| analysis | Sonnet | ~54K–70K    | ~7.6K–13.6K   |
| decision | Opus   | ~68K–102K   | ~57K–100K     |

Most AlphaMind prompts hit the cache, so in a healthy run the bulk of
the per-call input volume lands in `cache_read_tokens` and the bare
`input_tokens` field shows the non-cached delta only (single-to-low-
double-digit). The HTML report renders these as three separate columns
("Input tokens" / "Cache read" / "Cache write") so the operator sees
the split directly. If `input_tokens` looks "tiny" in `progress.jsonl`,
that is the SDK's cache-hit signature — check `cache_read_tokens` for
the real prompt volume (ALP-701).

Roughly 10–15% of the nominal weekly Sonnet cap and a smaller slice of
the Opus cap per `docs/design/cost-and-rate-limit-modeling.md`. Don't
re-run gratuitously.

Wall-clock varies sharply with cache state and seeded portfolio shape:

- **Default fixture (`SYNTHETIC_PORTFOLIO`), cache-warm:** ~5–15 min
  end-to-end. The strategist scenario (8 theses to assess) is the
  typical long pole.
- **`--fresh-start` (`FRESH_START_PORTFOLIO`), cache-cold:** ~25–35 min
  end-to-end. The strategist degenerates to a ~30–60 s no-op (empty
  theses), so it is no longer the long pole — `adaptive` and the three
  domain researchers dominate, and the first-fill `cache_write_tokens`
  on every SDK call adds several minutes of latency that the warm-cache
  numbers above don't include. A recent ALP-618 run measured 29m 11s
  total: distillation 4m 56s; parallel researchers 5–6.5 min (tech_semis
  6m 28s the longest, paired with `qualitative` 5m 38s); `adaptive` 7m
  58s; `synthesizer` 3m 10s; analyst/strategist parallel TaskGroup
  3m 27s (analyst dominates; strategist 37 s); pm 2m 57s; command_execution 29 ms.

See the archive's `progress.jsonl` for per-phase timings on a specific
invocation; budget your iteration cadence against the higher end of
whichever bucket applies.

## Resuming a failed run

The motivating shape: the analyst SDK call hits its latency budget 9
minutes into a debug-e2e run, the operator bumps
`decision.analyst.latency_budget_s` in `config/run_types/<trigger>.yaml`,
and re-invokes the verify wrapper with `--resume-from <inv-id>:<phase>`.
The new invocation re-runs the deterministic prefix (fill_collection +
snapshot_assembly + distillation — all cheap, all deterministic against
the synthetic portfolio fixture) and hydrates the upstream SDK-phase
outputs from the prior archive, then runs the named target phase and
everything downstream with the new budget. Saves ~10–12 minutes of
wall-clock and ~6 Sonnet calls per iteration.

`--resume-from` is the verify wrapper's pass-through to the scheduler
CLI's [ALP-693](https://linear.app/alphamind-jatassi/issue/ALP-693)
flag. The wrapper does no validation — the underlying CLI is the
source of truth on `<invocation-id>:<phase>` validity and exits 2 with
a named-cause stderr message on rejection. Mutually exclusive with
`--fresh-start` (a different portfolio fixture would invalidate the
prior archive's outputs — surfaced at argparse-time by the underlying
CLI).

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify/verify_debug_e2e.py \
        --archive-root .archive/verify-debug-e2e \
        --resume-from <inv-id>:<phase>
```

**Reading the resume target out of a failed run.** Open the source
invocation's `progress.jsonl` and look for the last `phase_done`
event; the next `phase_start` with no matching `phase_done` is the
phase the original run died on. That is your resume target. The 9
SDK-phase names recognized by `--resume-from` are: `tech_semis`,
`financials`, `energy`, `qualitative`, `adaptive`, `synthesizer`,
`analyst`, `strategist`, `pm`. The deterministic phases (`fill_collection`,
`snapshot_assembly`, `distillation`, `pre_processor`, `command_execution`) are
always re-run from scratch on resume and cannot be named as the
target — they are cheap and produce identical outputs against the
synthetic fixture, so the resume contract does not need to
special-case them.

**Deterministic-prefix check.** On every resume run the verify
wrapper adds one extra check (`check_deterministic_prefix`) that
hashes the source-archive vs new-archive distillation outputs
pairwise and FAILs on the first byte mismatch. A clean resume run
shows 9 PASS lines (the existing 8 plus this one) and the summary
reads `9/9 checks passed`; a FAIL surfaces as `8/9 checks passed`.
The check fires ONLY on resume — fresh debug-e2e invocations stay at
`8/8 checks passed` with no extra line emitted.

A FAIL of this check means the upstream-replayed SDK phases are now
operating against a different deterministic prefix than they
originally saw — a silent correctness bug. Do not trust the run's
downstream output.

```
PASS: deterministic_prefix — 5 distillation file(s) byte-identical (N bytes hashed) vs source archive
```

## Failure-mode triage

| `verify_debug_e2e.py` FAIL line                          | Likely cause | First fix to try |
|----------------------------------------------------------|--------------|------------------|
| `FAIL: auth — missing required env var(s)`               | `.env` not sourced | Re-run with `set -a && source .env && set +a && uv run …` |
| `FAIL: subprocess — debug-e2e subprocess exited N`       | CLI raised internally | Read `stderr` tail in the FAIL message; the orchestrator names the failing layer |
| `FAIL: subprocess — ... OperationalError: no such table: <X>` | Debug DB behind alembic head — a migration that added `<X>` never ran on the snapshot | Compare `alembic current` (under `DATABASE_PATH=data/alphamind-debug-e2e.db`) against `alembic heads`; if they differ, re-snapshot (`scripts/verify/snapshot_prod_for_debug_e2e.py --force`) or `alembic upgrade head` against the debug DB. See Prerequisites step 4 pre-flight |
| `FAIL: archive_directory — archive directory missing`    | CLI never wrote the archive | The orchestrator died before `insert_invocation_record` committed — check `~/AlphaMind/logs/pipeline.log` |
| `FAIL: archive_directory — resolved_config.json missing` | Fill collection prep crashed | Same as above — orchestrator died before the resolved-config writer fired |
| `FAIL: archive_directory — progress.jsonl missing`       | JSONL emitter never wired | Re-verify `DebugE2ESettings.emitter_factory` populated (story ALP-500) |
| `FAIL: jsonl_ordering — missing phase_start for <name>`  | A pipeline stage never emitted its `phase_start` | Cross-check `src/alphamind/pipeline/{analysis,decision}.py` against the 12-in-invocation-phase ladder |
| `FAIL: jsonl_ordering — phase_done for <name> precedes its own phase_start` | An emitter mis-emits | Same as above |
| `FAIL: jsonl_ordering — non-monotonic timestamp`         | Clock skew or out-of-order write | Inspect the JSONL line referenced in the message; the fsync per write should have prevented this |
| `FAIL: jsonl_ordering — missing agent_request/response pair(s)` | An SDK call site bypassed `_harness_core.invoke_sdk` | Cross-check the failing agent's harness against the 9 SDK pairs in parent § E |
| `FAIL: synthetic_portfolio — required table(s) missing`  | DB not migrated to head | Delete the debug DB; the CLI re-migrates on next run |
| `FAIL: synthetic_portfolio — positions count expected 8` | Seeder didn't run | Either the seeder raised (check stderr) or a downstream stage truncated `positions` |
| `FAIL: synthetic_portfolio — cash_ledger.current_cash_usd expected 24440.0` | Seeder bug or a downstream write mutated the row | Cross-check `wipe_and_seed` against `SYNTHETIC_PORTFOLIO.starting_cash_usd` |
| `FAIL: no_alpaca — captured stream carries alpaca indicator(s)` | `LogOnlyAccountStateQueries` not wired | Verify `context.debug_e2e is not None` reaches `fill_collection_inputs.gather_fill_collection_inputs`; the import-linter contract should have caught this at lint time |
| `FAIL: invocation_summary — staleness_flag expected false` | Fill collection saw a stale data source | Inspect the staleness logger output in the pipeline log; debug-e2e seeds fresh state so this is a real regression |
| `FAIL: invocation_summary — trigger_source expected 'debug_e2e_cli'` | CLI dispatch routed to `_run_once` instead of `_run_debug_e2e` | The `--debug-e2e` argparse branch in `__main__.py` regressed |
| `FAIL: invocation_summary — commands_submitted` | The orchestrator's typed return shape changed | Inspect `InvocationSummary` against `scheduler/orchestrator.py` |
| `FAIL: deterministic_prefix — distillation file <name> differs` (resume only) | Non-determinism regression in fill_collection / snapshot_assembly / distillation, OR the synthetic portfolio fixture changed between runs | First re-snapshot the debug DB (`scripts/verify/snapshot_prod_for_debug_e2e.py --force`) in case the source archive's distillation was computed against drifted upstream state; if the FAIL repeats, `git bisect` for the regression starting from the source archive's commit |

For deeper investigation: every agent harness writes its own diagnostic
archive under `<archive>/<YYYY-MM-DD>/<id>/analysis/<agent>/` (and
`decision/<agent>/`) — the same per-agent shape the retired verify
suite produced. Each carries the assembled input bundle, the prompt,
the full SDK response, and a `metadata.json` with token + wall-clock +
tool-call counts.

## Supporting standalone tools

Four standalone scripts survived ALP-502; they serve roles orthogonal
to the pipeline e2e gate and remain operator-runnable:

- `scripts/verify/verify_bootstrap.py` — bootstrap DB invariant check (18
  tables present, row counts within tolerance). Operator-monitoring
  surface. Operates against the snapshotted production DB, not the
  debug DB.
- `scripts/verify/verify_ongoing_collection.py` — collector freshness check
  (14 active collectors producing rows within 2× their configured
  cadence). Operator-monitoring surface. Same DB as above.
- `scripts/verify/verify_bootstrap_calibration_mix.py` — calibration-state
  distribution check on `distillation_ticker_baseline` /
  `distillation_pair_lag`. Operator-monitoring surface.
- `scripts/verify/verify_position_thesis_model.py` — offline pure-function
  verifier for the position-thesis-model type layer (ALP-122). Sub-10s
  runtime; no SDK calls, no DB reads. See
  `scripts/verify/RUNBOOK_position_thesis_model.md` for the dedicated runbook.

## References

- `scripts/verify/RUNBOOK_position_thesis_model.md` — failure-mode triage for
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
