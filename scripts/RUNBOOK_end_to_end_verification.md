# End-to-End Pipeline Verification Runbook

Operator workflow for verifying everything that's been built so far in
the AlphaMind pipeline (data layer → distillation layer → analysis
layer) by running the existing per-layer verification scripts in
dependency order. Targets a live operator, with a fresh agent session
co-piloting the run.

A green run proves today's actual distillation outputs flow correctly
through the domain researchers AND the qualitative researcher; their
actual outputs flow into the adaptive researcher; all five upstream
briefs (3 sectors + correlation/regime + qualitative + adaptive) flow
into the synthesizer. The cross-layer flow is enforced by a
stage-artifact cache (ALP-287): each phase 2-5 script writes its parsed
output to `<archive_root>/invocations/<invocation_id>/stage_artifacts/`
on success, and the next script reads its predecessor's outputs via
`--upstream-from`.

## TL;DR for the agent

You're going to run 11 verification scripts in 7 phases. Three rules:

1. **Stop on first FAIL.** Each phase depends on prior phases' state
   AND its predecessor's stage artifacts. Don't continue past a red
   signal — the next script will fail fast with a "run phase N first"
   message anyway.
2. **Track LLM cost.** The five Sonnet-driven analysis-layer scripts
   together consume roughly 60–65K input + 8–12K output tokens (~10–15%
   of weekly Sonnet cap); the analyst phase adds ~15K input + ~4K
   output Opus tokens across both scenarios. If the operator wants to
   skip to a specific layer, support that — but the downstream scripts
   will need a stage-artifacts directory from a prior run, or they'll
   fall back to fixtures (and the run is no longer end-to-end).
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

Pick the shell variables once at the top so the per-phase commands
stay short and every phase shares the same archive + invocation-id
(stage-artifact cache requires both to match):
```bash
DB_PATH="$(pwd)/data/alphamind-snapshot.db"  # macOS dev
# DB_PATH="%USERPROFILE%\AlphaMind\data\alphamind.db"  # Windows prod
ARCHIVE_ROOT=".archive/verify-pipeline-$(date +%Y%m%d)"
INVOCATION_ID="$(date -u +%Y%m%dT%H%M%SZ)-verify-pipeline"
STAGE_ARTIFACTS="$ARCHIVE_ROOT/invocations/$INVOCATION_ID/stage_artifacts"
```

`STAGE_ARTIFACTS` is the directory each phase 2-5 script writes to on
success and reads from via `--upstream-from`. Phase N requires phase
M's artifacts under that path; if the directory is empty or missing
the file the script needs, the script fails fast with a "run
scripts/verify_M.py first" message.

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
| 6 | Decision: analyst | analyst (normal + halt scenarios) | Yes (Opus) | ~30–90s |
| 7 | Decision: strategist | strategist (normal + defensive_posture + emergency scenarios) | Yes (Opus) | ~10–15 min |

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
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID"
```

Verifies: `run_external_distillation()` produces `DistillationOutputs`
with 3 sector blocks, the correlation/regime brief carries `[CR-N]`
references, the regime label is one of the 4 valid strings, all 5
state tables have rows within a 5-minute freshness window, and the
invocation archive has 5 files (`prompt.md`, `user_message.md`,
`response.md`, `errors.json`, `metadata.json`).

On success, writes `distillation_outputs.json`,
`correlation_regime_brief.json`, and `universal_regime_label.json` to
`$STAGE_ARTIFACTS` for the downstream phases.

`distillation_contract_history` is now an active probe (ALP-274 wired
the prediction-market scope through to phase-1 ingestion). On a fresh
DB whose `prediction_market_snapshots` table is still cold, the probe
will fail freshness — re-snapshot or wait for the contract collector
to run before re-verifying.

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

`DEFERRED` rows on the high-freq bands are expected on a freshly-migrated
DB and exit 0 (ALP-273): the cold-start signature is all-bootstrap +
every `n_observations < window_days` + full universe coverage. `OK`,
`DEFERRED`, and `EMPTY` all pass; only `OUT` fails.

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
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID" \
    --upstream-from "$STAGE_ARTIFACTS"
```

