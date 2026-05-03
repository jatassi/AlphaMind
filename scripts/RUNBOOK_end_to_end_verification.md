# End-to-End Pipeline Verification Runbook

Operator workflow for verifying everything that's been built so far in
the AlphaMind pipeline (data layer → distillation layer → analysis
layer) by running the existing per-layer verification scripts in
dependency order. Targets a live operator, with a fresh agent session
co-piloting the run.

## Important: this is NOT a single live composed pipeline run

The analysis layer's downstream agents — **adaptive researcher** and
**synthesizer** — currently consume *hand-constructed fixtures* in
their verification scripts, not the live outputs of the upstream
distillation/qualitative/domain-researcher chain. The live pipeline
composition wiring is deferred work (see `docs/project-tracker.md` §
Substantial → "Analysis-layer pipeline composition wiring").

What this runbook gives you:

- ✅ Each layer independently verified end-to-end against real database
  state and the real Claude Agent SDK.
- ✅ Confidence that the *pieces* talk to their substrates (DB, SDK,
  config) correctly.
- ❌ Not: a guarantee that the full distillation→synthesizer flow
  composes correctly when wired together. That story has not landed
  yet — flag any cross-layer schema drift to the operator.

## TL;DR for the agent

You're going to run 9 verification scripts in 5 phases. Three rules:

1. **Stop on first FAIL.** Each phase depends on prior phases' state.
   Don't continue past a red signal.
2. **Track Sonnet cost.** The five live-SDK scripts together consume
   roughly 60–65K input + 8–12K output tokens (~10–15% of weekly cap).
   If the operator wants to skip to a specific layer, support that.
3. **Reference the per-layer runbook for failure triage.** Each
   live-SDK layer has its own runbook with a failure-mode table. Don't
   reinvent triage — read those.

## Prerequisites

Before running anything, confirm:

