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

You're going to run 17 verification scripts in 12 phases plus a final
HTML-report render. Three rules:

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
5. **No extra env vars** beyond `CLAUDE_CODE_OAUTH_TOKEN` (item 3) are
   required on either platform. The verify scripts call
   `configure_utf8_stdio()` at the top of `main()` so Windows runs
   accept UTF-8 output (Greek letters, em-dashes, smart quotes in
   LLM-produced briefs) without setting `PYTHONIOENCODING=utf-8`
   manually. The legacy workaround — `$env:PYTHONIOENCODING = "utf-8"`
   in PowerShell — is still safe but no longer needed.

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
| 0 | Types | position_thesis_model | No | <10s |
| 1 | Data | bootstrap, ongoing_collection | No | ~5s |
| 1b | Execution: state persistence | state_persistence | No | <5s |
| 1c | Execution: OMS commands | oms_commands | No | <60s |
| 1d | Execution: broker adapter | broker_adapter | No (live broker) | <2 min |
| 1e | Execution: corporate actions | corporate_actions | No | <2s |
| 1f | Execution: Reg T margin attribution | regt_margin_attribution | No | <2s |
| 1g | Execution: continuous monitor | continuous_monitor | No | <30s |
| 2 | Distillation | distillation, regime_transition, calibration_mix | Yes (1 of 3) | ~30–60s |
| 3 | Analysis: domain researchers | domain_researchers, domain_researcher_failure_modes | Yes (1 of 2) | ~30–60s |
| 4 | Analysis: qualitative + adaptive | qualitative_researcher, adaptive_researcher | Yes | ~45–90s |
| 5 | Analysis: synthesizer | synthesizer | Yes | ~10–30s |
| 5b | Execution: guardrail enforcement | guardrail_enforcement | No | <1s |
| 6 | Decision: analyst | analyst (normal + halt scenarios) | Yes (Opus) | ~30–90s |
| 7 | Decision: strategist | strategist (normal + defensive_posture + emergency scenarios) | Yes (Opus) | ~10–15 min |
| 8 | Decision: proposal pre-processor | proposal_pre_processor (4 scenarios) | No | <1s |
| 9 | Decision: portfolio manager | pm (normal + halt + emergency + synchronous_rejection scenarios) | Yes (Opus) | ~20–30 min |
| 9c | Decision: pipeline composition | decision_pipeline (normal scenario) | Yes (Opus, x4 agents) | ~3–10 min |
| 9d | Operational: pipeline scheduler | pipeline_scheduler (one --once invocation) | Yes (full pipeline) | ~5–15 min |
| 10 | Reporting | build_e2e_report (HTML render of the archive) | No | <2s |

## Phase 0 — Type-layer self-check

Pure in-process checks against the position-thesis-model type layer (ALP-122
work tree). No SDK calls; no DB reads. Must complete in under 10 seconds.
Failures here invalidate every downstream layer — the type contracts the
pipeline builds on are broken.

```bash
uv run python scripts/verify_position_thesis_model.py
```

Verifies: 40 cases across 6 waves covering the 15 sub-stories of ALP-122
(bracket-thesis coverage validator, thesis-resolution classifier, strategy-payoff
utilities, seven additive schema fields, RegimeLabel relocation, typed bracket-leg
payloads, records/events/aggregates structural reorg, PositionRecord/PositionView
split, BasePositionProtocol, ThesisHealthSnapshot lifecycle).

**On failure:** read `scripts/RUNBOOK_position_thesis_model.md` § Failure-mode
triage. The FAIL output names the wave and scenario; match the wave number to
the responsible story IDs in that table. Do not proceed to Phase 1 — if the
type layer is broken, the data pipeline will still read and write successfully
but the downstream agents consuming typed records will fail at parse time in
harder-to-diagnose ways.

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
on the dev machine for collector errors. Don't proceed to phase 1b —
both the state-persistence schema check and the distillation pipeline
ultimately depend on the same DB.

## Phase 1b — State-persistence substrate

Pure in-process integration check against the durable substrate the
execution layer writes through. No SDK calls, no LLM cost. Sub-second
runtime against a fresh on-disk DB. Phase 1b proves the migration head is
applied + every write/read path is internally consistent + the snapshot
isolation contract holds.

```bash
uv run python scripts/verify_state_persistence.py \
    --db-path "$DB_PATH"
```

Verifies six phases against a freshly-migrated DB: schema (table
existence), `InvocationContext` round-trip (commit + rollback), Phase 1
fill-integration (PENDING→OPEN position transition + activity-log event
chain), Phase 2 envelope writeback (new position/thesis/bracket/orders
+ event chain), Phase 2 Layer-1 parse-failure (in-memory log + SQL
`envelope_parse_failed` parallel surfaces), and `SqlPortfolioStateRepository`
read parity (`assemble_snapshot` end-to-end + `RepositoryConsistencyError`
on pre-Phase-1 reads).

