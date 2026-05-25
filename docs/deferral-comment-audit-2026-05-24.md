# Deferral-comment audit — 2026-05-24

Systematic scan of every Python comment in the AlphaMind codebase for references to potentially-deferred functionality. Motivation: untracked deferrals (code with comments anticipating behavior that may not exist) have surfaced repeatedly as a class of bug during validation runs.

## Method

- 25 parallel Haiku subagents scanned `src/`, `tests/`, `scripts/` in chunks.
- 5 follow-up rescans + 1 targeted gap scan closed coverage where the initial pass under-read files.
- Each scanner flagged comments matching deferral patterns (TODO/FIXME/HACK, temporal hedges, placeholders, phase/version refs, cross-component handoffs, `NotImplementedError`, etc.) and skipped pure WHAT-docs, lint pragmas, license headers, and rationale comments about already-implemented behavior.
- Scanners listed candidates only — no investigation. Operator triage renders the verdict per entry.

## Coverage

| Surface | Files | Coverage |
|---|---|---|
| `src/` | 685 | 685/685 (after rescan + gap) |
| `tests/` | 614 | 614/614 |
| `scripts/` | 22 | 22/22 |
| Total | 1,321 | full |

## Totals

| Group | Count |
|---|---|
| A — Untracked (no ALP-XXX reference in comment) | 176 |
| B — Tracked (cites ALP-XXX or already-landed story) | 31 |
| **Total flagged** | **207** |

Triage convention: a finding is presumed **tracked** if the comment cites a Linear issue ID or names a story that has clearly landed. **Untracked** = highest priority for verdict-rendering, since these are the candidates most likely to be the phantom references that have caused validation bugs.

---

## Group A — Untracked deferrals

### src/execution

