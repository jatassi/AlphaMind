# Project tracker

---

## Ready for implementation

Each entry is a feature ready to have its implementation requirements defined as user stories tracked in Linear. Items are listed in rough dependency order — earlier items unblock later ones.

Status:

- _requirements pending_ — needs user stories drafted
- _stories drafted_ — user stories exist; implementation has not begun
- _in progress_ — some stories complete
- _done_ — all stories complete and verified

Cross-cutting policies and reference specs that constrain implementation but aren't features themselves — architecture decisions, failure-handling policies, asset universe methodology, source-to-target mappings, API key inventory, scenario tests, and the unit/integration test plans — live under `docs/architecture/` and `docs/design/` and are linked from the design docs of the features that consume them.

### Foundation

- [x] **Configuration management** ([ALP-8](https://linear.app/alphamind-jatassi/issue/ALP-8)) — _done_ — [design](design/configuration-management.md)

### Data layer

- [x] **Collector** ([ALP-30](https://linear.app/alphamind-jatassi/issue/ALP-30)) — _done_ — [design](design/01-data-layer/collector/)
- [x] **Portfolio state** ([ALP-53](https://linear.app/alphamind-jatassi/issue/ALP-53)) — _done_ — [design](design/01-data-layer/internal/portfolio-state.md)

### Distillation layer

- [*] **Distillation** ([ALP-74](https://linear.app/alphamind-jatassi/issue/ALP-74)) — _Partially done_ — [external](design/02-distillation-layer/external.md), [internal](design/02-distillation-layer/internal.md)
- [*] **Threshold calibration framework** ([ALP-76](https://linear.app/alphamind-jatassi/issue/ALP-76)) — _Partially done_ — [design](design/02-distillation-layer/threshold-calibration.md). Stories 02 (config schema), 03 (state schema), 04 (bootstrap framework), 07 (Class B refresh), 09 (regime classification), 13 (verification incl. `verify_regime_transition.py`), 14a (audit trail + calibration log convention), 14b (no-magic-numbers audit), 15 (emergency-invocation review), 16 (flag-rate reporter), 17 (calibration-state snapshot + dashboard panel) tracked in Linear.
- [x] **Replay harness** ([ALP-75](https://linear.app/alphamind-jatassi/issue/ALP-75)) — _done_ — [design](design/02-distillation-layer/replay-harness.md)

### Risk guardrails

- [x] **Rules & limits** ([ALP-6](https://linear.app/alphamind-jatassi/issue/ALP-6)) — _done_ — [design](design/06-risk-guardrails/rules-and-limits.md)
- [x] **Guardrail evaluation primitives** ([ALP-134](https://linear.app/alphamind-jatassi/issue/ALP-134)) — _done_ — [design](design/06-risk-guardrails/guardrail-evaluation.md)
- [x] **State delivery** ([ALP-143](https://linear.app/alphamind-jatassi/issue/ALP-143)) — _done_ — [design](design/06-risk-guardrails/state-delivery.md)
- [x] **Regime adaptation** ([ALP-213](https://linear.app/alphamind-jatassi/issue/ALP-213)) — _done_ — [design](design/06-risk-guardrails/regime-adaptation.md)
- [x] **Breach behavior** ([ALP-212](https://linear.app/alphamind-jatassi/issue/ALP-212)) — _done_ — [design](design/06-risk-guardrails/breach-behavior.md)

### Analysis layer

- [x] **Domain researchers** ([ALP-113](https://linear.app/alphamind-jatassi/issue/ALP-113)) — _done_ — [tech-semis](design/03-analysis-layer/domain-researchers/tech-semis.md), [financials](design/03-analysis-layer/domain-researchers/financials.md), [energy](design/03-analysis-layer/domain-researchers/energy.md)
- [x] **Qualitative research** ([ALP-111](https://linear.app/alphamind-jatassi/issue/ALP-111)) — _done_ — [design](design/03-analysis-layer/qualitative-research.md)
- [x] **Adaptive research** ([ALP-112](https://linear.app/alphamind-jatassi/issue/ALP-112)) — _done_ — [design](design/03-analysis-layer/adaptive-research.md)
- [x] **Synthesizer** ([ALP-114](https://linear.app/alphamind-jatassi/issue/ALP-114)) — _done_ — [design](design/03-analysis-layer/synthesizer.md)

### Decision layer

- [ ] **Analyst** ([ALP-115](https://linear.app/alphamind-jatassi/issue/ALP-115)) — _requirements pending_ — [design](design/04-decision-layer/analyst.md), [output schema](design/04-decision-layer/analyst-output-schema.md)
- [ ] **Strategist** ([ALP-116](https://linear.app/alphamind-jatassi/issue/ALP-116)) — _requirements pending_ — [design](design/04-decision-layer/strategist.md), [output schema](design/04-decision-layer/strategist-output-schema.md)
- [ ] **Proposal pre-processor** ([ALP-118](https://linear.app/alphamind-jatassi/issue/ALP-118)) — _requirements pending_ — [design](design/04-decision-layer/proposal-pre-processor.md), [bundle schema](design/04-decision-layer/proposal-pre-processor-bundle-schema.md)
- [ ] **Portfolio manager** ([ALP-117](https://linear.app/alphamind-jatassi/issue/ALP-117)) — _requirements pending_ — [design](design/04-decision-layer/portfolio-manager.md), [envelope schema](design/04-decision-layer/pm-envelope-schema.md), [submit_envelope tool schema](design/04-decision-layer/submit-envelope-tool-schema.md)

### Execution layer

- [ ] **Position & thesis model** ([ALP-122](https://linear.app/alphamind-jatassi/issue/ALP-122)) — _requirements pending_ — [position model](design/05-execution-layer/position-model.md), [thesis model](design/05-execution-layer/thesis-model.md), [orders & brackets](design/05-execution-layer/orders-and-brackets.md)
- [ ] **State persistence** ([ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119)) — _requirements pending_ — [design](design/05-execution-layer/state-persistence.md). _Distillation-track stories blocked on this substrate (the SQL `invocations` and `activity_log` tables and a transactional invocation context):_ `14a-config-change-emission`, `15-emergency-invocation-review-report`, `16-flag-rate-empirical-reporter`. When the substrate ships, set the three stories to `not_started` and dispatch.
- [ ] **OMS commands** ([ALP-120](https://linear.app/alphamind-jatassi/issue/ALP-120)) — _requirements pending_ — [design](design/05-execution-layer/oms-commands.md), [command schema](design/05-execution-layer/oms-command-schema.md), [engine envelope schema](design/05-execution-layer/engine-envelope-schema.md), [command IDs](design/oms-command-ids.md)
- [ ] **Broker adapter** ([ALP-121](https://linear.app/alphamind-jatassi/issue/ALP-121)) — _requirements pending_ — [design](design/05-execution-layer/broker-adapter.md), [venue configuration](design/05-execution-layer/venue-configuration.md)
- [ ] **Guardrail enforcement layer** ([ALP-125](https://linear.app/alphamind-jatassi/issue/ALP-125)) — _requirements pending_ — [design](design/05-execution-layer/architecture.md)
- [ ] **Corporate actions** ([ALP-124](https://linear.app/alphamind-jatassi/issue/ALP-124)) — _requirements pending_ — [design](design/05-execution-layer/corporate-actions.md)
- [ ] **Reg T margin attribution** ([ALP-126](https://linear.app/alphamind-jatassi/issue/ALP-126)) — _requirements pending_ — [design](design/05-execution-layer/regt-margin-attribution.md)
- [ ] **Continuous monitor** ([ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123)) — _requirements pending_ — [design](design/05-execution-layer/architecture.md)

### Operational tooling

- [ ] **LLM output validation** ([ALP-127](https://linear.app/alphamind-jatassi/issue/ALP-127)) — _requirements pending_ — [design](design/testing/llm-output-validation.md)
- [ ] **Paper-evaluation harness** ([ALP-130](https://linear.app/alphamind-jatassi/issue/ALP-130)) — _requirements pending_ — [design](design/05-execution-layer/paper-evaluation-harness.md)
- [ ] **Counterfactual replay engine** ([ALP-129](https://linear.app/alphamind-jatassi/issue/ALP-129)) — _requirements pending_ — [design](design/05-execution-layer/counterfactual-replay-engine.md)
- [ ] **Command center** ([ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128)) — _requirements pending_ — [design](design/command-center.md), [pipeline schema](design/pipeline-control-and-events-schema.md), [monitor schema](design/monitor-control-and-events-schema.md)
- [ ] **Feedback loop** ([ALP-131](https://linear.app/alphamind-jatassi/issue/ALP-131)) — _requirements pending_ — [design](design/feedback-loop.md)

---


## To-do list

### Quick wins

_Single-line spec edits, config additions, doc cross-references, or deferrals._

#### Land analysis-layer prompts _(Configuration management)_

- [x] Land production-quality prompts at `prompts/analysis/{tech_semis_researcher,financials_researcher,energy_researcher,qualitative_researcher,adaptive_researcher,synthesizer}.md` so the path-existence validator in story 03i (configuration management) can pass. Each prompt implements the design-doc contract for its agent — role, operating context, inputs, task, method, tool policy (where applicable), output contract, worked example, anti-pattern constraints — at the same readiness as the existing `prompts/decision/{analyst,strategist,pm}.md`. Side-effect cleanup: `portfolio_analyst` was identified as a hallucinated agent (no design doc; references traced to the initial commit only) and struck from `architecture/llm-integration.md`, `architecture/system-characterization.md`, `design/configuration-management.md`, `design/cost-and-rate-limit-modeling.md`, `design/feedback-loop.md`, `design/05-execution-layer/state-persistence.md`, and the `03i` / `04e` configuration stories. The strategist subsumes the role the phantom would have played. _Source: discovered while orchestrating the configuration-management implementation work tree on 2026-04-27; resolved 2026-04-27._

#### Update end-to-end verification runbook + script CLIs from 2026-05-03 dry run _(Operational tooling)_

- [ ] Three findings from the first end-to-end exercise of `scripts/RUNBOOK_end_to_end_verification.md`:
  - **Three verify scripts ignore `--db-path`**: `scripts/verify_bootstrap.py`, `scripts/verify_ongoing_collection.py`, and `scripts/verify_synthesizer.py` have no argparse — they only read `DATABASE_PATH` env var. The runbook documents `--db-path` for all three. Fix: add argparse to the three scripts (matching the pattern in the seven that already have it) so the runbook's invocation pattern is uniform.
  - **macOS-side snapshot workflow undocumented**: the runbook says "point to a local replica via `--db-path`" but doesn't document how to make a replica from the live production DB. Direct reads against `/Volumes/Users/jacks/AlphaMind/data/alphamind.db` while the collector is writing fail with `sqlite3.OperationalError: database is locked` (SMB+WAL+remote-writer). Working procedure is `cp` of the WAL trio (`alphamind.db` + `alphamind.db-wal` + `alphamind.db-shm`) into a local `data/alphamind-snapshot.db*`. Add this to the prerequisites section.
  - **Phase 3 needs `PYTHONPATH=.`** for `verify_adaptive_researcher.py` (separately tracked in this section). The runbook should mention it until that script is fixed.
- [ ] _Source: 2026-05-03 first end-to-end run; the runbook explicitly invites this kind of edit ("If you find drift, flag it to the operator and update the runbook in the same change")._

#### `session.py` should fail loudly when default-path fallback would create an empty DB _(Foundation)_

- [ ] `_resolve_path` in `src/alphamind/persistence/session.py:34` falls through silently to `_default_db_path()` (`~/AlphaMind/data/alphamind.db` on POSIX, `%USERPROFILE%\AlphaMind\data\alphamind.db` on Windows) when no explicit path is provided, no `DATABASE_PATH` env var, and `config/main.yaml` either doesn't exist or has an unresolved value. SQLAlchemy then creates an empty SQLite file at that path on first connection, and downstream callers see "all tables MISSING" rather than "no DB configured". Verified by the 2026-05-03 `verify_bootstrap.py` invocation: passing `--db-path /Volumes/...` (which the script ignores because it has no argparse) caused fall-through, an empty 9.4MB DB at `~/AlphaMind/data/alphamind.db`, and a misleading 17-table-missing FAIL report. Fix: add an `ALPHAMIND_REQUIRE_EXPLICIT_DB_PATH` opt-in env var (default off for backwards compat) that raises `RuntimeError("no database path configured")` when the resolution chain would fall through. Or, simpler: have `make_engine` log a WARNING when the default path is used so the operator sees it. _Source: surfaced during the 2026-05-03 verification run; sister items at "Cross-platform `%VAR%` expansion" + "Update end-to-end verification runbook + script CLIs" address the proximate causes; this entry hardens the underlying defensive behavior._

#### `verify_adaptive_researcher.py` PYTHONPATH workaround _(Operational tooling)_

- [ ] `scripts/verify_adaptive_researcher.py` imports `tests.analysis.adaptive_research.fixtures` for its E2E fixture loader at runtime (`src/alphamind/scripts/verify_adaptive_researcher.py:668`), but `tests/` isn't on `sys.path` under `uv run python scripts/...`. Crashes on import with `ModuleNotFoundError: No module named 'tests'`. Workaround: `export PYTHONPATH=.` before invoking. Fix: either (a) script self-augments `sys.path` with the repo root in `_load_e2e_fixtures_lazy`, or (b) move the E2E fixture data out of `tests/` into a runtime-importable location (e.g., `src/alphamind/analysis/adaptive_research/_e2e_fixtures.py`). _Source: surfaced during the 2026-05-03 verification run; the runbook's invocation example doesn't include the PYTHONPATH prefix and crashes immediately on a fresh shell._

#### Suppress benign `aclose()` warning from analysis-layer harnesses _(Analysis layer)_

- [ ] After the 2026-05-03 fix that adds `break` to `_collect_response` after a successful ResultMessage (to avoid the SDK's post-result `Command failed exit 1` bug), the early loop exit can leave the SDK's internal async generator un-drained. On exit, the SDK's `__aexit__` / cleanup path emits `RuntimeError: aclose(): asynchronous generator is already running` to stderr. Cosmetic only — verification still passes. Fix: either drain the generator explicitly (`async for _ in sdk_query_fn(...): pass` after the break-bearing loop completes) or call `await query.aclose()` on the iterator before returning. _Source: surfaced during the 2026-05-03 verify_synthesizer run; same harnesses landed earlier in the day._

#### Cross-platform `%VAR%` expansion in YAML-supplied DB path _(Foundation)_

- [ ] `_resolve_path` in `src/alphamind/persistence/session.py:50` uses `os.path.expandvars(db_path)` to expand `%USERPROFILE%` in `main.yaml`'s `paths.database`. This works on Windows (where `os.path.expandvars` honors `%VAR%`) but not on POSIX (where the function only expands `$VAR` / `${VAR}`). Side-effect: `tests/test_persistence.py::TestResolvePath::test_yaml_path_expands_environment_variables` fails on macOS dev machines because `%ALPHAMIND_TEST_ROOT%` survives the call unchanged. Fix: replace the call with a manual `re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: os.environ.get(m.group(1), m.group(0)), db_path)` so the same string-substitution semantics work on both OSes. _Source: surfaced by the full pytest run on macOS during the 2026-05-03 verification; the production-side fix at `41e0ffa` solved the Windows boot-time crash but did not survive cross-platform testing._

#### Windows-safe directory fsync in profile_switch _(Risk guardrails)_

- [ ] Guard the directory-fsync block in `_rewrite_active_profile` (`src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py:143`) with `if sys.platform != "win32":` (or drop the parent-dir fsync entirely — it is a Linux-only durability primitive and POSIX `os.replace` already gives the in-tree atomicity Windows can offer). Currently `os.open(<dir>, os.O_RDONLY)` raises `PermissionError [Errno 13]` on Windows because Win32 rejects directory handles via the POSIX `_open` shim. Two tests in `tests/risk_guardrails/rules_and_limits/test_profile_switch.py` (`test_switch_to_different_profile_rewrites_main_yaml`, `test_rewrite_preserves_every_non_active_profile_field`) currently fail on the production Windows server; the bug also surfaces if `/profile-switch` is ever invoked there. _Source: discovered while restarting the production collector after the 2026-05-03 distillation-tables migration; fix is small but blocked from being bundled with that work._

### Moderate

_Schema additions, well-scoped multi-file edits, or single-component contributions._

#### Windows-safe TemporaryDirectory cleanup in replay harness _(Distillation layer)_

- [ ] Replace the `tempfile.TemporaryDirectory(prefix="alphamind_replay_")` context manager at `src/alphamind/distillation/replay_harness/engine.py:533` with a manual `mkdtemp` + `shutil.rmtree(..., onerror=_retry_on_winerror_32)` (or an equivalent retry loop) so the harness runs cleanly on Windows. On exit, `TemporaryDirectory` walks the tree calling `os.unlink` on `isolated.sqlite`; on POSIX an unlink of an open file is fine, but on Windows any lingering SQLite handle (connection-pool finalizer, child-process inheritance) takes a mandatory share-lock and the unlink fails with `PermissionError [WinError 32] The process cannot access the file because it is being used by another process`. Currently 24 tests across `tests/distillation/replay_harness/{test_engine,test_cli,test_e2e}.py` fail on the Windows server — basically the entire replay-harness e2e surface — even though the harness itself runs fine on POSIX dev machines. Test-only friction in practice (the harness is invoked from dev), so the fix can wait, but it should land before any operator wants to drive replays from the Windows box. _Source: surfaced while running the full pytest suite on the Windows server after the 2026-05-03 distillation-tables migration._

#### Loosen analysis-layer parsers + tighten prompt examples + harden retry directive _(Analysis layer)_

- [ ] Multiple analysis-layer parsers reject model output for literal-template regexes Sonnet drifts from under load. Reproduced on the 2026-05-03 verification run across at least two agents:
  - **Domain-researcher parser** (`src/alphamind/analysis/domain_researchers/parser.py`): `_CONVICTION_RE = r"^(low|moderate|high)\s+with\s+(.+)$"` (line 445) requires the literal word "with" — Sonnet emits `moderate — technical alignment...`; `_DEGRADED_REASON_RE` (line 54) requires the bracketed `[If DEGRADED: reason — ...]` template — Sonnet writes a free-form indented `Signal quality: DEGRADED\n  Sentiment aggregates absent...` instead.
  - **Qualitative-researcher parser** (`src/alphamind/analysis/qualitative_research/parser.py`): same `signal_quality is DEGRADED but no reason line found` failure on the bracketed-template regex; SAME root cause as the domain-researcher case.
  - **Adaptive-researcher parser** (`src/alphamind/analysis/adaptive_research/parser.py`): `Invocation header 'inv-2026-05-03T00-00Z' does not match caller-supplied invocation_id` — model invents its own invocation ID instead of echoing the one supplied in the input bundle. Parser is strict (must match exactly); model needs prompt example showing the literal echo pattern.
  - **Corrective-retry directive** (across all analysis harnesses): when given the retry directive, the model frequently prepends a conversational preamble (`The parse error is clear — a DEGRADED signal quality line requires...`) before producing the corrected brief, tripping the parser's `first non-blank line must be exactly 'QUALITATIVE BRIEF'` (or sector-equivalent) check.
- [ ] Three-part fix:
  - **(a) Loosen regexes** to accept reasonable separators (`(low|moderate|high)[\s,—\-:]+(.+)`, free-form `Reason:` variant for DEGRADED).
  - **(b) Add explicit RIGHT/WRONG format examples** in `prompts/analysis/{tech_semis,financials,energy,qualitative}_researcher.md` so the model defaults to canonical syntax.
  - **(c) Strengthen the corrective-retry directive** in each harness to explicitly say "no preamble, no acknowledgment, output the corrected brief starting with the envelope marker".
- [ ] Verification surfaced this on the 2026-05-03 run: energy sector parsed cleanly (9 tickers, lower cognitive load), financials failed both attempts (17 tickers, more drift), tech_semis was killed mid-flight by `asyncio.gather`'s fail-fast when financials raised, qualitative-researcher failed both attempts (issues 1 + 2 hit on initial vs retry). _Source: 2026-05-03 verification run; the contract is achievable (energy proves it) but unforgiving._

#### Compress sector-researcher input bundle to design budget _(Analysis layer)_

- [ ] Reduce the per-sector input bundle from ~15K tokens back down to the ~9K target named in `docs/design/cost-and-rate-limit-modeling.md:128`. Today the bundle's distillation slice (`SectorOutput.text`) dumps verbose YAML `per_ticker:` blocks for every q1/q3/q6/q7 indicator over the full sector roster — e.g., `q1.divergence_flags` produces 58 lines for 17 financials tickers even when every row is `divergence_pairs: []` and `rsi_1d` near 50. Two approaches: (1) compact format — collapse each indicator's `per_ticker:` map to one row per ticker (e.g., `AXP rsi_1d=51.42 div=[]`), saving ~3× on whitespace; (2) anomaly-pruned format — drop per-ticker rows for tickers that fired no anomaly in that block, leaving the block header + only the noteworthy entries. Touches `src/alphamind/distillation/sector_assembly.py` and the per-q format helpers in `q1/output_blocks.py`, `q3_options/`, `q6/`, `q7/`. Stretch: also have `src/alphamind/analysis/domain_researchers/input_bundle.py` enforce a `context_token_budget` ceiling at assembly time and raise rather than silently overrun. Once compressed, revert the budget bumps in `config/agents.yaml` (240→120s, 16000→8000 ctx) for the three sector researchers. _Source: surfaced during the 2026-05-03 verification run; the 17-ticker financials sector produced 62.8KB / 1461-line bundles that timed out the 120s SDK budget twice in a row before bumping the budgets unblocked phase 3._

#### `verify_bootstrap_calibration_mix` cold-start tolerance _(Distillation layer)_

- [ ] On a freshly-migrated DB (no prior `distillation_ticker_baseline` rows), the first orchestrator invocation tags every per-ticker baseline as `bootstrap` because the calibration framework's `decide_calibration_state(observed_n, required_n)` only returns `CALIBRATED` when `observed_n >= required_n` and the cold start has zero rows accumulated against the lookback window. The verifier's high-freq band `[80%, _]` for `volume` / `atr` / `spread` then fails (0% calibrated). Path forward: either (a) extend the verifier to skip the band check when the DB-side row count matches the per-kind universe size and every row is `bootstrap` with `n_observations < window_days` (cold-start signature), or (b) backfill `distillation_ticker_baseline` from the OHLCV history at migration time so the first invocation runs against pre-calibrated state. Option (a) is cheaper and matches the existing `deferred=True` pattern on `distillation_contract_history`. _Source: surfaced during the 2026-05-03 end-to-end verification run on a freshly-migrated production snapshot; pre-existing baseline rows on a long-running prod DB would mask the issue._

#### Wire prediction-market contract scope into distillation orchestrator _(Distillation layer)_

- [ ] Wire `run_external_distillation` to compute and pass a non-empty `contract_scope` into `refresh_contract_history` and `compute_prediction_market_deltas`. Currently `src/alphamind/distillation/orchestrator.py:248` hardcodes `contract_scope=()`, so `distillation_contract_history` is never populated despite the data layer carrying ~49K `prediction_market_contracts` rows and ~2.5M snapshots. Per `docs/design/01-data-layer/external/qualitative.md` § 3, every invocation should refresh deltas for all in-scope contracts so the analysis pipeline sees trajectory, not just current level. Recommended scope: a category-curated allowlist (Fed funds, OPEC, election outcomes, etc. per qualitative.md examples) lived in a new `prediction_market.tracked_categories: [...]` key under `config/distillation.yaml`, joined against `prediction_market_contracts` filtered to `resolution_date > as_of OR resolution_date IS NULL`. Avoid the all-active fallback (~35K contracts/invocation) — it's real DB pressure on every cycle. Once wired, drop the `deferred=True` flag on the `distillation_contract_history` probe in `src/alphamind/scripts/verify_distillation.py:97`. _Source: surfaced during the 2026-05-03 end-to-end verification run; the orchestrator's empty-scope stub was an acknowledged TODO that the verifier had been silently flagging as STALE. Verifier now reports DEFERRED until this work lands._

#### Phase 1 enforcement-layer composition wiring _(Risk guardrails)_

- [ ] Build the per-invocation Phase 1 composition step that produces the final `ActiveRiskParameterSet` consumed by state-delivery's renderers and the engine's T3 check. Sequence (per `state-delivery.md § Portfolio state ingestion payload`): `regime_adaptation.resolve_regime_adaptation()` produces the regime-resolved + overlay-applied `ActiveRiskParameterSet`; `breach_behavior.classify_cumulative_drawdown_tier()` reads `DrawdownState.current_drawdown_pct`; `breach_behavior.apply_progressive_tier_overrides()` further-tightens position-size / gross-exposure values when a tier is active. The composed result is what state-delivery renders and what engine T3 checks against. _Source: surfaced by the risk-guardrails story-review audit on 2026-04-28; the regime-adaptation work tree's orchestrator (story 09) explicitly does NOT call into breach-behavior, and breach-behavior's `apply_progressive_tier_overrides` (story 04b) names no caller. Both stories carry an "integration site" note pointing here. Likely home: a new story under the execution-layer / continuous-monitor work tree once that work tree's scope is drafted. Until this lands, scenario tests and the breach-behavior E2E story 08 inline the composition._

### Substantial

_New infrastructure, cross-cutting consolidations, or UI surfaces._

#### Analysis-layer pipeline composition wiring _(Analysis layer)_

- [ ] Compose the analysis-layer agents into a single per-invocation pipeline runner: `run_external_distillation` → three `run_<sector>_researcher` calls (parallel) + `run_qualitative_researcher` (parallel) → `run_adaptive_researcher` (consumes upstream typed briefs) → `run_synthesizer`. Each agent's runner returns a typed `*Result` value; the composition runner threads `DistillationOutputs`, `tuple[SectorBrief, ...]`, `QualitativeBrief`, `CorrelationRegimeBrief`, and `AdaptiveBrief` through to the synthesizer's input bundle and out to the decision-layer agents. Per-trigger budget overrides from `config/run_types/<trigger>.yaml` apply to every agent's `agent_config` lookup. The adaptive-researcher work tree (Adaptive research, _stories drafted_) and the synthesizer work tree (Synthesizer, _stories drafted_) each ship with fixture-based E2E verification but defer live composition to this story. _Source: surfaced by the adaptive-research drafting on 2026-05-02 (parent issue ALP-112 § Pre-resolved configuration decisions § H); the synthesizer work tree's own E2E expectations land at draft time of that work tree's stories._

### Deferred

_Items whose work is gated on an external trigger (vendor activation, downstream consumer materializing, sub-decision needed) rather than on operator capacity. Each entry names its trigger; promote to Quick wins / Moderate / Substantial when the trigger fires._

#### Mapping coverage — deferred per-category schemas _(Data layer)_

- [x] Q4 short selling and Q5 partial (earnings-estimate revisions) landed under the two-tier approach. Sources confirmed via POC: FINRA CDN (daily Reg SHO short volume, bi-weekly short interest) and iBorrowDesk's undocumented JSON endpoint (`/api/ticker/{TICKER}` — daily collector + on-demand single-ticker refresh, paced ≥5s to avoid the observed HTTP 444 rate-limit block). Storage tables `short_interest_snapshots`, `short_volume_daily`, `borrow_cost_daily`, `borrow_cost_intraday`, and `earnings_estimate_revisions` documented in [storage.md](design/01-data-layer/collector/storage.md); providers `finra` and `iborrowdesk` registered in [`data_sources.yaml`](../config/data_sources.yaml) (corrects the prior `q4_short_selling.primary: sec_edgar` misconfiguration); collector cron entries added to [`collector_schedule.yaml`](../config/collector_schedule.yaml); module skeletons listed in [data-sources.md § Module layout](design/01-data-layer/collector/data-sources.md). Implementation work split into user stories `05k-finra-vendor-adapter`, `05l-iborrowdesk-vendor-adapter`, `05m-finnhub-estimate-revisions` tracked in Linear. The remaining five deferred categories convert to forward-trigger entries below; each lands when its consumer materializes per the storage doc's deferral reasons.

**Forward-trigger entries** _(promote when the trigger fires)_:

- **Q2 microstructure / order flow / dark pool.** Trigger: Polygon tick-data subscription activation. Entities `TradeFlowAggregate`, `LiquiditySnapshot`, `InstitutionalFlow`, `AuctionData`, `IntradayFlowPattern`, `ExtendedHoursFlow` already exist in `schema/order_flow.py`; storage tables land when the upstream tick feed comes online.
- **Q8 commodities specialized (futures curves, CFTC positioning).** Trigger: paper-trading evidence of commodity-positioning theses needing futures-curve / COT inputs. Sub-decision required first: CFTC bulk CSV vs. their Public Reporting Environment (PRE) API; CME futures-curve source pending.
- **Qual2 social sentiment.** Trigger: distillation layer requesting a `SocialSentimentScore` input. StockTwits provider already configured (`qual2_social.primary: stocktwits`); storage table lands when the consumer demands it.
- **Qual4 earnings transcripts.** Trigger: qualitative-research agent or analyst tool requesting transcript bodies. Hybrid pattern (metadata in DB, transcript text on disk) per Qual1's `news_articles`. Source choice (Motley Fool / Quartr / YouTube + Whisper) deferred until consumer requirements are clear.
- **Qual6 sector-specific qualitative catalysts.** Trigger: qualitative-research layer build-out. LLM-derived; the storage shape will track whatever the qualitative-research agent emits (likely event-shaped rows).

#### Earnings transcript NLP pipeline _(Analysis layer)_

- [ ] Specify the transcript ingestion pipeline and the NLP extraction pipeline (tone classification, Q&A clustering, non-answer detection, forward-looking statement extraction). Trigger: earnings commentary data source acquired. _Source: [qualitative-research.md](design/03-analysis-layer/qualitative-research.md)._

**Context.** Pairs with the data-layer Qual4 forward-trigger entry above — both gated on a transcript source being in hand. Tier 2 of the `earnings_commentary` tool currently degrades to `partial_no_transcript` for every call. Schemas (`ManagementToneAnalysis`, `AnalystQADynamics`, `ForwardLookingStatement`, `NonAnswerFlag`, `QAExchange` in [schema/earnings_commentary.py](design/01-data-layer/schema/earnings_commentary.py)) and mappings ([mappings/earnings_commentary.yaml](design/01-data-layer/mappings/earnings_commentary.yaml)) already exist with transcript-derived fields marked `derived from TBD`; storage hybrid pattern pre-cleared in [storage.md §Deferred categories — Qual4](design/01-data-layer/collector/storage.md). Source choice (Quartr / Motley Fool / YouTube + Whisper) and extraction architecture (single-pass LLM vs. two-stage deterministic structuring + LLM annotation) decided at promotion.