Phase E exercises only the Layer-1 `envelope_parse_failed` surface. The
Layer-2/3 `EventType.ENVELOPE_REJECTED` surface added in ALP-368 (for
guardrail/risk rejections post-parse) has no dedicated verify phase yet
— it's covered indirectly through the Phase 2 envelope-writeback round-trip.

`--db-path` defaults to the standard resolution chain (`DATABASE_PATH` env
var, then `config/main.yaml` `paths.database` key). For ad-hoc verification
against a fresh DB, the script's runbook documents an in-process snippet
that runs `Base.metadata.create_all` against a tmp-path DB.

**On failure:** read `scripts/RUNBOOK_state_persistence.md` § Failure-mode
triage. The FAIL output names the phase and a one-line diagnostic; match
the phase letter (A–F) to its row in the triage table. Pass-state confirms
the durable substrate is live; downstream pipeline-composition wiring
(ALP-310) can be invoked safely. Failure here invalidates phase 9 (the PM
runs against `submit_envelope` which writes through to the substrate); the
PM will silently degrade to in-memory-only operation if the substrate is
broken.

## Phase 1c — OMS commands

Pure in-process integration check against the OMS commands work tree
(ALP-120) — the canonical broker-grade Pydantic command shapes, the
PM-originated and engine-originated command-ID derivation utility, the
engine-stub `submit_envelope` MCP wrapper (PM envelope path), and the
monitor-facing `submit_engine_envelope` write function (engine envelope
path). No SDK calls, no LLM cost. Sub-minute runtime against a fresh DB.
Phase 1c proves the canonical command surface is internally consistent
and that the Phase 2 writeback consumes real command fields (no stub
constants).

Phase 1c requires a **freshly-migrated tmp DB** — not the prod snapshot
`$DB_PATH`. Phase 3 of the script seeds `cash_ledger.id='current'` as
part of the PM envelope path; the prod DB already has that row, so
`$DB_PATH` collides with `UNIQUE constraint failed: cash_ledger.id`.
Migrate a tmp DB to head and pass that:

```bash
mkdir -p /tmp/alphamind-verify-oms
rm -f /tmp/alphamind-verify-oms/alphamind.db
uv run alembic -c alembic.ini \
    -x db=/tmp/alphamind-verify-oms/alphamind.db upgrade head
uv run python scripts/verify_oms_commands.py \
    --db-path /tmp/alphamind-verify-oms/alphamind.db
```

The `-x db=<path>` form is the `alembic env.py` contract; plain
`-x db_url=…` is silently ignored (migration writes to the default path).

Verifies four phases against the freshly-migrated DB (the script's
`--help` labels the trailing summary line "Phase 5", but only four real
phases run): canonical Pydantic round-trip across all five command
variants (OPEN / CLOSE / ADJUST / CANCEL / ADD) plus `EngineEnvelope`;
command-ID utility (PM derivation against the `oms-command-ids.md`
worked example, engine derivation, parse round-trip, `attempt_seq`
computation); PM envelope path (`build_submit_envelope_mcp_server` →
Phase 2 writeback → activity log, asserting the persisted
`capital_reserved` amount equals the canonical command's real
`dollar_value` — proves the retired `$1k` stub from ALP-374 is gone);
engine envelope path (`submit_engine_envelope` → Phase 2 close
writeback → activity-log entry with `engine_guardrail` provenance
threaded through).

**On failure:** read `scripts/RUNBOOK_oms_commands.md` § Failure-mode
triage. The FAIL output names the phase and a one-line diagnostic; match
the phase number (1–4) to its row in the triage table. The most common
regression is a Phase 3 stub-constant reintroduction — the
`capital_reserved.amount_usd=1000.0` failure means `_writeback_open` is
again hard-coding a stub instead of reading
`command.position_size.dollar_value`.

## Phase 1d — Broker adapter (live paper environment)

Live integration check against Alpaca's paper environment. No SDK calls
(in the LLM sense), no LLM cost — but unlike phases 1b / 1c this one
talks to the live broker over the network. Runs in under two minutes
during US market hours; fill latency outside session can stretch the
runtime.

Phase 1d is the first verification step that exercises real broker
submissions. State produced (positions / orders / activity-log entries)
flows through to the OMS Phase 2 path, so the SQLite DB after this
phase reflects real Alpaca paper-mode positions.

```bash
set -a && source .env && set +a  # source ALPACA_PAPER_KEY + SECRET
mkdir -p /tmp/alphamind-verify-broker
rm -f /tmp/alphamind-verify-broker/alphamind.db
uv run alembic -c alembic.ini \
    -x db=/tmp/alphamind-verify-broker/alphamind.db upgrade head
DATABASE_PATH=/tmp/alphamind-verify-broker/alphamind.db \
    uv run python scripts/verify_broker_adapter.py
```