- **src/alphamind/execution/broker_adapter/errors.py:7** — `later stories` — References deferred helpers for permanent rejections
- **src/alphamind/execution/continuous_monitor/breach_loop/wiring.py:48** — `Story-04a callback placeholder — replaced when the cascade dispatcher lands`
- **src/alphamind/execution/continuous_monitor/breach_loop/wiring.py:52** — `Story-04b callback placeholder — replaced when the emergency trigger lands`
- **src/alphamind/execution/continuous_monitor/breach_loop/wiring.py:56** — `Persistence-side sink placeholder — replaced once activity-log writes wire up`
- **src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:7** — `replaces each stub with substrate`
- **src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:216** — `library_config_factory later overrides`
- **src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:726** — `future monitor consumer of regime_transition_breaches`
- **src/alphamind/execution/continuous_monitor/bracket_stops/task.py:141** — `conservative-by-design placeholder`
- **src/alphamind/execution/continuous_monitor/bracket_stops/task.py:149** — `placeholder — actual fill cascade overwrites this`
- **src/alphamind/execution/continuous_monitor/bracket_stops/triggers.py:63** — `does not currently read`
- **src/alphamind/execution/continuous_monitor/emergency_trigger/cooldown.py:69** — `future audit may key on it`
- **src/alphamind/execution/continuous_monitor/underlying_stream/reader.py:9** — `minimal SqlOpenPositionsReader`
- **src/alphamind/execution/continuous_monitor/greeks_refresh/state.py:105** — `reserved for future invariant-logging`
- **src/alphamind/execution/continuous_monitor/session.py:4** — `later stories`
- **src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py:51** — `minimal stub without going through AccountStateQueries`
- **src/alphamind/execution/continuous_monitor/__main__.py:710** — `future subcommand addition does not silently fall through`
- **src/alphamind/execution/corporate_actions/dispatch.py:8** — `replaces those stubs with real implementations`
- **src/alphamind/execution/corporate_actions/handlers/cash_dividends.py:67** — `currently supports equity positions only` — Deferred options/strategy support
- **src/alphamind/execution/corporate_actions/handlers/splits.py:39** — `currently supports equity positions only` — Deferred options/strategy support
- **src/alphamind/execution/corporate_actions/handlers/_handler_base.py:102** — `future stories with a richer snapshot can replace this read`
- **src/alphamind/execution/corporate_actions/reconciliation.py:90** — `caller has not yet wired the account fetch (the pre-04 default)`
- **src/alphamind/execution/guardrail_enforcement/composition.py:6** — `(eventually) by the continuous monitor when it`
- **src/alphamind/execution/oms/broker_dispatch.py:8** — `synthetic-acknowledgment behavior of the engine-stub`
- **src/alphamind/execution/oms/broker_dispatch.py:11** — `broker routing was deferred and lands here`
- **src/alphamind/execution/oms/submit_engine_envelope.py:79-81** — `_ENVELOPE_ID_PATTERN comment mentions validation already enforced by Pydantic at Layer-1` — Defense-in-depth implies envelope construction can bypass Pydantic
- **src/alphamind/execution/oms/submit_engine_envelope.py:286** — `from alphamind.execution.write_paths.phase2 import` — Phase 2 import — verify phase2 actually exists
- **src/alphamind/execution/oms/submit_engine_envelope.py:292** — `see phase2.py rationale_metadata build`
- **src/alphamind/execution/oms/submit_engine_envelope.py:433** — `a hardcoded NotImplementedError`
- **src/alphamind/execution/paper_evaluation_harness/spread.py:37-41** — `constants are model parameters (not operator knobs) per the parent decision E and the harness's calibrated-not-pessimistic principle` — Hardcoded calibration constants
- **src/alphamind/execution/paper_evaluation_harness/harness.py:59** — `caller persists no estimate when None returned due to missing data` — Intentional graceful skip path
- **src/alphamind/execution/regt_margin_attribution/__init__.py:9** — `This package provides the configuration skeleton (story 01a). Later stories...`
- **src/alphamind/execution/write_paths/phase1.py:255** — `currently unused; the signature is forward-shaped`
- **src/alphamind/execution/write_paths/phase1.py:384** — `future revisions can extend this with price-plausibility and order-existence checks`
- **src/alphamind/execution/write_paths/phase1.py:588** — `raise NotImplementedError(msg)` — Defensive — verify unhandled variants can't occur
- **src/alphamind/execution/write_paths/phase1.py:604-605** — `SHORT entry fills not yet supported by Phase 1; supported direction is LONG only`
- **src/alphamind/execution/write_paths/phase1.py:931** — `BRACKET_INCOMPLETE_WARNING entry for operator follow-up`
- **src/alphamind/execution/write_paths/phase1.py:1137-1139** — `While any leg is still a zero-count skeleton (an entry not yet fully filled)`
- **src/alphamind/execution/write_paths/phase1.py:1776-1779** — `Thin shim: delegate to the corporate_actions package dispatcher`
- **src/alphamind/execution/write_paths/phase2/__init__.py:313** — `raise NotImplementedError(msg)` — Defensive variant dispatch

### src/distillation

- **src/alphamind/distillation/_config_domain.py:19** — `can be applied to each as a follow-up`
- **src/alphamind/distillation/_repository.py:7** — `in-memory stub`
- **src/alphamind/distillation/_repository.py:11-12** — `defers broader propagation to follow-up issues`
- **src/alphamind/distillation/orchestrator.py:635** — `has not yet been built (no VVIX entries`
- **src/alphamind/distillation/orchestrator.py:658** — `has not yet been built` — VX1 collector
- **src/alphamind/distillation/orchestrator.py:683** — `placeholder snapshot without fabricating`
- **src/alphamind/distillation/orchestrator.py:744** — `placeholder caller propagates that state`
- **src/alphamind/distillation/orchestrator.py:1349** — `{} placeholder the operator sees`
- **src/alphamind/distillation/calibration_snapshot.py:225** — `doesn't currently track`
- **src/alphamind/distillation/q3/_loaders.py:366** — `multi-quarter Protocol-propagation follow-up`
- **src/alphamind/distillation/q3/assemble.py:584** — `not yet routed through the orchestrator`
- **src/alphamind/distillation/q6/assemble.py:365** — `not yet routed through the orchestrator`
- **src/alphamind/distillation/q7/assemble.py:68** — `not yet routed through the orchestrator`
- **src/alphamind/distillation/regime.py:565** — `future implementations cache the value, switch`
- **src/alphamind/distillation/qualitative/__init__.py:21** — `which is now a thin backward-compat shim over the per-classifier modules`
- **src/alphamind/distillation/qualitative/news_price_divergence_compute.py:47** — `raise it as a future story rather than tuning silently here`

