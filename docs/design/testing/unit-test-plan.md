# Unit test plan

Unit tests protect the deterministic surfaces of the pipeline — the math in the distillation layer, the state mutations in the OMS, the rule evaluations in the guardrails, the translation logic in the broker adapter, and the estimation logic in the paper-evaluation harness. They do not evaluate LLM reasoning quality and they do not exercise end-to-end pipeline flows; those concerns are covered by the sibling Phase 3 testing items ([`llm-output-validation.md`](llm-output-validation.md), [`integration-test-plan.md`](integration-test-plan.md)).

This document defines the scope, disciplines, and preliminary unit catalogs for each testable area. Individual test cases are an implementation concern and are not enumerated here.

---

## Scope

### In scope

- **Distillation layer** — deterministic computations that transform raw external data and raw internal state into signals, indicators, composites, and rollups. Covers both [external distillation](../02-distillation-layer/external.md) (normalization, technical indicators, order flow, derivatives, short interest, expectations, volatility regime classification, correlation, anomaly detectors) and [internal distillation](../02-distillation-layer/internal.md) (beta-adjusted exposure, correlation profile, P/L attribution, capital efficiency, performance metrics, execution quality).

- **OMS processing** — command envelope parsing, [command ID derivation](../oms-command-ids.md), conflict detection, sequential command execution for each of the [five commands](../05-execution-layer/oms-commands.md), fill integration into position / bracket / cash ledger / thesis / activity log, [Phase 1 atomicity](../05-execution-layer/architecture.md) guarantees, broker submission retry and abandonment (per [state-persistence.md Phase 2 write path](../05-execution-layer/state-persistence.md)), continuous-monitor-originated command issuance.

- **Risk guardrails** — per-rule evaluation for each of the [17 rules](../06-risk-guardrails/rules-and-limits.md), [escalation zones](../06-risk-guardrails/breach-behavior.md), forced-reduction position selection, drawdown halt (daily + three-tier cumulative), [regime parameter lookup and transition interpolation](../06-risk-guardrails/regime-adaptation.md), pre-event and stress overlays, cross-constraint impact, [guardrail validation tool](../06-risk-guardrails/state-delivery.md) with cumulative tracking, cascade logic, emergency invocation triggers, guardrail state header projection per agent and per portfolio profile.

- **Broker adapter** — OMS-command-to-Alpaca-REST translation (order-class mapping, `legs` array construction for `mleg`, bracket sub-object population, PATCH replace semantics including the race-window handling), `trade_updates` websocket event translation (every event type mapped to an OMS fill-report shape), Alpaca order-ID ↔ client-order-ID mapping maintenance across PATCH replacements, disconnect recovery via `GET /v2/orders` reconciliation, account-state query wrappers. Tests stub the Alpaca REST + websocket surface with a configurable mock.

- **Paper-evaluation harness** — spread/impact estimation per instrument class, regulatory-fee rate-table lookup, fill-probability confidence tagging, `live_execution_estimate` structure construction, calibration-delta computation against paired live fills. Tests use the same Alpaca mock to produce paper-mode fills that the harness annotates; the harness logic itself is deterministic and doesn't require broker fidelity.

- **Continuous monitor** — Alpaca fill-stream consumer (event → durable-write path, dedup on retry), options bracket-stop evaluation against underlying-price ticks (trigger firing + broker submission on trigger), greeks refresh orchestration (15-minute timer, 2%-move detection, IV fetch retry/fallback), guardrail breach detection on live prices with engine-originated CLOSE authority, emergency invocation trigger evaluation.

### Out of scope (deferred elsewhere)