Phase 1d requires a **freshly-migrated tmp DB**, sourced Alpaca paper
credentials, `config/main.yaml` set to `execution_mode: paper`, and US
market hours for low-latency paper-mode fills (Phases 3 / 4 / 5 of the
verify wait for fills via the trade_updates websocket; off-session
latency exceeds the verify's per-phase budget).

Verifies seven phases: adapter substrate (factory + retry helper +
error classifier), account-state queries (every `AccountStateQueries`
method against the live paper account), full equity / single-leg
options / multi-leg strategy order lifecycles (OPEN → fill → CLOSE
round-trips through the OMS engine-stub coordinated swap), venue
configuration in isolation (constants check + calendar cache +
account-state surfacer + settlement calculator), and the
disconnect-recovery routine (`recover_missed_fills_since` over a 1-hour
lookback). Phase 6 (venue config) prints the operator-relevant venue
state — resolved margin-interest tier, day-trade headroom, PDT
qualification — to the verbose output.

**On failure:** read `scripts/RUNBOOK_broker_adapter.md` § Failure-mode
triage. The FAIL output names the phase and a one-line diagnostic; the
most common boot-time failure is missing credentials (`Alpaca paper
credentials not set: environment variable(s) ['ALPACA_PAPER_KEY'] are
unset or empty`) and the most common in-flight failure is paper-mode
buying-power exhaustion on Phases 3 / 4 / 5. DEFERRED phases are not
failures; the only operator action required for a `RESULT: PASS` line
with deferrals is to confirm the deferral rationale (live-mode
assertions in paper mode) matches the deferral message.

## Phase 1e — Corporate actions

Pure in-process integration check against the corporate-actions pipeline
(ALP-124 work tree). The script drains nine synthetic
`CorporateActionActivity` records — one per `CorporateActionType` member —
through `process_unprocessed_fills` inside an `InvocationContext`, and
asserts the post-state matches the per-action-type matrix from
`docs/design/05-execution-layer/corporate-actions.md` (quantity, cost
basis, ticker, status, cash impact, `corporate_action_adjustment_needed`
flag, one `corporate_action_integration_ledger` row per
`alpaca_activity_id`). A tenth check supplies a deliberately-offset
`PositionSnapshot` so the post-merge reconciliation step emits exactly one
`RECONCILIATION_ALERT`. No SDK calls, no live broker contact — synthetic
`PositionSnapshot` / `TradeAccountSnapshot` records stand in for Alpaca.
Sub-second runtime.

```bash
uv run python -m alphamind.scripts.verify_corporate_actions
```

Expected output is a 9-row pass/fail table (one per `CorporateActionType`
member) plus a reconciliation-summary line showing `alerts emitted=1,
expected=1`. Exit code `0` on full pass, `1` on any failure. The
cross-tree handoff: the chronological-merge step exercised here depends on
State persistence (Phase 1b) and Broker adapter (Phase 1d) being verified
clean upstream — the same `InvocationContext` substrate from 1b and the
same `PositionSnapshot` / `TradeAccountSnapshot` types from 1d are what
the corporate-actions handlers consume.

**On failure:** read `scripts/RUNBOOK_corporate_actions.md` § Failure-mode
triage. The FAIL output names the action row and a one-line diagnostic;
match the row label to its row in the triage table. Live integration of
the corporate-actions pipeline (real Alpaca v1beta1 fetch + Phase 1
drain against a production-shaped position set) belongs with ALP-123
(continuous monitor) when it lands.

## Phase 1f — Reg T margin attribution

Pure in-process integration check against the Reg T margin attribution
work tree (ALP-126). The script seeds a representative four-position
portfolio (long NVDA equity, short AMD equity, long SPY call, NVDA
bull-call-spread strategy) so the pre-fill Reg T and PM-equivalent
margins are non-trivial, then drains two unprocessed fills (a BUY entry
on a new PENDING NVDA long, a SELL partial-exit on a separate OPEN AMD
long) through `process_unprocessed_fills` inside an `InvocationContext`.
Rehydrates the per-fill `RegTMarginAttribution` records, invokes the
snapshot assembler so its Step 11 enrichment lands the trailing-window
aggregates on `CashLedger`, and asserts the headline algebra
(`regt_excess_over_pm == regt_marginal_consumption − pm_marginal_consumption`)
plus the trailing-30d aggregate identity. No SDK calls, no live broker
contact, sub-second runtime against a fresh on-disk DB.

Phase 1f requires a **freshly-migrated tmp DB** — not the prod snapshot
`$DB_PATH`. The seeding step writes a canonical four-position portfolio
that would collide with prod rows. Migrate a tmp DB to head and pass
that:

```bash
mkdir -p /tmp/alphamind-verify-regt
rm -f /tmp/alphamind-verify-regt/alphamind.db
uv run alembic -c alembic.ini \
    -x db=/tmp/alphamind-verify-regt/alphamind.db upgrade head
uv run python scripts/verify_regt_margin_attribution.py \
    --db-path /tmp/alphamind-verify-regt/alphamind.db \
    --invocation-id verify-regt-001
```

Expects: `PASS` with an 8-field `RegTMarginAttribution` block printed
per processed fill and a three-field trailing-window aggregate block
(`regt_excess_trailing_30d_usd` / `regt_excess_trailing_90d_usd` /
`regt_excess_lifetime_usd`) printed after the per-fill blocks. Exit code
`0` on full pass, `1` on any structured assertion failure (each failing
assertion appears on its own `- ` line in the FAIL trailer).

Stage artifact: the seeded DB at `/tmp/alphamind-verify-regt/alphamind.db`
carries `fill_records.regt_attribution_json` populated rows that
downstream phases (notably the command-center verify) may read for
cumulative delivery surface assertions.

**On failure:** read `scripts/RUNBOOK_regt_margin_attribution.md` §
Failure-mode triage. The FAIL output names the failing assertion in the
runbook's triage-table vocabulary (`algebra mismatch`,
`trailing-30d aggregate mismatch`, `non-finite attribution field`, etc.);
match the message to its row in the triage table.