### src/data_sources

- **src/alphamind/data_sources/polygon/corporate_actions.py:240** — `later rows fall through` — Multiple deactivating actions per ticker not handled
- **src/alphamind/data_sources/finnhub/estimate_revisions.py:281** — `tuples are skipped (idempotent)`
- **src/alphamind/data_sources/finnhub/news.py:153** — `the target is skipped` — Failing targets skip silently
- **src/alphamind/data_sources/finnhub/news.py:182** — `skipped {len(failed_targets)} targets` — Logged but not persisted for retry
- **src/alphamind/data_sources/finra/short_volume.py:15** — `file not yet published`
- **src/alphamind/data_sources/finra/short_volume.py:113** — `silently skipped` — 404s without retry mechanism
- **src/alphamind/data_sources/finra/short_volume.py:152** — `not yet published`
- **src/alphamind/data_sources/finra/short_interest.py:18** — `not yet published`
- **src/alphamind/data_sources/finra/short_interest.py:136** — `not yet published`
- **src/alphamind/data_sources/finra/short_interest.py:173** — `not yet published`
- **src/alphamind/data_sources/finra/client.py:65** — `file not yet published`
- **src/alphamind/data_sources/prediction_market/polymarket/contracts.py:133** — `until a future ingestion catches it`
- **src/alphamind/data_sources/prediction_market/kalshi/contracts.py:96** — `exhausted busy_timeout` — Rate-limit blocking without queue
- **src/alphamind/data_sources/sec_edgar/rss.py:79** — `safe placeholder` — Default fallback for missing env var
- **src/alphamind/data_sources/marketaux/client.py:54** — `Probe the API with a minimal request`
- **src/alphamind/data_sources/bls/client.py:89** — `posting a minimal probe request`

### src/risk_guardrails