Verifies: 3 populated `SectorBrief` results (tech_semis, financials,
energy), each brief re-validates through its parser, 4 diagnostic
files per sector, wall-clock < latency_budget, tokens within
(context + output) budget, retry counts tracked.

`--upstream-from` makes the script load phase-2's
`distillation_outputs.json` instead of re-running the distillation
orchestrator (which would double-spend the phase-2 SDK tokens). On
success, the 3-tuple of sector briefs is written to
`$STAGE_ARTIFACTS/sector_briefs.json` for phases 4 and 5.

Post-ALP-288 the harness runs in `output_format = {"type": "json_schema",
...}` mode: it reads `ResultMessage.structured_output` and the
diagnostic `response_initial.md` / `response_retry.md` files contain
JSON-rendered payloads (text-block prefix prepended only when the model
narrated alongside, which is rare in JSON mode). The `tool_calls_used`
counter in `metadata.json` filters to `mcp__alphamind_<server>__*` so
the SDK's `ToolSearch` / `StructuredOutput` pseudo-events do not inflate
the budget — the recorded count is the agent's real research-tool spend
(domain researchers run tool-less, so this should be 0).

The runbook for this script is `scripts/RUNBOOK_domain_researchers.md`
— read it for the failure-mode triage table if anything trips.

```bash
uv run python scripts/verify_domain_researcher_failure_modes.py \
    --archive-root "$ARCHIVE_ROOT"
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

Two more analysis-layer agents against real Sonnet. Both consume
phase-2 stage artifacts via `--upstream-from`; the adaptive researcher
additionally consumes the phase-3 sector briefs and the phase-4
qualitative brief, so run qualitative before adaptive.

```bash
uv run python scripts/verify_qualitative_researcher.py \
    --db-path "$DB_PATH" \
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID" \
    --upstream-from "$STAGE_ARTIFACTS" \
    --as-of 2026-05-03T14:30:00Z
```

Verifies: structured `QualitativeBrief` output, 5-file archive,
wall-clock < 180s, output_tokens < 1000, tool_calls < 15.

`--upstream-from` makes the script load phase-2's
`universal_regime_label.json` instead of reading from
`distillation_regime_state` — more reliable than the DB-state
fallback, which can stub on a cold DB. On success, the qualitative
brief is written to `$STAGE_ARTIFACTS/qualitative_brief.json` for
phases 4 (adaptive) and 5 (synthesizer).

Post-ALP-288 the harness runs in JSON-Schema output mode (see Phase 3
note for details). `tool_calls_used` filters to
`mcp__alphamind_qualitative__*` so the budget reflects real
`news_search` / `prediction_markets` / `earnings_commentary` calls
exclusive of SDK pseudo-events; the diagnostic `response_*.md` files
contain JSON-rendered payloads. Output-token usage may run modestly
higher than the legacy text format (JSON syntax overhead), but the
spike's cost-regression estimate is <10% per call, so the existing
`output_tokens < 1000` threshold should hold.

Runbook: `scripts/RUNBOOK_qualitative_researcher.md`.

```bash
uv run python scripts/verify_adaptive_researcher.py \
    --db-path "$DB_PATH" \
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID" \
    --upstream-from "$STAGE_ARTIFACTS" \
    --as-of 2026-05-03T14:30:00Z