## Phase 1g — Continuous monitor

Pure in-process integration check against the continuous monitor work tree
(ALP-123). The script exercises the five monitor responsibilities described
in `docs/design/05-execution-layer/architecture.md` § 4 — Alpaca fill-stream
consumption, guardrail breach detection + protective response, emergency
invocation triggering, options-greeks refresh orchestration, options
bracket-stop firing — against in-memory fixtures (synthetic portfolios,
fake broker / websocket / IV-fetch surfaces). No SDK calls, no live broker
contact, no live websocket calls. Sub-30s runtime; safe in CI.

```bash
uv run python scripts/verify_continuous_monitor.py
```

Exercises 11 documented scenarios labeled (a) through (k):

- (a) `UnderlyingPriceCache` accepts a quote.
- (b) `append_fill_record` persists a fake equity fill.
- (c) Greeks refresh fires on the scheduled cadence trigger.
- (d) Greeks refresh fires on the underlying-move trigger.
- (e) Greeks refresh failure preserves prior values + emits
  `GREEKS_REFRESH_FAILED`.
- (f) Halt onset emits `HALT_ACTIVATED`.
- (g) Immediate-action breach (`per_position_max_loss`) dispatches one
  engine envelope with `engine_guardrail` provenance.
- (h) Deferred-rule breach (`sector_concentration`) is NOT dispatched.
- (i) Options bracket-stop fires through the direct broker-adapter close
  path; one `POSITION_CLOSED` entry with `source=BRACKET_MANAGER` lands.
- (j) The strategist's `between_invocation_closures` projection contains
  both the cascade closure (from g) and the bracket-stop closure (from
  i), ordered chronologically.
- (k) Emergency request — regime jump fires once; second jump inside
  cooldown is suppressed. Scenario is `[SKIP]`-ed when ALP-439 has not
  landed (the `EMERGENCY_INVOCATION_REQUESTED` vocabulary is missing).

Stage-artifact handoff:

- **Inputs (consumes):** `fill_records` writes ← broker_adapter (ALP-121);
  `options_chains` reads ← collector (ALP-30); `activity_log` writes ←
  state_persistence (ALP-119); `submit_engine_envelope` ← OMS (ALP-375);
  `compose_phase_1_enforcement` ← guardrail_enforcement (ALP-125).
- **Outputs (produces):** `POSITION_CLOSED` events with
  `engine_guardrail` provenance (cascade closures from 04a),
  `POSITION_CLOSED` events with `BRACKET_MANAGER` source and
  `STOP_TRIGGERED` / `TARGET_REACHED` exit method (bracket-stop fires
  from 04c), `GREEKS_REFRESH_FAILED` events, `HALT_ACTIVATED` /
  `HALT_LIFTED` events, `EMERGENCY_INVOCATION_REQUESTED` events
  (consumed by the pipeline scheduler — ALP-431 — when that work tree
  lands).

Exit code: `0` on every scenario PASS (or SKIP for k); non-zero on any
FAIL, with the failing scenario(s) named in the `RESULT: FAIL` summary
line.

**On failure:** read `scripts/RUNBOOK_continuous_monitor.md` § Failure-mode
triage. The FAIL output names the scenario letter and a one-line
diagnostic; match the scenario label to the per-scenario row in the
triage table. The most common regressions are (a) a cascade-dispatcher
selector returning no candidate (scenario g), (b) the bracket watcher
not firing because `evaluate_price_based_trigger`'s direction-comparator
regressed (scenario i), or (c) the `HaltTransitionTracker` failing to
yield the activation entry on the inactive→active edge (scenario f).

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
invocation archive has 5 files (`tech_semis_sector.md`,
`financials_sector.md`, `energy_sector.md`,
`correlation_regime_brief.md`, `regime.md`). The
`[ PLACEHOLDER GAPS ]` section of the summary now reads
`None — all six Phase 2 categories integrated.` (post-ALP-484/485/486/487
compute/load splits; the orchestrator's `_PHASE_2_PLACEHOLDER_GAPS`
tuple is empty).

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
verdict, exit 0), `stop_reason` in `{end_turn, max_tokens}`. The
verbose output renders `tool_calls_used` and `stop_reason` but the
script does not assert a tool-call count.

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