- **src/alphamind/risk_guardrails/library_snapshot.py:74** — `For StrategyPositionDetails, reads the pre-aggregated strategy_greeks field (no per-leg aggregation needed — the assembler pre-computes this)` — Assumes upstream strategy_greeks pre-computation
- **src/alphamind/risk_guardrails/library_snapshot.py:129** — `Mirror _order_notional_estimate in execution/write_paths/phase2/cancel.py` — Cross-module logic-mirror, fragile if either side drifts
- **src/alphamind/risk_guardrails/library_snapshot.py:172** — `position-level placeholder` — Multi-leg strategy placeholder
- **src/alphamind/risk_guardrails/delta_adjusted.py:64-73** — `CLOSE on options/strategy may omit option_legs ... library does not synthesize a proposal DAE` — Skips DAE computation on CLOSE
- **src/alphamind/risk_guardrails/delta_adjusted.py:104-106** — `A multi-leg STRATEGY has no position-level direction (direction is None — ALP-603)`
- **src/alphamind/risk_guardrails/effective_limits.py:1-14** — `The adapter does not re-cascade — the regime/overlay/mode multiplication is compose_config's responsibility`
- **src/alphamind/risk_guardrails/feature_gate.py:18-20** — `Detailed handling (allow CLOSE-only on disabled features, etc.) lives at the entry point in story 05`
- **src/alphamind/risk_guardrails/iv_sourcing.py:12-15** — `The Polygon-backed production adapter lands when the options collector is built` — **Polygon options IV adapter not built**
- **src/alphamind/risk_guardrails/iv_sourcing.py:94-96** — `fallback is the underlying's trailing 30-day realized volatility` — Fallback path when IV surface unavailable
- **src/alphamind/risk_guardrails/projection.py:20-24** — `Rules with novel classification needs require either a new flag (rare, design-doc-driven) or a custom contribution function`
- **src/alphamind/risk_guardrails/risk_budget.py:35-50** — `Sector-concentration rule_ids are not enumerated here; the renderer's sector-label resolver handles those`
- **src/alphamind/risk_guardrails/rules/__init__.py:69** — `Sector-concentration specs are generated dynamically: one RuleSpec per sector ∈ config.active_sectors`
- **src/alphamind/risk_guardrails/regime_adaptation/__init__.py:9-11** — `Module load order (types first) is load-bearing` — **Fragile module load order; innocuous refactor would break it**
- **src/alphamind/risk_guardrails/regime_adaptation/inputs_assembly.py:64** — `richer per-rule metadata is the rules-and-limits feature's job and will land via a later integration`
- **src/alphamind/risk_guardrails/breach_behavior/cascade.py:766** — `a strategy yields None and has no position-level side, so it is excluded from a directional-candidate set`
- **src/alphamind/risk_guardrails/guardrail_evaluation/projection.py:20** — `operator-tunable later if proved wrong`
- **src/alphamind/risk_guardrails/guardrail_evaluation/projection.py:40** — `Operator-tunable later if the band proves wrong`
- **src/alphamind/risk_guardrails/rules_and_limits/registry.py:73** — `direct dataclass instantiation by future code`
- **src/alphamind/risk_guardrails/state_delivery/validation_tool.py:84** — `_validate_cross_field_invariants enforces the direction is None ⇔ asset_type is STRATEGY invariant`
- **src/alphamind/risk_guardrails/state_delivery/primitives.py:97** — `_ZONE_WARNING_THRESHOLD = 0.70` — **Hardcoded numeric anchor**

### src/analysis

- **src/alphamind/analysis/tools/__init__.py:100** — `Transcript analysis is not yet available (Tier 2 deferred)`
- **src/alphamind/analysis/tools/earnings_commentary.py:6** — `Tier 2 fields (transcript_available, transcript_analysis) are stub-shaped`
- **src/alphamind/analysis/tools/earnings_commentary.py:12** — `Tier 2 not yet built`
- **src/alphamind/analysis/tools/earnings_commentary.py:272** — `deferred: populates when analyst-rating ingestion lands`
- **src/alphamind/analysis/_sdk_subprocess.py:25** — `here for now; siblings added incrementally`

### src/portfolio_state

- **src/alphamind/portfolio_state/pricing.py:68** — `Revisit when a true remote pricing service lands`
- **src/alphamind/portfolio_state/repository.py:143** — `revisit if/when Postgres lands`
- **src/alphamind/portfolio_state/assembler.py:429** — `field is set to 0.0 here as a placeholder; the second-pass enrichment`
- **src/alphamind/portfolio_state/events/position_lifecycle.py:60** — `exit_price=0 as a placeholder while Phase 1 reconciliation is pending`

### src/state + persistence