- **LLM agent reasoning quality.** Whether the analyst proposes good trades, whether the strategist classifies thesis status correctly, whether the PM's rejections are well-calibrated — none of these are unit-test concerns. Structural validation of LLM outputs (schema conformance, reference ID typing, required field presence) is the validator's responsibility and is unit-tested in [`llm-output-validation.md §Unit test plan`](llm-output-validation.md#unit-test-plan). Calibration and quality are evaluated through the Phase 4 feedback loop.

- **End-to-end pipeline flows.** Analyst → pre-processor → strategist → PM → engine sequencing, cumulative-impact annotation across real multi-agent outputs, cross-agent envelope ordering — deferred to the integration test plan. Individual stages are unit-tested with stubbed upstream inputs.

- **Broker contract compliance against the live Alpaca API.** Unit tests use a configurable Alpaca mock. Verifying that the mock's responses match the real Alpaca API's actual behavior is an integration concern handled via recorded-response fixtures in [`integration-test-plan.md`](integration-test-plan.md).

- **Thesis dependency mapping.** Distillation units that consume thesis narrative text (catalyst overlap matrix, narrative dependency clustering) shade into qualitative interpretation. These are validated through integration walkthroughs rather than unit tests; their structural outputs are schema-checked but the semantic grouping is not unit-asserted.

- **Guardrail scenario walkthroughs.** [scenario-tests.md](../06-risk-guardrails/scenario-tests.md) validates the *design* across multi-step portfolio-state narratives (regime transitions, cumulative proposal breaches, cascade scenarios). Those scenarios inform which unit-level cases matter but are themselves design-validation artifacts, not unit tests.

---

## Testing disciplines

Five principles applied uniformly across all four test areas.

### Structure, not behavior

Unit tests verify deterministic computations and state transitions. "Does this function produce the right output for this input" is a unit-test question. "Does the analyst propose good trades" is a feedback-loop question. The boundary is worth defending because structural tests are stable across prompt iteration and integration tests are not.

### Boundary coverage is load-bearing

The system is defined by its thresholds, not its interiors. Every named limit — position size cap, sector concentration limit, drawdown tier boundary, escalation zone threshold, moneyness band, DTE band, ADV bucket, regime parameter — is exercised at the boundary value, one minimal unit under, and one minimal unit over. Interior coverage is cheap but low-value; edge coverage is where bugs live and where the design decisions show through.

Boundary coverage applies recursively. A rule with a 25% sector limit is tested at 24.99%, 25.00%, and 25.01%, but it is also tested across regime transitions (normal 25% → elevated 20% → crisis 15%), across escalation zones (70% warning, 85% critical, 95% block), and across profile feature flags (omitted for the primary portfolio, active for full-system). The boundaries multiply; the test design names which axes matter per unit.

### Deterministic replay for stochastic units

Every unit that draws randomness — greek-derived price IV perturbation, any harness sub-model that uses a random component, stochastic test generators — accepts an explicit RNG source as a constructor or call parameter. Tests pass a seeded RNG; the same seed produces the same outcome. A stochastic test without a fixed seed is not a test, it is a monitor, and it belongs in the observability layer.

The heavily stochastic units the previous simulator had (fill probability, borrow rate, locate delay, forced buy-in) do not exist in the new architecture — Alpaca produces the fills and the harness's estimation models are deterministic given their inputs. Seeded RNG remains relevant primarily for IV-perturbation sensitivity analysis and for generating test fixtures from randomized inputs.

### Atomicity is a named test property

For state-mutation units with atomicity guarantees, the "does partial failure leave the portfolio in a valid state" question is a first-class test concern distinct from happy-path output. The specific units where this matters:

- **Phase 1 fill integration.** All fills committed or none; fill record processing_status marker coupled to state mutations; fill arriving mid-transaction is ignored until the next invocation's collect.
- **Bracket lifecycle on entry fill.** Protective legs submitted together (for OMS-managed brackets) or not at all; entry cancellation cancels both protective legs; stop trigger cancels the target and vice versa.
- **Forced-reduction cascade.** Primary breach response plus secondary-breach check; both the close command and the secondary-breach alert share a `cascade_id`; rollback of the close does not orphan the alert.
- **Gateway submission retry with abandonment.** Within-invocation retry succeeds (command committed) or retry window exhausts (full transaction rolled back, `command_abandoned` activity log entry written, abandoned command surfaced to the originating agent at the next invocation).
- **Capital reservation cascade across sequential commands.** Command N validates against cumulative state from commands 1..N-1; if command N fails, commands 1..N-1 remain committed and command N produces a rejection without affecting state.

Each of the above gets happy-path, partial-failure-with-rollback, and mid-transaction-fault tests.

### Shared fixtures, not per-cluster replicas

Portfolio state, market data bars, regime parameters, and activity log entries are consumed by multiple test clusters. One canonical fixture catalog exists under the test tree; all test files read from it. A per-cluster fixture drift is the largest avoidable maintenance cost in a system with 140+ testable units. The inventory is named in the [Shared fixture inventory](#shared-fixture-inventory) section below.

---

## Distillation layer

The distillation layer is dominated by pure computations — input vector → output scalar or structured output, no side effects. Testing pattern is parameter-space coverage with particular emphasis on degenerate inputs, startup conditions, and threshold crossings.

### Test concerns

**Degenerate inputs.** Zero-variance series (flat price, flat volume), zero-denominator cases (division by ATR where ATR ≈ 0, division by trailing average where volume is zero), single-element windows, missing baseline history. The distillation layer consumes data from the data layer and sometimes runs before the data layer has accumulated enough history; each rolling-window computation needs an explicit startup-period test.

**Threshold crossings.** Anomaly detectors fire at specific z-scores; regime classification crosses specific VIX and term-structure thresholds; correlation breakdown detection fires at a specific rate-of-change. Each threshold is tested at value, one ε under, and one ε over. For regime classification specifically, the boundary matters because regime transitions trigger immediate tightening in the guardrails layer — a drift in regime-classification behavior cascades.

**Persistent rollup state.** A small number of units (volatility regime classification, correlation regime stability, expectations-vs-reality scorecard) carry state across invocations. Tests for these units require multi-invocation fixtures that simulate a sequence of prior states; single-invocation fixtures do not exercise the rollup logic.

**Conservative-buffer and tolerance applications.** Several units apply explicit buffers: the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md)'s +10% delta buffer, the greek-derived price calculator's 5% uncertainty buffer, the observed-options-spread 1.2× floor. Tests assert the buffer is applied in the expected direction and magnitude, not just that the output is "close."

### Stub boundary

- Historical baseline series per ticker: ATR, 20-day realized volatility, rolling correlation matrix, rolling short-volume percentiles.
- Sector classifications, beta estimates per ticker, sector-ETF return series.
- Macro consensus estimates and actuals feed.
- Trading calendar for time-alignment and DST handling.

### Preliminary unit catalog

Preliminary. Expect the list to move as implementation begins; the axes (category grouping, boundary conditions, stub dependencies) are more stable than individual entries.

**External distillation** (~27 units):

| Group | Units |
|---|---|
| Normalization | unit standardization (price / ATR-relative / dollar), time alignment (DST, weekend gaps), per-ticker volatility normalization, extended-hours confidence discounting, macro surprise framing |
| Technical indicators | multi-timeframe RSI + divergence, Bollinger bands, ATR and volatility regime, moving-average slopes and crossovers, ADX, volume profile and point-of-control |
| Order flow | net-dollar-flow aggregation, liquidity score, dark-pool classification, extended-hours flow confirmation |
| Derivatives | options flow classification, ETF-vs-single-name IV divergence, index-hedging vs. sector-conviction classifier |
| Short interest | triangulation across fast and slow feeds, directional-vs-mechanical classifier |
| Expectations | expectation-gap tracking, revision-price lag detector |
| Volatility regime | multi-input regime detector, regime-transition flagging |
| Correlation | rolling pairwise matrix, sector rotation velocity, lead-lag timing, correlation stability and breakdown |
| Anomaly triggers | volume z-score, ATR-relative price move, news-price divergence, put-call skew spike, funding-stress composite |

**Internal distillation** (~18 units):

| Group | Units |
|---|---|
| Exposure | beta-weighted exposure aggregator (sector and directional) |
| Correlation profile | pairwise correlation matrix generator, portfolio implied correlation, correlation clustering, regime-change detector |
| P/L attribution | market component, sector component, alpha residual, attribution ratio, portfolio-level alpha |
| Thesis concentration | effective independent thesis count (consumes thesis dependency mapping, which is scope-deferred per above) |
| Capital efficiency | average capital utilization, capital turnover, cash drag estimate |
| Execution efficiency | time-to-deploy, position sizing efficiency, thesis-to-execution conversion rate |
| Performance metrics | Sharpe, Sortino, expectancy per trade, profit factor |
| Execution quality | slippage statistics, fill-rate analyzer, volume participation rate |

---

## OMS processing

OMS processing splits into two flavors: pure transformations (ID derivation, envelope parsing, conflict detection) and state mutations (command executors, fill integrators, Phase 1 transaction). The state-mutation units are where atomicity tests concentrate.

### Test concerns

**Deterministic command ID assembly.** [oms-command-ids.md](../oms-command-ids.md) defines the four-part ID derivation `{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}` and the parallel `MON.{session}.{trigger}.{ordinal}` scheme for engine-originated commands. Tests verify the ID is a deterministic function of its inputs (no clock, no RNG), the envelope_id prefixes are bijective with source proposals, `attempt_seq` increments on post-rejection modifications, and duplicate IDs raise a structural error rather than silently dedup.

**Envelope-to-command parsing.** Each of the five command types has distinct required fields and parameter shapes. Tests cover malformed envelopes (missing fields, invalid enum values, conflicting fields), cross-command conflicts within a batch (CLOSE + ADD on the same position), and the boundary between PM-originated envelopes and engine-originated envelopes (different schema variants, different ID schemes).

**Sequential command execution.** Commands within an invocation are processed in envelope order; each command's guardrail validation accounts for state changes from prior commands in the same batch. Tests exercise the capital reservation cascade (N OPENs each reserving capital; command N sees cumulative state), CLOSE-then-OPEN sequencing (capital released before next reservation), and conflict detection when a later command references an earlier command's output.

**Phase 1 atomicity.** Fill integration into position / bracket / cash / thesis / activity log runs as a single transaction. Tests cover happy path (all fills processed), mid-loop failure (transaction rolled back, all fills remain unprocessed), concurrent fill arrival from the monitor (fills arriving after the initial query are ignored until the next invocation), and OMS restart mid-Phase 1 (state recovery on next startup).

**Broker submission retry and abandonment.** [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md) defines a within-invocation retry window followed by full rollback and `command_abandoned` activity log entry. Tests cover retry succeeding on first attempt, retry succeeding after N transient failures, retry window exhausting (rollback executes, abandoned command surfaced to the originating agent at the next invocation). The rollback is the important property: if retry exhausts, position / thesis / bracket / orders must all revert and the capital reservation must be released.

**Engine-originated command issuance.** Continuous-monitor-originated protective CLOSE commands use the `MON.` ID scheme, carry a guardrail trigger reference, and skip the PM evaluation path. Tests verify the monitor's session-initialization sequence, breach detection and trigger recording, secondary-breach check before issuance, and cooldown enforcement on emergency invocation triggers.

### Stub boundary

- Alpaca REST + websocket mock with configurable responses (acknowledge, transient failure, persistent failure, race-condition fill arrival during PATCH).
- Deterministic sources for UUID / session ID / invocation ID generation.
- Clock source (for monitor cooldowns, retry timing, order expiration).
- Activity log writer (in-memory for assertion).
- Portfolio state fixture builder (see shared fixtures).

### Preliminary unit catalog

Preliminary. ~30 units grouped by processing phase.

| Group | Units |
|---|---|
| Pure transformations | command ID derivation (PM), command ID derivation (monitor), envelope parsing and validation, command conflict detection, fill deduplication, fill validation and quarantine |
| Command validation | OPEN guardrail validation, CLOSE guardrail validation (protective, engine-originated), command ID deduplication, sequential state projection |
| Command execution | OPEN executor, CLOSE executor, ADJUST executor, ADD executor, CANCEL executor |
| Fill integration | fill collection from gateway, fill → position update, fill → bracket state transition, fill → cash ledger update, fill → activity log entry generation, thesis resolution on close |
| Phase 1 transaction | atomic write wrapper, snapshot reader (post-Phase 1 read model) |
| Phase 2 processing | command sequencer, gateway submission manager, per-command rollback |
| Continuous monitor | session initializer, breach detector, secondary-breach validator, protective CLOSE issuer, emergency invocation trigger, invocation ID assigner, greeks refresh orchestrator (scheduled timer + 2%-move trigger + IV fetch + Black-Scholes recompute + position-record write-through), greeks refresh failure handler (retry window, stale-buffer widening, escalation to emergency invocation on ambiguous breach) |

---

## Risk guardrails

Guardrails have the largest testable surface by rule count: 17 rules × 4 regimes × 3 escalation zones × overlay flags × portfolio profiles. Exhaustive enumeration of all combinations is infeasible and would not pay for itself. The approach is per-rule evaluation tested at named boundaries, cross-rule interaction tests for the concerns that genuinely couple (secondary breach, forced reduction selection, cascade), and snapshot tests for guardrail state header projection.

### Test concerns

**Per-rule evaluation.** Each of the 17 rules is a function from (portfolio state, regime parameters) to (current value, limit, zone, headroom). Tests cover: at-limit, one ε over, one ε under; each of the four regimes; each of the three escalation zones (plus the drawdown-specific overrides 60/80/90 and 50/70/85); presence and absence of pre-event and stress overlays; feature-flag omission under the primary portfolio profile. The rules themselves don't interact at this level — the interaction tests are separate.

**Validation tool cumulative tracking.** The validation tool is not a pure function; it carries cumulative state across calls within the same agent's turn (proposal 1 validates against live state; proposal 2 validates against live + proposal 1 projected impact; and so on). Tests cover single-call, cumulative cascade, reset at agent boundary, and feature-flag first-check rejection. The tool's cumulative-tracking wrapper is what's tested here; the underlying delta computation, conservative buffer, and per-rule projection live in the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) and have their own primitive-level tests.

**Regime transition interpolation.** Tightening is immediate on transition; loosening is linear over three invocations. Tests cover the transition-tick boundary (invocation zero vs. invocation one after transition), intermediate tightening during a loosening run (overrides, resets counter), and compounding overlays (pre-event tightening during elevated regime).

**Forced reduction position selection.** For rules classified as automatic reduction, the engine selects which position(s) to close based on deterministic criteria (unrealized loss %, ADV, sector balance). Tests cover tie-breaking, multi-rule breach on a single position (higher severity wins), and the secondary-breach check (closing short in a sector breach would create a net-long breach; the check defers or proposes alternative).

**Cascade linking.** Margin calls and their downstream forced reductions share a `cascade_id`. Tests verify the ID is assigned once at the cascade root and propagated to all follow-on activity log entries.

**Emergency invocation triggers.** Four objective conditions trigger an emergency invocation (regime jump skipping a level, 3+ deferred rules breaching simultaneously, drawdown velocity above threshold, broker margin call). Tests cover each trigger condition, cooldown enforcement, coalescing of multiple simultaneous triggers, and the case where an emergency fires shortly before a scheduled invocation.

**Guardrail state header projection.** Analyst / strategist / PM guardrail state headers differ in detail level, feature-flag conditional inclusion, and halt-mode modification. Tests use snapshot assertions: given a specific portfolio state, regime, and mode flag, the serialized header matches a golden fixture. Snapshot tests specifically cover the feature-flag omission cases (options section absent when options are disabled for the primary portfolio), the halt-mode header and action-restriction language, and the cross-constraint summary in the PM header.

### Stub boundary

- Portfolio state fixture with positions at configurable distances from each rule's limit.
- Regime state (current regime, invocations since last transition, active overlays).
- Event calendar stub (pre-event overlay trigger).
- Broker margin interface mock (for margin-call cascade tests).
- Frozen clock (drawdown velocity, emergency cooldown).
- Market data stub (delta / IV / underlying price for delta-adjusted exposure, greeks, options-specific rules).

### Preliminary unit catalog

Preliminary. ~40 units organized by concern rather than by rule (per-rule evaluation is one "unit" with 17 parameterized cases, not 17 separate units).

| Group | Units |
|---|---|
| Per-rule evaluation | one parameterized unit covering all 17 rules (per-position size, position-level max loss, sector concentration, net long, net short, gross exposure, daily drawdown, cumulative drawdown, options delta, portfolio theta, portfolio vega, correlation, total short exposure, single short max size, borrow cost budget, min cash reserve) |
| Breach behavior | escalation zone evaluator, forced-reduction classifier, position-selection logic, drawdown halt activator, cumulative-drawdown tier transition, daily reset |
| Regime adaptation | parameter lookup per regime, transition interpolation (linear over 3 invocations), pre-event overlay applier, stress overlay applier, tightening-creates-immediate-breach detection |
| Delta-adjusted exposure | delta computation (equity, option, multi-leg strategy), +10% conservative buffer, greeks integration |
| Validation tool | single-call contract, cumulative tracking across proposals, agent-boundary reset, feature-flag first check, failure guidance generation |
| Cross-constraint | pre-processor annotation generator, PM cross-constraint summary, secondary-breach simulator |
| Cascade | margin-call response, cascade_id assignment and propagation, primary + secondary breach co-occurrence |
| Emergency invocation | trigger condition evaluator (one parameterized unit covering the four triggers), cooldown enforcement, trigger coalescing, context-modification injection |
| Context package | analyst package serializer, strategist package serializer, PM package serializer, halt-mode modification applier, feature-flag omission logic, profile-specific binding |

---

## Broker adapter

The adapter's units are pure translation: OMS commands ↔ Alpaca REST bodies, Alpaca `trade_updates` events ↔ OMS fill reports. Units are deterministic given their inputs; stubs are a scripted Alpaca mock.

### Test concerns

**Order-class construction.** Bracket orders produce well-formed `take_profit` + `stop_loss` sub-objects; `mleg` orders produce `legs` arrays with ratios in simplified form (GCD = 1); `oco` and `oto` produce the correct same-side and single-child structures. Each order class has a round-trip test: OMS command → adapter request body → parsed back → matches the OMS command semantically.

**PATCH replace race handling.** The adapter's PATCH path has three outcomes: `200` success → new Alpaca ID returned, mapping updated; `200` success → `replace_rejected` event arrives on `trade_updates` before the new ID is registered (original filled first); REST error → exception propagated to the OMS as a modification rejection. All three are exercised; the race-condition one asserts that the original fill takes precedence and the replacement is discarded.

**`trade_updates` event translation.** Every event type listed in [broker-adapter.md § Fill stream](../05-execution-layer/broker-adapter.md) is translated to the corresponding OMS fill-report shape. Tests parameterize over the event matrix. Timestamps are preserved from the Alpaca event; Alpaca order IDs are mapped to OMS `client_order_id` via the adapter's mapping table.

**Disconnect recovery.** On websocket disconnect, the adapter re-queries `GET /v2/orders` with a `since` parameter and reconciles against the local fill-record table. Tests cover the three cases: no events missed (idempotent no-op), N events missed during the gap (recovered and written with `processing_status = unprocessed`), stateful divergence where local believes an order is still pending but Alpaca reports it terminal (log delta, trust Alpaca).

**Account-state query wrappers.** Thin wrappers over `GET /v2/account`, `GET /v2/positions`, `GET /v2/orders`, `GET /v2/account/activities`, `GET /v2/assets/{symbol}`, `GET /v2/calendar`, `GET /v2/clock`. Each has a happy-path test plus transient-error retry plus malformed-response rejection.

### Stub boundary

- Alpaca REST mock with scripted responses per request (acknowledge, transient failure, persistent failure, race-condition fill arriving before PATCH completes).
- Alpaca websocket mock with scripted event sequences (`new` → `partial_fill` → `fill`, bracket-complete sequences, `replace_rejected`, disconnect-and-reconnect).
- Deterministic clock and session state.

### Preliminary unit catalog

Preliminary. ~15 units.

| Group | Units |
|---|---|
| Order construction | simple order builder, bracket builder, OCO builder, OTO builder, mleg builder, `client_order_id` assigner, extended-hours guard |
| Modification | PATCH request builder, PATCH race handler, Alpaca-ID mapping updater |
| Fill stream | event-type dispatcher, fill-report constructor, bracket-child event handler, `replace_rejected` handler |
| Recovery | `GET /v2/orders` reconciler, local-vs-Alpaca delta detector |
| Account queries | account / positions / orders / activities / assets / calendar / clock wrappers |

---

## Paper-evaluation harness

The harness is pure estimation: given a paper fill and context (order size, ADV, spread, regime), produce a `live_execution_estimate` structure. Deterministic given inputs.

### Test concerns

**Spread + impact estimation.** The slippage formula `(spread/2) + impact_coefficient * spread * sqrt(fill_shares / ADV)` is verified at multiple boundaries (zero-size-fraction, participation caps). Impact coefficients differ per order type (market 0.5, limit 0.25, stop 0.75); each is tested.

**Fee rate-table lookup.** Per-instrument-class, per-side (buy/sell) fee computation. Equity sells attract TAF + CAT + SEC; equity buys attract CAT only; options attract CAT + OCC + ORF plus SEC on sells; crypto has no regulatory fees. Tests parameterize over the matrix.

**Confidence tagging.** Limit touches with bar-extreme = limit and low bar volume tag `confidence: low`; instruments where the fill size would exceed a realistic volume-participation share tag `confidence: medium`. Tests cover the boundaries of each tag's triggering condition.

**Calibration delta computation.** Given a paired (paper-fill, live-fill) pair, the harness computes the delta between estimated live-adjusted fill and actual live fill. Tests verify the sign conventions (paper overestimating vs. underestimating) and the aggregation across multiple pairs feeding into coefficient adjustment.

### Stub boundary

- Alpaca mock producing paper fills (reuses the broker-adapter mock).
- Market data stub providing ADV, spread observations, and regime context per ticker.
- Fee rate table (static, loaded from config) — tests exercise the lookup, not the rates themselves.

### Preliminary unit catalog

Preliminary. ~10 units.

| Group | Units |
|---|---|
| Spread + impact | observed-spread buffer applier, spread estimator (price/ADV/volatility fallback), impact calculator, total-drag composer |
| Fees | rate-table lookup, per-fill fee allocator, daily aggregation vs. Alpaca activities reconciler |
| Confidence | bar-extreme touch classifier, participation-share classifier, confidence tag assigner |
| Calibration | paired-fill comparator, coefficient adjustment proposer |

---

## Continuous monitor

The monitor runs five responsibilities. Each has testable surface; together they form a stateful event loop that is tested both per-responsibility and in short-sequence scenarios.

### Test concerns

**Fill-stream consumption path.** Event arrives on websocket → durable write to fill buffer → dedup on retry (same composite key). Tests cover the happy path, retry-arrives-before-original-persists (both dedupe to one record), and reconnect-replay (same events delivered twice, idempotent).

**Options bracket-stop evaluation.** Underlying-price tick arrives → stop condition evaluated against each open options position → on trigger, closing order submitted via the broker adapter. Tests cover the trigger firing at-level, one tick under, one tick over; the strategy-position case (multi-leg close); and the race where the PM closes the position via a normal command in the same invocation window.

**Greeks refresh orchestration.** Two trigger paths — 15-minute scheduled timer and 2% cumulative underlying move — tested independently and together. IV fetch failure path: brief retry, then `greeks_refresh_failed` logged + uncertainty buffer widened; if that leaves a breach determination ambiguous, escalate to emergency invocation.

**Guardrail breach detection on live prices.** Periodic portfolio revaluation against live prices; on breach, the per-rule classification in [breach-behavior.md](../06-risk-guardrails/breach-behavior.md) determines whether to issue an engine-originated CLOSE. Tests cover the three-way split (no breach, breach with immediate action, breach with deferral), the secondary-breach pre-check, and the `cascade_id` propagation on margin-cascade scenarios.

**Emergency invocation trigger.** Trigger condition evaluator for the four trigger types (regime jump, multi-rule simultaneous breach, drawdown velocity, margin call), cooldown enforcement, trigger coalescing. Tests cover each trigger independently and the coalescing behavior when two triggers fire within the cooldown window.

### Stub boundary

- Websocket event feeder (scripted sequences of `trade_updates` events at prescribed timestamps).
- Underlying-price tick feeder (scripted sequences of equity ticks for options-stop and greeks-refresh triggers).
- Clock source.
- Data-pipeline mock providing IV surface snapshots.
- Portfolio state fixture (for guardrail revaluation).

### Preliminary unit catalog

Preliminary. ~15 units.

| Group | Units |
|---|---|
| Fill stream | event writer, dedup enforcer, reconnect-replay reconciler |
| Options stops | underlying-tick evaluator, trigger firer, mleg close submitter |
| Greeks refresh | scheduled-timer tick, 2%-move detector, IV-fetch + retry + fallback, refresh writer |
| Guardrail breach | live-price revaluator, per-rule classifier, protective CLOSE issuer, secondary-breach pre-checker, cascade_id propagator |
| Emergency invocation | trigger condition evaluator (four triggers), cooldown enforcer, trigger coalescer |

---

## Shared fixture inventory

Fixtures that multiple clusters consume. One catalog, one source of truth. Fixtures are constructed by builders with sensible defaults and overridable fields — tests declare only the fields they care about.

### Portfolio state builder

Consumed by: guardrails (every rule), OMS command executors, guardrail state header projection, P/L computation, exposure aggregation.

Builder API accepts positions (with direction, size, cost basis, current greeks for options), sector classifications, cash balance, pending orders, active theses with components, activity log entries, drawdown state (daily + cumulative + HWM), regime state. Defaults produce a portfolio at "normal regime, healthy state, no breaches."

Named presets exist for recurring scenarios: "primary $1,500 at 60% deployed," "full-system $100K with 8 positions," "tech sector at WARNING zone," "drawdown tier 1 active," "post-margin-call in-progress cascade."

### Market data fixture

Consumed by: fill modeling (every unit), distillation anomaly detectors, guardrail rules that depend on current price (position max loss, sector concentration at current market value).

Fixture provides bar OHLCV per ticker per timeframe per date range, trading calendar, DST-aware timestamps, spread data. Defaults to a quiet market with predictable ATR; named overlays exist for "earnings-day gap," "intraday halt and resume," "flash-crash tick sequence."

### Regime state fixture

Consumed by: guardrails (parameter lookup and transition interpolation), distillation (regime classification's own input baseline), guardrail state header projection (regime label in every header).

Fixture provides current regime, invocations since last transition, active overlays (pre-event flags, stress flags), historical regime sequence for loosening-interpolation tests.

### Activity log fixture

Consumed by: OMS fill integration (writes entries), guardrails state header (reads recent engine-originated actions), fill-modeling continuous monitor (writes monitor session events).

Fixture is an append-only in-memory log with filtering by invocation ID, cascade ID, event type. Assertion helpers exist for "exactly N entries of type X in invocation Y," "cascade_id C has entries in this sequence," "no entries of type Z were written."

### RNG sources

Consumed by: fill modeling (every stochastic unit), distillation rollup units with sampling.

Convention: each stochastic unit receives its own seeded RNG at construction; tests assert on exact outputs given exact seeds. A utility produces deterministic seeds from test names for reproducibility without per-test seed bookkeeping.

### Alpaca mock

Consumed by: OMS processing (every command submission path), broker adapter (every REST/websocket unit), paper-evaluation harness tests that need paper-mode fills, continuous monitor tests that need `trade_updates` sequences.

Mock exposes the Alpaca REST endpoints and the `trade_updates` websocket with scripted responses per submitted order (acknowledge, transient failure with N retry attempts before success, persistent failure triggering abandonment, out-of-order fill report delivery, PATCH race conditions). State inspection API returns the sequence of submitted orders and emitted events for assertion.

---

## Resolved design questions

Design questions the test catalog surfaced during scoping, now closed:

- Greeks refresh ownership — the continuous monitor owns refresh orchestration (scheduled 15-min per underlying + 2%-underlying-move triggered). See [architecture.md §4d](../05-execution-layer/architecture.md). Testable units are in the continuous monitor section above.

- Multi-position forced-closure handling — AlphaMind's ETB-only universe and Alpaca's no-HTB-shorting constraint mean lender-recall forced buy-ins don't occur in practice; the closest analogue is Alpaca's ETB→HTB overnight auto-closure on a previously-ETB name. Synchronized multi-position closures are validated through scripted scenarios in [scenario-tests.md A11](../06-risk-guardrails/scenario-tests.md) rather than stochastic unit tests.

- Corporate action handling of active bracket parameters — any corporate action (split, reverse split, stock or cash dividend, merger, acquisition, spin-off) cancels the bracket and flags the position; the strategist produces a fresh `adjust-bracket` or `close` via the normal command path. See [orders-and-brackets.md § Corporate action handling](../05-execution-layer/orders-and-brackets.md#corporate-action-handling) and [strategist.md § Corporate-action-pending positions](../04-decision-layer/strategist.md#corporate-action-pending-positions). The position-level integration mechanics — per-action quantity, cost basis, ticker, status mutations, cash ledger movements, spin-off child creation, Phase 1 chronological merge with fills, idempotency on retry, and the activity log catalog additions — are specified in [corporate-actions.md](../05-execution-layer/corporate-actions.md). Unit tests cover: the bracket cancellation + flagging path against each per-action-type fixture, the spin-off child position creation path (orphan thesis_id/bracket_id with origin reference), the chronological merge of unprocessed fills and unprocessed CA activities in Phase 1, the `processed_corporate_actions` ledger dedup behavior on retry, and the reconciliation-against-Alpaca step at end of Phase 1.

---

## Cross-references

- Phase 3 testing items: [project-tracker.md](../../project-tracker.md)
- Guardrail scenario walkthroughs (design validation): [scenario-tests.md](../06-risk-guardrails/scenario-tests.md)
- OMS command ID discipline: [oms-command-ids.md](../oms-command-ids.md)
- Broker submission retry and abandonment: [state-persistence.md § Phase 2 write path](../05-execution-layer/state-persistence.md)
- Two-phase invocation model: [architecture.md](../05-execution-layer/architecture.md)
- Broker adapter (Alpaca): [broker-adapter.md](../05-execution-layer/broker-adapter.md)
- Paper-evaluation harness: [paper-evaluation-harness.md](../05-execution-layer/paper-evaluation-harness.md)
- Guardrail rules and limits: [rules-and-limits.md](../06-risk-guardrails/rules-and-limits.md)
- Regime adaptation: [regime-adaptation.md](../06-risk-guardrails/regime-adaptation.md)
- Breach behavior: [breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
- Validation tool and guardrail state headers: [state-delivery.md](../06-risk-guardrails/state-delivery.md)
- External distillation: [external.md](../02-distillation-layer/external.md)
- Internal distillation: [internal.md](../02-distillation-layer/internal.md)
- Orders and brackets: [orders-and-brackets.md](../05-execution-layer/orders-and-brackets.md)
- LLM agent failure handling (informs out-of-scope boundary): [llm-agent-failure-handling.md](../llm-agent-failure-handling.md)