## Phase 5b — Guardrail enforcement layer

Pure in-process integration check against the Phase 1 guardrail-enforcement
orchestrator. No SDK calls, no LLM cost. Sub-second runtime against a fresh
on-disk DB. Phase 5b proves the composition primitive + orchestrator +
repository-provider helper + assembler integration are wired together correctly
so the canonical `ActiveRiskParameterSet` consumed by the decision-layer agents
(Phases 6 / 7 / 9) is composed faithfully from the regime-resolved baseline
plus cumulative-drawdown progressive-tier overrides.

```bash
uv run python scripts/verify_guardrail_enforcement.py \
    --db-path "$DB_PATH"
```

Verifies four phases against a freshly-migrated DB: composition primitive
(four tier cases — no-tier / `CONSTRAINED` / `HEAVILY_CONSTRAINED` /
`FULL_HALT` — all loading their triggers from `config/guardrails.yaml`),
Phase 1 enforcement orchestrator (synthetic `RegimeAdaptationOutput` +
per-tier `DrawdownState` → `Phase1EnforcementResult`), repository provider
(`make_active_risk_parameters_provider` + `SqlPortfolioStateRepository.get_active_risk_parameters`
identity), and assembler integration (`assemble_snapshot` surfaces the
composed parameters at `snapshot.active_risk_parameters`).

`--db-path` defaults to the standard resolution chain (`DATABASE_PATH` env
var, then `config/main.yaml` `paths.database` key). For ad-hoc verification
against a fresh DB, the script's runbook documents the migration recipe
(both Alembic chain and in-process `Base.metadata.create_all`).

This phase runs after distillation (Phase 2 produces the regime label the
adaptation orchestrator translates into the regime-resolved baseline) and
before the decision-layer agents (Phases 6 / 7 / 9 consume
`snapshot.active_risk_parameters`). On a paper-evaluation harness run the
work tree's e2e harness exercises the same surface with live regime +
drawdown inputs; this phase keeps the contract verified in isolation.

**On failure:** read `scripts/RUNBOOK_guardrail_enforcement.md` § Failure-mode
triage. The FAIL output names the phase and a one-line diagnostic. Most
common: `missing required tables: <list>` (the DB schema is partial —
re-run the migration recipe), or per-tier mismatch
(`tier 1 / CONSTRAINED: tier=<X>, expected …`) which means
`config/guardrails.yaml` lost a non-halt tier or the classifier regressed.
Don't proceed to Phase 6 — if the guardrail-enforcement layer is broken,
the decision-layer agents will consume a malformed parameter set and
either reject correct proposals or accept invalid ones.

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

Pass `--scenario {normal,defensive_posture,emergency,all}` to re-run a
single scenario after a transient failure (default is `all`). Each
scenario is roughly one third of the phase's wall-clock budget.

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

Pass `--scenario {normal,halt,emergency,normal_with_breach,all}` to re-run
a single scenario (default is `all`). Sub-second total runtime so
selective re-run is rarely needed.

**Outputs.** Four bundle JSON files at
`tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json`,
consumed by the PM phase.

**Cost.** Zero. No SDK calls. Sub-second total runtime.

**See:** [`RUNBOOK_proposal_pre_processor.md`](RUNBOOK_proposal_pre_processor.md)

## Phase 9 — Portfolio manager

**Purpose.** Verify the PM agent (ALP-117) end-to-end against the real
Claude Agent SDK across four scenarios — `normal`, `halt`, `emergency`,
and `synchronous_rejection`. Each scenario exercises a different slice
of the PM's contract: clean approval flow, halt-mode risk-reduction-only
rendering, regime-transition breach handling, and the post-rejection
modification path triggered by an engine-stub `submit_envelope`
rejection.

**Prerequisites.** Phase 5 (synthesizer) must have completed — its
recorded `response.md` and `retrieval_store.json` are the brief inputs.
Phase 8 (proposal pre-processor) must have completed and emitted the
four pre-processor fixtures
`tests/fixtures/decision/proposal_pre_processor/{normal,halt,emergency,normal_with_breach}.json`
— the PM verifier reads each as the corresponding scenario's input.

**Run.**

```bash
uv run python scripts/verify_pm.py \
    --archive-root "$ARCHIVE_ROOT" \
    --synthesizer-invocation-id "$INVOCATION_ID" \
    --save-fixtures
```

Pass `--scenario {normal,halt,emergency,synchronous_rejection,all}` to
re-run a single scenario after a transient failure (default is `all`).
Each scenario is ~5–7 minutes of Opus wall-clock — selective re-run
materially shortens recovery from a single-scenario blip.

Verifies, per scenario: schema-valid `PMCompletionRecord`,
parser-clean completion sentinel,
`submission_log` containing zero rejections (or, for the
`synchronous_rejection` scenario, at least one rejection). Wall-clock ≤
PM's `latency_budget_seconds` × 4 scenarios. Exits 0 on PASS or WARN
per scenario; exits 1 on FAIL on any.