- **src/alphamind/state/tables/invocations.py:11** — `Phase 1 / Phase 2 write paths (later stories) UPDATE these columns`
- **src/alphamind/state/tables/invocations.py:28** — `future direct-SQL writer (e.g. a backfill script)`
- **src/alphamind/state/tables/cash_ledger.py:14** — `Phase 1 / Phase 2 write paths do not maintain it`
- **src/alphamind/state/tables/cash_ledger_codec.py:30** — `Any future field additions require a matching codec update`
- **src/alphamind/state/tables/orders.py:11** — `future direct-SQL writer faces fail-closed guarantees`
- **src/alphamind/state/tables/positions.py:49** — `future direct-SQL writer (e.g. a backfill script) faces`
- **src/alphamind/state/tables/theses.py:15** — `future direct-SQL writer faces the same fail-closed guarantees`
- **src/alphamind/state/tables/brackets.py:36** — `future direct-SQL writer faces the same fail-closed guarantees`
- **src/alphamind/state/tables/process_lifetimes.py:21** — `future direct-SQL writer (e.g. a backfill script)`
- **src/alphamind/state/repository/sql_repository.py:15** — `thesis quality aggregates as an empty default placeholder`
- **src/alphamind/state/repository/sql_repository.py:160** — `follow-up feedback-loop story populates them`
- **src/alphamind/state/repository/sql_repository.py:192** — `follow-up feedback-loop story populates them` — win-rate/profit-factor
- **src/alphamind/persistence/migrations/versions/1f8b3c7d2a4e_vvix_percentile_nullable.py:9** — `The VVIX collector does not yet exist`

### src/decision

- **src/alphamind/decision/_shared/prompt_file.py:24** — `The synthesizer and the four analysis-layer researchers do not pass output_format` — Documents asymmetry
- **src/alphamind/decision/analyst/harness.py:154** — `Without the user_message in the retry SDK call, the model loses portfolio / synthesizer / guardrail context` — ALP-311 retry-context-loss; verify retry path actually carries the message
- **src/alphamind/decision/strategist/harness.py:161** — Same retry-context-loss pattern in strategist
- **src/alphamind/decision/portfolio_manager/models.py:1** — `backwards-compat re-export shim`
- **src/alphamind/decision/portfolio_manager/harness.py:29** — `Tests pass a stub so no test touches the Anthropic API`
- **src/alphamind/decision/portfolio_manager/submit_envelope/persist.py:13** — `default the StatePersistenceConfig argument uniformly via _stub_state_persistence_config`
- **src/alphamind/decision/portfolio_manager/submit_envelope/persist.py:105** — `Construct a no-op StatePersistenceConfig for callers that didn't supply one`
- **src/alphamind/decision/portfolio_manager/submit_envelope/persist.py:108** — `Phase 2 doesn't read any knob in this story; the config is part of the forward-shaped signature only`
- **src/alphamind/decision/portfolio_manager/submit_envelope/server.py:132** — `writes through every accepted envelope to SQL via the Phase 2 write`
- **src/alphamind/decision/portfolio_manager/submit_envelope/server.py:223** — `every accepted envelope writes through to SQL via the Phase 2`
- **src/alphamind/decision/portfolio_manager/submit_envelope/server.py:232** — `before Phase 2 writeback`
- **src/alphamind/decision/portfolio_manager/submit_envelope/server.py:366** — `using a placeholder`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:68** — `Phase 2 writeback is skipped for an abandoned command`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:204** — `invocation_id retained on the signature for future provenance threading`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:237** — `raise NotImplementedError(msg)`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:287** — `raise NotImplementedError(msg)`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:329** — `raise NotImplementedError(msg)`
- **src/alphamind/decision/portfolio_manager/submit_envelope/dispatch.py:386** — `raise NotImplementedError(msg)`
- **src/alphamind/decision/portfolio_manager/submit_envelope/types.py:49** — `command_id_counter is not currently incremented`
- **src/alphamind/decision/strategist/harness.py:18** — `Tests pass a stub so no test touches the Anthropic API`
- **src/alphamind/decision/strategist/input_bundle.py:542** — `Bracket: not yet activated`

### src/config

- **src/alphamind/config/assets_views.py:21** — `A future schema change that renames or relocates`
- **src/alphamind/config/assets_views.py:90** — `future schema changes surface as a visible warning`
- **src/alphamind/config/tools.py:21** — `Future stories may extend the set`
- **src/alphamind/config/validation/cross_reference.py:321** — `so a future bypass of parse-time validation still catches the divergence`