```

Verifies: `AdaptiveBrief` re-validates with Layer-3 reference
resolution, 4 input-bundle section markers present, tool names in
allowlist, wall-clock < 300s, output_tokens < 1500, tool_calls < 25.

`--upstream-from` makes the script load the upstream-brief tuple
(sector_briefs, qualitative_brief, correlation_regime_brief,
distillation_outputs, universal_regime_label) from phase 2 and 3's
artifacts — proving today's actual upstream artifacts flow through
the adaptive researcher. On success, the adaptive brief is written
to `$STAGE_ARTIFACTS/adaptive_brief.json` for phase 5.

Post-ALP-288 the harness runs in JSON-Schema output mode with
`_tighten_conditional_schema` applied to the InvestigationThread
SIGNAL/NOISE/INCONCLUSIVE invariant — the API rejects payloads that
emit `null` for a branch's required-conditional fields rather than
deferring the failure to the Pydantic validator. `tool_calls_used`
filters to `mcp__alphamind_adaptive__*` so the cumulative-25 cap is
measured against real research-tool spend exclusive of pseudo-events
(spike scenario 2 reported 4 tool calls when only 2 were real); the
diagnostic `response_*.md` files contain JSON-rendered payloads. The
parser still normalizes the wire-shape habits Sonnet retains under
load — `tools_used` parens commentary stripped, `strengthens` /
`weakens` bracket+free-text refs extracted — so the validator sees
clean inputs.

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
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID" \
    --upstream-from "$STAGE_ARTIFACTS"
```

Verifies: non-empty prose response, every cited reference ID resolves
in the per-invocation retrieval store (invented references → WARN
verdict, exit 0), `stop_reason` in `{end_turn, max_tokens}`. Three
portfolio-state tool calls plus the final prose generation.

`--upstream-from` makes the script load the six-way upstream-brief
tuple (3 sector briefs + correlation/regime + qualitative + adaptive
+ universal regime label) from phase 2-4's artifacts — proving
today's actual upstream artifacts flow through the synthesizer. On
success, the populated retrieval store is written to
`$STAGE_ARTIFACTS/retrieval_store.json` for any decision-layer
verification work that picks up downstream.

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

## Phase 6 — Analyst (decision layer)

The first decision-layer agent. The analyst consumes the synthesizer's
recorded prose (from phase 5's archive — fixture-based per ALP-115
parent-issue decision C, not live composition) and emits structured
trade recommendations or a watchlist. Two scenarios per run:
**normal-mode** (full guardrail header, recommendations expected) and
**halt-mode** (watchlist header, no `validate_guardrail` calls).

```bash
uv run python scripts/verify_analyst.py \
    --archive-root "$ARCHIVE_ROOT" \
    --synthesizer-invocation-id "$INVOCATION_ID" \
    --save-fixtures
```

Verifies, per scenario: schema-valid `AnalystOutput`, Layer-2/3
cross-field invariants hold (no `unknown_reference`, no leg-id
mismatches, no off-band sizing), normal mode emits ≥1
`validate_guardrail` tool call when recommendations are non-empty
(zero is allowed for an explicit empty-recommendations response), halt
mode records zero tool calls. Wall-clock ≤ analyst's
`latency_budget_seconds`. Exits 0 on PASS or WARN per scenario; exits
1 on FAIL on either scenario.

`--synthesizer-invocation-id` points at the phase 5 invocation; the
script reads `analysis/synthesizer/response.md` and
`stage_artifacts/retrieval_store.json` from that archive. Each
analyst-side scenario writes its own diagnostic archive to
`<archive-root>/invocations/<analyst-inv-id>/decision/analyst/`.

`--save-fixtures` writes the parsed `AnalystOutput` JSON for both
scenarios to `tests/fixtures/decision/analyst/{normal,halt}.json`.
These are the canonical inputs the downstream feature trees'
verifiers (strategist, proposal pre-processor, PM) will consume — see
`tests/fixtures/decision/analyst/README.md` for the handoff contract.

Runbook: `scripts/RUNBOOK_analyst.md`.