**Outputs.** Four fixture JSON files at
`tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json`
— each carrying `completion_record` (parsed sentinel), `submission_log`
(per-envelope record of the engine-stub's responses), and
`scenario_metadata` (verdict, tokens, wall clock). These will be
consumed by the decision-layer pipeline-composition wiring once
[ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310) lands.

**Cost.** Four Opus invocations; ~40K input + ~30K output tokens.

**See:** [`RUNBOOK_pm.md`](RUNBOOK_pm.md)

## Phase 9c — Decision-pipeline composition

Live-SDK end-to-end check of the decision-layer composition runner
(`alphamind.pipeline.decision.run_decision_pipeline`). Runs all four
decision-layer agents (analyst + strategist in parallel, then proposal
pre-processor, then PM) end-to-end against the real SDK in a single
composition, and validates the returned `DecisionPipelineResult` against
the parent issue's wiring invariants.

This phase runs **after** the per-agent verifies (Phases 6 / 7 / 9) so
the cheaper per-agent runs isolate any agent-side regression before the
~$1–3 composition spend lands. A per-agent FAIL upstream lets the
operator fix and re-run that one agent in seconds rather than discover
the regression mid-composition.

```bash
uv run python scripts/verify_decision_pipeline.py \
    --archive-root "$ARCHIVE_ROOT"
```

Verifies, in normal-mode only (halt and emergency are deferred per
parent decision G; the per-agent verifies in Phases 6 / 7 / 9 cover
modal coverage): pipeline returns a `DecisionPipelineResult`; analyst
output is structurally valid; strategist `position_assessments` covers
held positions; pre-processor `aggregate_observations` carries
populated `combined_set_impact` / `conviction_distribution` /
`book_health_summary` sub-blocks; PM `submission_log` is non-empty;
`pm_result.output.envelopes_submitted == len(submission_log)`. Exits 0
on PASS, 1 on FAIL (validation failure or any `HarnessFailure` from any
of the four agents).

The verify script supplies its own pre-recorded synthesizer text and a
fixture `RetrievalStore` rather than reading the live phase-5 archive
— **live synthesizer→decision chaining is the trigger layer's job and
is out of scope for this story**. A clean PASS at Phase 9c proves the
decision-layer wiring; it does not prove the cross-pipeline chain.

The serialized `DecisionPipelineResult` lands at
`<archive-root>/decision_pipeline/normal/result.json`; each per-agent
diagnostic archive lands at
`<archive-root>/invocations/<inv-id>/decision/<agent>/` (same shape
the per-agent verify scripts produce).

Runbook: `scripts/RUNBOOK_decision_pipeline.md`.

**On failure:** read the runbook's failure-mode triage section. The
failure block names the failing stage and any underlying agent; consult
the per-agent runbook (`RUNBOOK_analyst.md`, `RUNBOOK_strategist.md`,
or `RUNBOOK_pm.md`) for agent-specific triage. Phase 9c failures do
not block subsequent runs of the per-agent verifies — those cover
per-agent modal coverage independently — but a 9c FAIL flags a
wiring-level regression that should be triaged before relying on the
composition runner in production.

## Phase 9d — Pipeline scheduler

End-to-end check of the pipeline scheduler (ALP-431 work tree) — the
main entrypoint that drives the full Phase 1 → analysis pipeline →
decision pipeline → Phase 2 envelope dispatch sequence through one
`run_invocation` orchestrator pass and writes every artifact the
feedback loop joins against.

This phase runs **after** Phase 9c (the decision-pipeline composition
verifier) since the scheduler's orchestrator threads the decision
pipeline as one of its five phases — a 9c FAIL invalidates 9d, and
isolating decision-layer failures via 9c first is the cheaper path. It
also runs **after** Phase 1b (state-persistence substrate), Phase 1c
(OMS commands), Phase 1d (broker adapter), and Phase 5b (guardrail
enforcement) for the same reason: the scheduler composes every prior
layer's substrate, and a regression in any of them surfaces more
clearly when isolated.

```bash
uv run python scripts/verify_pipeline_scheduler.py \
    --archive-root "$ARCHIVE_ROOT"
```

Verifies nine checks against the paper DB: auth (`CLAUDE_CODE_OAUTH_TOKEN`
+ `ALPACA_PAPER_KEY` + `ALPACA_PAPER_SECRET` present); schema
(`invocations` / `process_lifetimes` / `activity_log` tables exist with
expected columns); process_lifetime row write (record_process_lifetime
returns a row id); one `--once` manual invocation through
`run_invocation` returns an `InvocationSummary`; the 22-field
`invocations` row is fully populated (`phase1_completed_at` /
`phase2_completed_at` set, JSON columns parse, snapshot paths exist on
disk); at least one `activity_log` entry was emitted for this
invocation; the archive directory under
`<archive-root>/invocations/<invocation_id>/` exists with
`resolved_config.json`; the story 04b vocabulary additions are wired
(`RunType.emergency`, `EventType.EMERGENCY_INVOCATION_REQUESTED`,
`config/run_types/emergency.yaml`, cooldown=30). Exit 0 on all-pass.

The script drives `trigger_type=manual` /
`trigger_source=verify_pipeline_scheduler` end-to-end against the
production-shape paper DB (no fixture DBs), so it doubles as the
production-readiness gate for the scheduler. Cost is the full pipeline
invocation — one Sonnet pass through the analysis layer and one Opus
pass through the four decision-layer agents (~$1–$5).

Per the verify script's convention, the testable predicates are
factored into helpers under
`alphamind.scripts.verify_pipeline_scheduler`; the end-to-end `--once`
invocation is exercised by the verify script itself, not by the unit
tests.

Runbook: `scripts/RUNBOOK_pipeline_scheduler.md`. The **downstream
consumer** of Phase 9d is the continuous-monitor phase (Phase 1g) which
shares the `EMERGENCY_INVOCATION_REQUESTED` activity-log vocabulary with
the scheduler's emergency-receiver task; the monitor writes those entries
and the scheduler reads + dispatches against them.

**On failure:** read the runbook's failure-mode triage section. The
verify script prints one PASS/FAIL line per check; the FAIL line names
the failing step and a one-line diagnostic. The most common causes are
(a) the operator forgot to source `.env` before running (FAIL: auth),
(b) the DB hasn't been migrated to head (FAIL: schema), or (c) the
orchestrator hit a per-layer failure inside `run_invocation` (FAIL:
once_invocation; the exception's type and message names the failing
layer, and the per-layer runbook covers the triage).

## When complete

Report a one-line summary to the operator:

```
End-to-end verification: <PASS|FAIL|WARN-only>
- Phase 0 (types): PASS (40/40 cases)
- Phase 1 (data): PASS
- Phase 2 (distillation): PASS
- Phase 3 (domain researchers): PASS
- Phase 4 (qualitative + adaptive): PASS
- Phase 5 (synthesizer): WARN (2 invented references)
- Phase 6 (analyst): normal=PASS, halt=PASS
- Phase 7 (strategist): normal=PASS, defensive_posture=PASS, emergency=PASS
- Phase 8 (proposal pre-processor): normal=PASS, halt=PASS, emergency=PASS, normal_with_breach=PASS
- Phase 9 (portfolio manager): normal=PASS, halt=PASS, emergency=PASS, synchronous_rejection=PASS
- Phase 9c (decision-pipeline composition): normal=PASS
- Phase 9d (pipeline scheduler): 9/9 checks PASS
- Phase 1g (continuous monitor): 11/11 scenarios PASS
Total LLM cost: ~Xk Sonnet input + Yk Sonnet output, ~Zk Opus input + Wk Opus output
Archives under .archive/verify-pipeline-YYYYMMDD/
Fixtures at tests/fixtures/decision/{analyst,strategist,proposal_pre_processor,pm}/*.json
```

If WARN-only or any FAIL, attach the per-script verdict block(s) so
the operator can act.

## Phase 10 — Generate HTML report

After the run is complete (or when summarizing for someone else), assemble
a single self-contained dark-mode HTML report from the archive:

```bash
uv run python scripts/build_e2e_report.py \
    --archive-root "$ARCHIVE_ROOT" \
    --invocation-id "$INVOCATION_ID" \
    --phase-summary "$ARCHIVE_ROOT/phase_summary.json" \
    --output "$ARCHIVE_ROOT/e2e-report.html"
```

The script auto-discovers the analyst / strategist / PM scenario invocations
under `<archive-root>/invocations/`, loads the stage artifacts (sector
briefs, qualitative + adaptive briefs, retrieval store, PM submission logs),
and renders one section per phase plus a top-level run-summary table. The
PM section renders each envelope from `submission_log.json` as a card
(verdict-coded border, evaluation pills, anti-pattern chips, concerns,
rationale narrative, and any emitted OMS commands). Open the file in a
browser — it carries its own CSS via the sidecar at
`scripts/_e2e_report_assets/style.css`.

Phases 0, 1, 1b, and 1c produce no archive artifacts, so their verdicts +
notes default to "PASS / baseline numbers." If the run differs from the
baseline (a stale collector, a state-persistence regression, a tweak to
phase 0's case count), pass `--phase-summary <path.json>` to override.
Generate the schema with:

```bash
uv run python scripts/build_e2e_report.py --print-phase-template
```

Operators typically save a hand-edited `phase_summary.json` next to the
archive (e.g. `<archive-root>/phase_summary.json`). Common overrides:

```json
{
  "phase-1": {
    "verdict": "WARN",
    "note": "fred.macro stale (operator pre-cleared)",
    "collectors": [
      {"name": "fred.macro", "age": "15:13:46 ago (cap 8:00)", "status": "FAIL"},
      {"name": "polygon.equity", "age": "(market closed)", "status": "SKIP"}
    ]
  },
  "phase-7": {
    "verdict": "FAIL",
    "note": "defensive_posture failed schema-conditional validation"
  }
}
```

Per-collector rows for phase 1 are visible in the report; if you want them
populated, copy the rows out of the `verify_ongoing_collection.py` console
output into the override.

**Reference example.** `docs/reports/e2e-verification-2026-05-09.html` is a
worked example from the 2026-05-09 run — it covers the full envelope-card
layout (4 PM scenarios, 21 envelopes), a phase-7 FAIL on the strategist
defensive_posture scenario, a phase-9 WARN on synchronous_rejection, and a
phase-1 WARN on `fred.macro`. Use it as a visual reference for what a
"normal" run report should look like, and a sanity check that
`build_e2e_report.py` still renders the same structure after edits.

## Cost summary

| Script | Model | Input tokens | Output tokens |
|--------|-------|--------------|---------------|
| verify_position_thesis_model | (none) | 0 | 0 |
| verify_distillation | Sonnet | 12K–16K | 2K–4K |
| verify_domain_researchers | Sonnet | 18K–24K | 3K–6K |
| verify_qualitative_researcher | Sonnet | 6K–8K | 0.4K–0.6K |
| verify_adaptive_researcher | Sonnet | 8K–10K | 0.7K–1K |
| verify_synthesizer | Sonnet | 10K–12K | 1.5K–2K |
| verify_analyst (×2 scenarios) | Opus | 12K–18K | 3K–5K |
| verify_strategist (×3 scenarios) | Opus | 24K–36K | 30K–55K |
| verify_proposal_pre_processor | (none) | 0 | 0 |
| verify_pm (×4 scenarios) | Opus | 32K–48K | 24K–40K |
| **Sonnet total** | | **~54K–70K** | **~7.6K–13.6K** |
| **Opus total** | | **~68K–102K** | **~57K–100K** |

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

2. **No execution-layer verification.** Anything downstream of the PM
   (real OMS submission, breach behavior, broker-side execution,
   persistence) is outside this runbook's scope. The PM phase covers
   only the engine-stub `submit_envelope` MCP wrapper — no live
   broker contact. The decision-layer pipeline-composition wiring
   (ALP-310) is not yet built; its verifier will consume the PM
   fixtures `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json`
   once landed. Check `docs/project-tracker.md` for current status.

3. **No "run all" wrapper.** This runbook is the closest thing.
   Sequence is manual; if any phase changes (new script, removed
   script, args drift), this runbook needs updating. Phase 10's
   `build_e2e_report.py` is the closest the run gets to a single
   summary artifact, but it only renders state — it does not drive
   any of the verification phases.

## When a script's CLI doesn't match this runbook

Run `--help` on the script:

```bash
uv run python scripts/verify_<name>.py --help
```

The script docstrings are authoritative; this runbook is a curated
sequence wrapper. If you find drift, flag it to the operator and
update the runbook in the same change.

## References

- `scripts/RUNBOOK_position_thesis_model.md` — phase 0 failure triage (ALP-122 work tree).
- `scripts/RUNBOOK_state_persistence.md` — phase 1b failure triage (ALP-119 work tree).
- `scripts/RUNBOOK_oms_commands.md` — phase 1c failure triage (ALP-120 work tree).
- `scripts/RUNBOOK_broker_adapter.md` — phase 1d failure triage (ALP-121 work tree).
- `scripts/RUNBOOK_corporate_actions.md` — phase 1e failure triage (ALP-124 work tree).
- `scripts/RUNBOOK_domain_researchers.md` — phase 3 failure triage.
- `scripts/RUNBOOK_qualitative_researcher.md` — phase 4 failure triage.
- `scripts/RUNBOOK_adaptive_researcher.md` — phase 4 failure triage.
- `scripts/RUNBOOK_synthesizer.md` — phase 5 failure triage.
- `scripts/RUNBOOK_analyst.md` — phase 6 failure triage.
- `scripts/RUNBOOK_strategist.md` — phase 7 failure triage.
- `scripts/RUNBOOK_proposal_pre_processor.md` — phase 8 failure triage.
- `scripts/RUNBOOK_pm.md` — phase 9 failure triage.
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
- `scripts/build_e2e_report.py` — phase-10 HTML report builder; reads the
  archive plus an optional `--phase-summary` JSON override and renders a
  dark-mode page with PM-envelope cards.
- `scripts/_e2e_report_assets/style.css` — sidecar stylesheet the report
  loads at render time.
- `docs/reports/e2e-verification-2026-05-09.html` — worked example of the
  rendered report (full envelope-card layout, phase-7 FAIL, phase-9 WARN,
  phase-1 WARN on fred.macro).
- `docs/design/cost-and-rate-limit-modeling.md` — cap budgets and
  per-agent token expectations.
- `docs/architecture/llm-integration.md` § Authentication —
  `CLAUDE_CODE_OAUTH_TOKEN` setup.