### src/scheduler + _kernel

- **src/alphamind/_kernel/clock.py:13** — `future stories can re-use the same Protocol`
- **src/alphamind/_kernel/ids.py:14-15** — `story 05a will add the constructor when consumers migrate` — **Symbol/OccSymbol validation constructor deferred**
- **src/alphamind/_kernel/exception_group.py:22-24** — `CancelledError children are an artifact of the cancellation cascade, not the originating failure`
- **src/alphamind/scheduler/__main__.py:6-11** — `empty registry shipped in story 01 until stories 04a / 04b register tasks` — **Placeholder task registration in daemon loop**
- **src/alphamind/scheduler/__main__.py:179** — `InvocationSummary later`
- **src/alphamind/scheduler/invocation.py:137** — `does not currently expose a "latest snapshot path" helper` — Missing functionality in distillation.calibration_snapshot
- **src/alphamind/scheduler/debug_e2e/seed.py:217-219** — `Fixture greeks ... defers options_chains seeding; downstream falls back` — **Options data scaffolding deferred**
- **src/alphamind/scheduler/debug_e2e/seed.py:263-268** — `net-credit strategy needs sign convention before ... it can be projected` — **Net-credit strategy variant unsupported**
- **src/alphamind/scheduler/debug_e2e/seed.py:461-462** — `filled_quantity and average_fill_price are left at defaults` — Fabricated fill fields omitted
- **src/alphamind/scheduler/debug_e2e/seed.py:530** — `future deadline is the simplest satisfying option`
- **src/alphamind/scheduler/debug_e2e/broker.py:105-107** — `A future net-credit variant will need a sign convention here before it can be projected`
- **src/alphamind/scheduler/orchestrator.py:717-732** — `Falls back to now - 24h when no successful prior row exists` — Bootstrap fallback window
- **src/alphamind/scheduler/phase1_inputs.py:86** — `fallback is a sane mid-cycle scalar so the bootstrap path does not blow up`

### src/misc (collector / commands / pipeline / scripts / etc.)

- **src/alphamind/collector/scheduler.py:128-132** — `polygon needs 2: polygon.equity holds executor 6-13 min ... polygon.options gets misfired` — **Operational workaround for executor contention**
- **src/alphamind/commands/command_models.py:467-474** — `rejecting non-pl_percentage targets here keeps the OPEN write path's ... unreachable`
- **src/alphamind/scripts/verify_bootstrap_calibration_mix.py:32** — `deferred=True state-table pattern in verify_distillation`

### tests/

- **tests/execution/oms/test_broker_dispatch.py:838** — `datetime/UTC/Any kept for future fixture parity`
- **tests/distillation/q7/test_assemble_blocks.py:4** — `_placeholder_blocks("q7") stub`
- **tests/distillation/external/test_q1_assemble_blocks.py:6** — `_placeholder_blocks("q1")`
- **tests/distillation/external/test_q3_assemble_blocks.py:3** — `_placeholder_blocks("q3") stub`
- **tests/risk_guardrails/breach_behavior/fixtures/builders.py:189** — `extend the mapping if a future scenario needs a different unit`
- **tests/risk_guardrails/regime_adaptation/test_regime_mapping.py:122** — `Locked here so a future refactor that changes either`
- **tests/risk_guardrails/regime_adaptation/test_regime_mapping.py:225** — `so a future refactor cannot quietly relax it`
- **tests/risk_guardrails/state_delivery/test_validation_tool.py:1067** — `locked here so future strategy-writeback support inherits a tested contract`
- **tests/risk_guardrails/guardrail_evaluation/test_registry_and_project_all.py:416** — `so a future per-position rule cannot silently leave its hook unwired`
- **tests/risk_guardrails/guardrail_evaluation/test_types.py:5** — `Library math lands in later stories`
- **tests/decision/portfolio_manager/test_validation.py:1300-1304** — `warnings are emitted in future`
- **tests/decision/portfolio_manager/test_prompt_round_trip.py:20** — `so a future edit cannot silently re-nest them`
- **tests/decision/strategist/test_prompt_round_trip.py:211** — `a future edit that re-introduces`
- **tests/decision/analyst/test_harness.py:474** — `a future schema with a property`
- **tests/decision/analyst/test_runner.py:652** — `once their work trees land`
- **tests/state/test_theses_table.py:543** — `JSON list so future extensions can rehydrate them; for now we verify zero-signal`
- **tests/portfolio_state/computations/test_live_adjusted_pnl.py:6** — `future evaluation reports`
- **tests/config/test_synthesizer_entry.py:58** — `future contributor must NOT "fix" this empty list by adding global tool names`