1. **Working directory** — the AlphaMind repo root.
2. **Local snapshot of the production DB** — on macOS, direct reads
   against the SMB-mounted production DB fail with `sqlite3.Operational
   Error: database is locked` while the production collector is
   writing (SMB + WAL + remote-writer don't cooperate). Snapshot the
   WAL trio locally before any phase:
   ```bash
   mkdir -p data
   cp /Volumes/Users/jacks/AlphaMind/data/alphamind.db     data/alphamind-snapshot.db
   cp /Volumes/Users/jacks/AlphaMind/data/alphamind.db-wal data/alphamind-snapshot.db-wal
   cp /Volumes/Users/jacks/AlphaMind/data/alphamind.db-shm data/alphamind-snapshot.db-shm
   ```
   ~2.5 GB; takes ~45–60s over SMB. The snapshot is gitignored
   (`data/` in `.gitignore`). Re-snapshot between phases only if you
   need a fresher view of live state — the snapshot is otherwise good
   for the whole run. On Windows production, point scripts at
   `%USERPROFILE%\AlphaMind\data\alphamind.db` directly (no snapshot
   needed — same machine as the writer).
3. **`CLAUDE_CODE_OAUTH_TOKEN` exported** — required for phases 2, 3,
   4, 5 (any script that talks to the SDK). Generate with
   `claude setup-token` if missing. If your token lives in `.env`,
   source it inline before each SDK-using phase:
   `set -a && source .env && set +a && uv run python scripts/...`
4. **`uv sync` completed** — `uv run` is the entry point for every
   script.
5. **Archive root chosen** — pick a directory like
   `.archive/verify-pipeline-$(date +%Y%m%d)` for diagnostic outputs.
   Reuse the same root across phases so all archives land together.

Pick a `DB_PATH` shell variable so the per-phase commands stay
short:
```bash
DB_PATH="$(pwd)/data/alphamind-snapshot.db"  # macOS dev
# DB_PATH="%USERPROFILE%\AlphaMind\data\alphamind.db"  # Windows prod
```

Pick an `--as-of` timestamp to use across phases for consistency. Use
ISO-8601 UTC like `2026-05-03T14:30:00Z`. Defaulting to "now" is fine
for ad-hoc runs but may produce different results between scripts as
they read time-dependent DB state.

## Phase map

| Phase | Layer | Scripts | SDK? | Wall-clock |
|-------|-------|---------|------|------------|
| 1 | Data | bootstrap, ongoing_collection | No | ~5s |
| 2 | Distillation | distillation, regime_transition, calibration_mix | Yes (1 of 3) | ~30–60s |
| 3 | Analysis: domain researchers | domain_researchers, domain_researcher_failure_modes | Yes (1 of 2) | ~30–60s |
| 4 | Analysis: qualitative + adaptive | qualitative_researcher, adaptive_researcher | Yes | ~45–90s |
| 5 | Analysis: synthesizer | synthesizer | Yes | ~10–30s |

## Phase 1 — Data layer

Pure schema and freshness checks. No SDK calls, no LLM cost. Should
run in seconds. If either fails, the data layer is broken or the DB
the agent is pointed at is empty/wrong — stop and surface to operator.

```bash
uv run python scripts/verify_bootstrap.py \
    --db-path "$DB_PATH"
```

Verifies: 18 tables exist with row counts in tolerance (525K OHLCV,
150 reference rows across 4 tables, treasury/macro/event/options/news/
prediction tables populated).

```bash
uv run python scripts/verify_ongoing_collection.py \
    --db-path "$DB_PATH"
```

Verifies: 14 active collectors have produced rows within 2x their
configured cadence window. Skips market-hours-only collectors when
NYSE is closed.

**On failure:** the data layer's bootstrap or ongoing collection has a
gap. Check `/Volumes/Users/jacks/AlphaMind/logs/collector.{out,err}.log`
on the dev machine for collector errors. Don't proceed to phase 2 —
distillation reads from these tables.

## Phase 2 — Distillation layer

Runs the live distillation orchestrator (one Sonnet call) plus two
DB-only state-machine checks.

```bash
uv run python scripts/verify_distillation.py \
    --db-path "$DB_PATH" \
    --archive-root .archive/verify-pipeline-YYYYMMDD
```

Verifies: `run_external_distillation()` produces `DistillationOutputs`
with 3 sector blocks, the correlation/regime brief carries `[CR-N]`
references, the regime label is one of the 4 valid strings, all 5
state tables have rows within a 5-minute freshness window, and the
invocation archive has 5 files (`prompt.md`, `user_message.md`,
`response.md`, `errors.json`, `metadata.json`).

```bash
uv run python scripts/verify_regime_transition.py \
    --db-path "$DB_PATH" \
    --lookback-days 7
```

Verifies state-machine invariants on `distillation_regime_state` over
the last 7 days: prior_label recorded, confirmed rows reached
threshold, early-strong/weak indicator counts correct.

```bash
uv run python scripts/verify_bootstrap_calibration_mix.py \
    --db-path "$DB_PATH"
```

Verifies the calibration state distribution in
`distillation_ticker_baseline` and `distillation_pair_lag`: high-freq
indicators (volume, ATR, spread) ≥80% calibrated; event-driven
indicators (sentiment, lead-lag) ≤70%. These are broad bands, not
SLAs — slight drift is expected.

**On failure:** if `verify_distillation` fails, the distillation
orchestrator itself is broken or the data layer's snapshots aren't
producing valid input. If the regime/calibration scripts fail, the
state machines have drifted — check the lookback window's row history
in the DB. Surface the specifics to the operator before proceeding.

## Phase 3 — Domain researchers

Three sector researchers running in parallel against real Sonnet.

```bash
uv run python scripts/verify_domain_researchers.py \
    --db-path "$DB_PATH" \
    --archive-root .archive/verify-pipeline-YYYYMMDD
```

Verifies: 3 populated `SectorBrief` results (tech_semis, financials,
energy), each brief re-validates through its parser, 4 diagnostic
files per sector, wall-clock < latency_budget, tokens within
(context + output) budget, retry counts tracked.

The runbook for this script is `scripts/RUNBOOK_domain_researchers.md`
— read it for the failure-mode triage table if anything trips.

```bash
uv run python scripts/verify_domain_researcher_failure_modes.py \
    --archive-root .archive/verify-pipeline-YYYYMMDD
```

No SDK calls. Runs three injection scenarios against a stubbed
harness: parse-failure-then-success, two-failures-into-malformed-output,
validation-failure-then-success. Verifies the diagnostic archive
layout in each scenario.

**On failure:** if `verify_domain_researchers` fails on a single
sector, the issue is sector-specific (prompt drift, data shape change
in that sector's tables). If all three fail, the harness or the
shared `_shared.py` types regressed. Read
`scripts/RUNBOOK_domain_researchers.md` § Failure-mode triage.

## Phase 4 — Qualitative + adaptive researchers

Two more analysis-layer agents against real Sonnet. They share state
prerequisites (regime label, ticker pool).

```bash
uv run python scripts/verify_qualitative_researcher.py \
    --db-path "$DB_PATH" \
    --archive-root .archive/verify-pipeline-YYYYMMDD \
    --as-of 2026-05-03T14:30:00Z
```

Verifies: structured `QualitativeBrief` output, 5-file archive,
wall-clock < 180s, output_tokens < 1000, tool_calls < 15. If the DB
has no regime state for the as-of timestamp, the script falls back
to a synthetic regime stub and reports it.

Runbook: `scripts/RUNBOOK_qualitative_researcher.md`.

```bash
uv run python scripts/verify_adaptive_researcher.py \
    --db-path "$DB_PATH" \
    --archive-root .archive/verify-pipeline-YYYYMMDD \
    --as-of 2026-05-03T14:30:00Z
```

Verifies: `AdaptiveBrief` re-validates with Layer-3 reference
resolution, 4 input-bundle section markers present, tool names in
allowlist, wall-clock < 300s, output_tokens < 1500, tool_calls < 25.

⚠️ **Heads up:** this script consumes a hand-constructed
`SectorBrief` + `QualitativeBrief` + `CorrelationRegimeBrief` triple
as upstream input. It does **not** consume the live outputs from
phases 2 and 3. So a PASS here means "the adaptive researcher works
against well-formed inputs", not "the adaptive researcher works
against today's live distillation/qualitative outputs". This is a
known gap (deferred to the analysis-layer composition wiring story).

Runbook: `scripts/RUNBOOK_adaptive_researcher.md`.

**On failure:** read the per-script runbook's failure table. If the
qualitative researcher fails specifically because of the regime-state
fallback, that's a phase-2 issue masquerading as phase-4 — go back
and verify distillation populated the regime state.

## Phase 5 — Synthesizer

The synthesizer integration story (the agent that consumes all six
upstream briefs).

```bash
uv run python scripts/verify_synthesizer.py \
    --as-of 2026-05-03T14:30:00Z \
    --archive-root .archive/verify-pipeline-YYYYMMDD
```

Verifies: non-empty prose response, every cited reference ID resolves
in the per-invocation retrieval store (invented references → WARN
verdict, exit 0), `stop_reason` in `{end_turn, max_tokens}`. Three
portfolio-state tool calls plus the final prose generation.

⚠️ **Same caveat as adaptive researcher:** the synthesizer's six-way
brief tuple (3 sector briefs, correlation/regime, qualitative,
adaptive) is **hand-constructed**, not pulled from phases 2–4's live
outputs. PASS here means "the synthesizer pipeline works against a
canonical fixture set"; it does not validate that today's actual
upstream outputs flow correctly through the synthesizer.

Runbook: `scripts/RUNBOOK_synthesizer.md`.

**On WARN:** the synthesizer produced usable prose but cited a
reference ID not present in the retrieval store. This is a
prompt-tightening signal. Compare `prompts/analysis/synthesizer.md`
against the prior known-good run; if the prompt is unchanged, the LLM
is hallucinating reference IDs and the prompt's
reference-mechanism section likely needs tightening. WARN exits 0 —
the operator decides whether to act on it.

**On FAIL:** read `scripts/RUNBOOK_synthesizer.md` § Failure-mode
triage. The diagnostic archive (under the `--archive-root`) has the
prompt, user message, and full response for offline analysis.

## When complete

Report a one-line summary to the operator:

```
End-to-end verification: <PASS|FAIL|WARN-only>
- Phase 1 (data): PASS
- Phase 2 (distillation): PASS
- Phase 3 (domain researchers): PASS
- Phase 4 (qualitative + adaptive): PASS
- Phase 5 (synthesizer): WARN (2 invented references)
Total Sonnet cost: ~Xk input + Yk output
Archives under .archive/verify-pipeline-YYYYMMDD/
```

If WARN-only or any FAIL, attach the per-script verdict block(s) so
the operator can act.

## Cost summary

| Script | Input tokens | Output tokens |
|--------|--------------|---------------|
| verify_distillation | 12K–16K | 2K–4K |
| verify_domain_researchers | 18K–24K | 3K–6K |
| verify_qualitative_researcher | 6K–8K | 0.4K–0.6K |
| verify_adaptive_researcher | 8K–10K | 0.7K–1K |
| verify_synthesizer | 10K–12K | 1.5K–2K |
| **Total** | **~54K–70K** | **~7.6K–13.6K** |

Roughly 10–15% of the nominal weekly Sonnet cap per
`docs/design/cost-and-rate-limit-modeling.md`. Don't re-run
gratuitously.

## Known gaps (so the agent doesn't claim more than the run proved)

1. **No live cross-layer composition.** The synthesizer and adaptive
   researcher use hand-crafted upstream-brief fixtures. A green run
   proves each agent works on canonical inputs; it does not prove
   that phase 2's actual live outputs flow correctly into phase 4
   and phase 5. Surface this distinction explicitly when reporting.

2. **Regime-transition verification is lookback-only.** The script
   reads recent `distillation_regime_state` rows and checks
   invariants; it does not exercise a fresh transition end-to-end
   (would require a live regime shift in the market data).

3. **No ongoing-execution-layer verification.** Anything downstream
   of the synthesizer (analyst, strategist, PM, breach behavior,
   execution) is outside this runbook's scope — those layers are
   either in progress or not yet built. Check
   `docs/project-tracker.md` for current status.

4. **No "run all" wrapper.** This runbook is the closest thing.
   Sequence is manual; if any phase changes (new script, removed
   script, args drift), this runbook needs updating.

## When a script's CLI doesn't match this runbook

Run `--help` on the script:

```bash
uv run python scripts/verify_<name>.py --help
```

The script docstrings are authoritative; this runbook is a curated
sequence wrapper. If you find drift, flag it to the operator and
update the runbook in the same change.

## References

- `scripts/RUNBOOK_domain_researchers.md` — phase 3 failure triage.
- `scripts/RUNBOOK_qualitative_researcher.md` — phase 4 failure triage.
- `scripts/RUNBOOK_adaptive_researcher.md` — phase 4 failure triage.
- `scripts/RUNBOOK_synthesizer.md` — phase 5 failure triage.
- `docs/project-tracker.md` — current build status; see § Substantial
  for the deferred "Analysis-layer pipeline composition wiring" story.
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