**On WARN:** the analyst produced structured output that parsed
cleanly but flagged a contract violation — most commonly an
`unknown_reference` (cited a reference ID not present in the
synthesizer's retrieval store) or a tool-call discipline mismatch
(zero `validate_guardrail` calls in normal mode with non-empty
recommendations, or any tool call in halt mode). The fixture is still
written; the operator decides whether to act on the warning.

**On FAIL:** read `scripts/RUNBOOK_analyst.md` § Failure-mode triage.
The diagnostic archive carries the prompt, the assembled input bundle
(header + brief), the full structured-output response, and the error
trail.

## Phase 7 — Strategist (decision layer)

The second decision-layer agent. The strategist consumes the same
synthesizer-prose archive the analyst does (from phase 5 — fixture-based
per ALP-116 parent-issue decision C, not live composition); phase 7 can
run in parallel with phase 6. The strategist runs three scenarios per
verify call: **normal mode** (full guardrail header, multi-position
assessments), **defensive_posture mode** (halt header active, no
`add` actions allowed), and **emergency invocation** (regime-transition
breach surfaced; runs in normal mode but with the EMERGENCY INVOCATION
line in the header).

```bash
uv run python scripts/verify_strategist.py \
    --archive-root "$ARCHIVE_ROOT" \
    --synthesizer-invocation-id "$INVOCATION_ID" \
    --save-fixtures
```

Verifies, per scenario: schema-valid `StrategistOutput`, Layer-2/3
cross-field invariants hold (no `unknown_reference`, no
`assessment_id` collisions, no orphan `linked_position_assessment_id`,
no `remedy_flag` ↔ `addressed_breaches` pairing mismatches), and
defensive_posture-mode forbids `recommended_action=add`. Wall-clock ≤
strategist's `latency_budget_seconds` × 3 scenarios. Exits 0 on PASS or
WARN per scenario; exits 1 on FAIL on any scenario.

`--synthesizer-invocation-id` points at the phase 5 invocation; the
script reads `analysis/synthesizer/response.md` and
`stage_artifacts/retrieval_store.json` from that archive (same files
the analyst reads). Each strategist-side scenario writes its own
diagnostic archive to
`<archive-root>/invocations/<strategist-inv-id>/decision/strategist/`.

`--save-fixtures` writes the parsed `StrategistOutput` JSON for all
three scenarios to
`tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json`.
These are the canonical inputs the downstream feature trees' verifiers
(proposal pre-processor, PM) will consume — see
`tests/fixtures/decision/strategist/README.md` for the handoff
contract.

Runbook: `scripts/RUNBOOK_strategist.md`.

**On WARN:** the strategist produced structured output that parsed
cleanly but flagged a contract violation — most commonly an
`unknown_reference` or a defensive-posture `add`-action mismatch. The
fixture is still written; the operator decides whether to act on the
warning.

**On FAIL:** read `scripts/RUNBOOK_strategist.md` § Failure-mode
triage. The diagnostic archive carries the prompt, the assembled input
bundle (header + tool reminder + portfolio state + brief), the full
structured-output response, and the error trail.

## Phase 8 — Proposal pre-processor

**Purpose.** Verify the deterministic pre-processor that bundles analyst +
strategist outputs for the PM, including combined-set impact projection,
conviction histogram, book-health summary, and same-underlying conflict
detection.

**Prerequisites.** Phase 6 (analyst) must have completed — its fixtures at
`tests/fixtures/decision/analyst/{normal,halt}.json` are the primary inputs
to this phase. Phase 7 (strategist) fixtures are not required as inputs;
strategist outputs are constructed in-code.

**Run.**

```bash
uv run python scripts/verify_proposal_pre_processor.py
```

**Outputs.** Four bundle JSON files at
`tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json`,
consumed by the PM phase.

**Cost.** Zero. No SDK calls. Sub-second total runtime.

**See:** [`RUNBOOK_proposal_pre_processor.md`](RUNBOOK_proposal_pre_processor.md)

## When complete

Report a one-line summary to the operator:

```
End-to-end verification: <PASS|FAIL|WARN-only>
- Phase 1 (data): PASS
- Phase 2 (distillation): PASS
- Phase 3 (domain researchers): PASS
- Phase 4 (qualitative + adaptive): PASS
- Phase 5 (synthesizer): WARN (2 invented references)
- Phase 6 (analyst): normal=PASS, halt=PASS
- Phase 7 (strategist): normal=PASS, defensive_posture=PASS, emergency=PASS
- Phase 8 (proposal pre-processor): normal=PASS, halt=PASS, emergency=PASS, normal_with_breach=PASS
Total LLM cost: ~Xk Sonnet input + Yk Sonnet output, ~Zk Opus input + Wk Opus output
Archives under .archive/verify-pipeline-YYYYMMDD/
Fixtures at tests/fixtures/decision/{analyst,strategist,proposal_pre_processor}/*.json
```

If WARN-only or any FAIL, attach the per-script verdict block(s) so
the operator can act.

## Cost summary

| Script | Model | Input tokens | Output tokens |
|--------|-------|--------------|---------------|
| verify_distillation | Sonnet | 12K–16K | 2K–4K |
| verify_domain_researchers | Sonnet | 18K–24K | 3K–6K |
| verify_qualitative_researcher | Sonnet | 6K–8K | 0.4K–0.6K |
| verify_adaptive_researcher | Sonnet | 8K–10K | 0.7K–1K |
| verify_synthesizer | Sonnet | 10K–12K | 1.5K–2K |
| verify_analyst (×2 scenarios) | Opus | 12K–18K | 3K–5K |
| verify_strategist (×3 scenarios) | Opus | 24K–36K | 30K–55K |
| **Sonnet total** | | **~54K–70K** | **~7.6K–13.6K** |
| **Opus total** | | **~36K–54K** | **~33K–60K** |

Roughly 10–15% of the nominal weekly Sonnet cap and a smaller slice of
the Opus cap per `docs/design/cost-and-rate-limit-modeling.md`. Don't
re-run gratuitously.

Post-ALP-288, output tokens for the three analysis-layer scripts
(domain / qualitative / adaptive researchers) may run a touch higher
than the table above — JSON syntax adds 15–25% over the legacy text
format on the brief itself, though the spike's measured cost regression
was <10% per call because extended thinking dominates the output budget.
Re-baseline these ranges after the first few clean post-migration runs
if the existing thresholds become misleading.

## Known gaps (so the agent doesn't claim more than the run proved)

1. **Regime-transition verification is lookback-only.** The script
   reads recent `distillation_regime_state` rows and checks
   invariants; it does not exercise a fresh transition end-to-end
   (would require a live regime shift in the market data).

2. **No ongoing PM / execution-layer verification.** Anything
   downstream of the strategist (proposal pre-processor, PM, breach
   behavior, execution) is outside this runbook's scope — those
   layers are either in progress or not yet built. Their verifiers
   will consume the analyst fixtures
   `tests/fixtures/decision/analyst/{normal,halt}.json` and the
   strategist fixtures
   `tests/fixtures/decision/strategist/{normal,defensive_posture,emergency}.json`
   once landed. Check `docs/project-tracker.md` for current status.

3. **No "run all" wrapper.** This runbook is the closest thing.
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
- `scripts/RUNBOOK_analyst.md` — phase 6 failure triage.
- `scripts/RUNBOOK_strategist.md` — phase 7 failure triage.
- `tests/fixtures/decision/analyst/README.md` — analyst-fixture
  provenance + downstream consumer contract.
- `tests/fixtures/decision/strategist/README.md` — strategist-fixture
  provenance + downstream consumer contract.
- `docs/project-tracker.md` — current build status.
- `src/alphamind/pipeline/analysis.py` — `run_analysis_pipeline` (ALP-276),
  the composition runner the runtime pipeline calls. The per-script
  verifications mirror its stage threading via the stage-artifact cache
  (ALP-287) instead of calling the runner directly so each phase stays
  independently iterable.
- `src/alphamind/scripts/_artifact_io.py` — the typed dump/load helpers
  the verification scripts use to thread stage artifacts (ALP-287).
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