---

## Group B — Tracked deferrals (cite ALP-XXX or landed story)

- **src/alphamind/execution/corporate_actions/dispatch.py:62** — `not yet implemented (all except SPLIT until stories 03a-03d land)`
- **src/alphamind/execution/corporate_actions/reconciliation.py:18** — `Deferred to ALP-123 (continuous monitor's reconciliation pass)`
- **src/alphamind/execution/counterfactual_replay_engine/__init__.py:1** — `placeholder namespace (scheduled for ALP-129)`
- **src/alphamind/execution/orders_and_brackets/__init__.py:1** — `placeholder namespace (partially shipped as ALP-122)`
- **src/alphamind/execution/orders_and_brackets/__init__.py:12** — `orders_and_brackets scheduled for ALP-122`
- **src/alphamind/execution/paper_evaluation_harness/lookups.py:131** — `the real per-ticker realized-vol substrate is tracked by ALP-530`
- **src/alphamind/distillation/orchestrator.py:1025** — `ALP-467 follow-ups`
- **src/alphamind/risk_guardrails/library_snapshot.py:270** — `ALP-489 — view USD fields carry Money; cash_usd is the repository-boundary float`
- **src/alphamind/risk_guardrails/breach_behavior/cascade.py:178** — `ALP-603 lands, a strategy passes an inert "long" placeholder here`
- **src/alphamind/risk_guardrails/state_delivery/validation_tool.py:76** — `(ALP-603)` strategy-direction reference
- **src/alphamind/risk_guardrails/regime_adaptation/active_parameters.py:14** — `companion build_synthetic_regime_output shim retired in ALP-513`
- **src/alphamind/risk_guardrails/guardrail_evaluation/risk_budget.py:9** — `Replaces the pre-ALP-503 empty stub the SQL repository returned`
- **src/alphamind/portfolio_state/records/thesis_quality.py:5** — `shim exists so the ~22 existing import sites keep working` (ALP-347)
- **src/alphamind/portfolio_state/records/activity_log.py:4** — `shim exists so the ~28 existing import sites keep working` (ALP-347)
- **src/alphamind/decision/portfolio_manager/submit_envelope/persist.py:3** — `After ALP-458 broke the decision↔execution cycle`
- **src/alphamind/decision/portfolio_manager/input_bundle.py:139** — `ALP-462 — render_pm_header still takes float; cast at the boundary` — Incomplete Money refactor
- **src/alphamind/decision/portfolio_manager/submit_envelope/types.py:18** — `ALP-476 (story 10c): SubmitEnvelopeState is now frozen=True, slots=True per the audit's L8 mutable-dataclass remediation`
- **src/alphamind/scheduler/debug_e2e/seed.py:468** — `placeholder; the order-level direction is not a meaningful side for a strategy` (ALP-603)
- **src/alphamind/scheduler/orchestrator.py:45-48** — `layer-spanning helpers ... lifted out ... ALP-472`
- **src/alphamind/_kernel/mode.py:45** — `reserved for the operator-pinning mechanism a future story lands` (ALP-431)
- **src/alphamind/command_center/__init__.py:1** — `placeholder namespace (scheduled for ALP-128)`
- **src/alphamind/command_center/backend/__init__.py:1** — `placeholder namespace (scheduled for ALP-128)`
- **src/alphamind/commands/command_models.py:25-27** — `hoisted from execution.oms.command_models; relocated by ALP-458`
- **src/alphamind/feedback_loop/__init__.py:1** — `placeholder namespace (scheduled for ALP-131)`
- **src/alphamind/feedback_loop/discovery/__init__.py:1** — `placeholder namespace (scheduled for ALP-131)`
- **src/alphamind/feedback_loop/retrospective/__init__.py:1** — `placeholder namespace (scheduled for ALP-131)`
- **src/alphamind/feedback_loop/validation/__init__.py:1** — `placeholder namespace (scheduled for ALP-131)`
- **src/alphamind/feedback_loop/analytics/__init__.py:1** — `placeholder namespace (scheduled for ALP-131)`
- **tests/execution/corporate_actions/test_reconciliation.py:9** — `deferred to ALP-123`
- **tests/execution/corporate_actions/test_reconciliation.py:383** — `Auto-correction is deferred to ALP-123`
- **tests/execution/state_persistence/test_six_step_contract.py:1** — `End-to-end six-step snapshot-isolation contract test (ALP-119 follow-up)`

---

## Suggested early-triage targets

Items most likely to be real untracked deferrals or bug-risk patterns worth a verdict pass first:

1. **`risk_guardrails/iv_sourcing.py:12-15`** — Polygon options IV adapter "lands when the options collector is built." Is the collector built? If yes, why is the production adapter still missing? If no, what IV surface is production using?
2. **`risk_guardrails/regime_adaptation/__init__.py:9-11`** — load-order-dependent import. An innocuous refactor that reorders imports would break it silently.
3. **`risk_guardrails/state_delivery/primitives.py:97`** — `_ZONE_WARNING_THRESHOLD = 0.70` — hardcoded numeric anchor.
4. **`_kernel/ids.py:14-15`** — Symbol/OccSymbol constructor deferred to story 05a; consumers may rely on a missing validator.
5. **`scheduler/__main__.py:6-11`** — Empty task registry in daemon loop "until stories 04a / 04b register tasks." Are 04a / 04b shipped?
6. **`scheduler/debug_e2e/seed.py:217-219` + `:263-268` + `broker.py:105-107`** — Three places reference net-credit strategy variants as unsupported. Real gap if the strategist proposes a net-credit strategy.
7. **`collector/scheduler.py:128-132`** — Polygon executor-contention workaround. Verify it's still load-bearing on the current collector queue.
8. **`execution/oms/submit_engine_envelope.py:286-292`** — Phase 2 imports + `see phase2.py rationale_metadata build`. Verify phase2 module exists and matches the docstring contract; this is the pattern that has bitten validation runs before.
9. **`distillation/orchestrator.py:635, 658` + `persistence/migrations/.../vvix_percentile_nullable.py:9`** — VVIX / VX1 collector "has not yet been built." If they did land quietly, the defensive null path is dead code; if not, downstream consumers expecting populated values are silently degraded.
10. **`continuous_monitor/breach_loop/wiring.py:48-56`** — Three callback placeholders waiting on cascade dispatcher / emergency trigger / activity-log writes. Per recent commit history those landed — check whether the placeholders were ever swapped out.
11. **Five `NotImplementedError` raises in `decision/portfolio_manager/submit_envelope/dispatch.py`** — defensive but each represents an envelope variant that genuinely crashes if reached. Check which variants the system can produce.
12. **`corporate_actions/handlers/{cash_dividends,splits}.py`** — both say `currently supports equity positions only`. What happens to option legs on a div/split? Likely silent skip.
