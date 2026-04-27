# Integration test plan

Integration tests verify components cooperate across system boundaries under realistic conditions. They exercise the full pipeline runtime, real OMS transaction model, real guardrail evaluation, and real database. Only *external* boundaries — LLM provider, external data APIs, Alpaca — are mocked. The questions are cross-component: does a malformed agent output actually result in an aborted invocation with no commands submitted? Does a broker submission abandonment actually surface to the originating agent at the next invocation? Does a regime transition actually propagate guardrail-state-header parameters through the validation tool and into strategist remedy flags?

Individual computation correctness is a unit-test concern ([unit-test-plan.md](unit-test-plan.md)). LLM reasoning quality is a Phase 4 feedback-loop concern. Design-validation walkthroughs over multi-step portfolio narratives live in [scenario-tests.md](../06-risk-guardrails/scenario-tests.md).

---

## Scope

### In scope

- **End-to-end pipeline flows** — data collection through command submission with scripted LLM fixtures and data-layer payloads. Covers feature-flag variants (primary vs. full-system portfolio), regime transitions within an invocation, halt-mode propagation across invocations, emergency invocation triggering, PM rejection-and-retry, abandoned-command surfacing.

- **Broker adapter contract compliance** — adapter's end-to-end behavior against [broker-adapter.md](../05-execution-layer/broker-adapter.md) via the Alpaca mock. Scenarios cover every order class (simple, bracket, OCO, OTO, mleg), every `trade_updates` event type, PATCH replace race, fee reconciliation via `account/activities`, disconnect recovery. Also exercises broker-unavailable failure paths in both pipeline phases.

- **Two-process interaction via shared database** — pipeline ↔ continuous monitor round-trips. Order handoff, fill buffering, engine-originated CLOSE flow, greeks refresh write-through, emergency invocation trigger, cross-process activity log ordering, monitor restart recovery.

- **Failure-mode propagation** — one scripted fixture per documented failure mode at the LLM invocation boundary and the Alpaca mock, named to match failure-taxonomy labels in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md) and the adapter's error surface in [broker-adapter.md](../05-execution-layer/broker-adapter.md). Asserts cross-component propagation (abort, diagnostic persistence, OMS state invariants, monitor independence).

### Out of scope (deferred elsewhere)

- **Individual computation correctness.** Distillation math, per-rule guardrail evaluation, per-command OMS executors, stochastic fill units — covered by [unit-test-plan.md](unit-test-plan.md) at the unit level with stubbed upstream inputs.

- **LLM reasoning quality.** Whether scripted fixtures represent *good* reasoning is not the question. Fixtures represent *contractually valid* outputs; calibration and quality are evaluated through the Phase 4 feedback loop.

- **LLM output validation detection logic.** Schema validators, reference-ID checkers, and output parsers are unit-tested in [`llm-output-validation.md`](llm-output-validation.md) against synthetic malformed payloads. Integration runs real validators against scripted malformed fixtures to exercise the propagation path, not re-test detection.

- **Design-validation walkthroughs.** [scenario-tests.md](../06-risk-guardrails/scenario-tests.md) validates the *design* across multi-step portfolio narratives. Those scenarios inform which integration scenarios matter but are design-validation artifacts.

- **Backtesting / historical replay.** Phase 4 concern (paper-to-live transition criteria, asset universe validation). Integration covers pipeline correctness under scripted conditions; backtesting covers strategy validation against real history.

- **Live broker adapter testing.** Deferred until a live adapter exists. The gateway contract-test suite is the acceptance criterion.

---

## Testing disciplines

Five principles applied uniformly.

### One fault-injection seam per external boundary

The LLM invocation boundary is the single seam for LLM failures. The gateway mock is the single seam for gateway failures. Scripted misbehavior is never layered through the stack. A single seam keeps happy-path fixtures clean and makes failure-mode coverage auditable against the failure-taxonomy spec by grep.

### Fixture name equals taxonomy label

Every scripted-misbehavior fixture is named after the failure mode it represents (`llm_timeout`, `llm_malformed_output_recover`, `llm_context_overflow`, `gateway_unavailable_phase2`, etc.). When the spec adds, removes, or renames a failure mode, the corresponding fixture must change — drift is detectable mechanically.

### Realistic operational conditions

Only external boundaries are mocked. OMS transaction path, guardrail evaluation, pre-processor annotation, and the database are real (SQLite in-memory or tmpfile per test). In-process stubs of domain code defeat the point — they test a pipeline that doesn't match production.

### Cross-boundary state assertions

Tests assert on what ended up in the database — positions, cash, thesis records, activity log entries, command envelopes, `command_abandoned` events, `forced_buy_in` flags, cascade_id linkage — not method call counts or intermediate in-memory state. Where an assertion needs to observe transient runtime state, capture it through a test-only observer that reads the DB, not direct state access.

### Determinism over speed

Frozen clocks, seeded RNGs, deterministic IDs, scripted fixture responses. APScheduler configured against the frozen clock. A flaky integration test is worse than no test. Slow but deterministic beats fast and flaky.

---

## End-to-end pipeline flows

Happy path plus state-carrying variants requiring whole-pipeline orchestration: feature-flag projection, regime transitions, halt-mode cascade, emergency invocation, multi-invocation continuity.

### Test concerns

**Full-pipeline happy path.** One scripted invocation from data collection through command submission. Assertions on terminal DB state: orders submitted with correct command IDs, positions updated, cash debited, thesis records created, activity log entries written, pre-processor annotations reflected in PM reasoning (visible in the PM envelope's rationale field).

**Feature-flag projection.** The same scripted scenario drives both primary ($1,500, options/shorts disabled, 2 active sectors, max 3 positions) and full-system ($100K, all features active). Guardrail-state-header projection, validation-tool behavior, and OMS command validation must differ correctly across profiles without duplicating test logic.

**Regime transition within an invocation.** Distillation output reclassifies the regime. Guardrail state headers flip. Validation tool loads the new regime's parameter set. Strategist guardrail state header surfaces `regime_transition` remedy flags on breaching positions. PM cross-constraint summary reflects tightened limits. Remediation commands flow to the OMS with correct attribution. Single invocation, full cascade.

**Halt-mode propagation across invocations.** Invocation N's monitor detects a daily drawdown breach → halt flag persisted → N+1's analyst receives watchlist-only context, strategist receives `defensive_posture` context, PM receives restricted action space (CLOSE/ADJUST/CANCEL only). Terminal state: no OPEN/ADD for the rest of the session; next-day open resets halt.

**Emergency invocation mid-cadence.** Monitor writes an emergency trigger record. Scheduler observes and starts a pipeline invocation outside the rolling cadence. Emergency context header propagates through every agent. Pipeline runs to completion; next rolling invocation scheduled from the emergency timestamp.

**PM rejection and retry within same invocation.** PM sizing modification fails T3. Synchronous rejection returned. PM adjusts the envelope and re-submits. Activity log contains both rejection and subsequent successful submission with correct `attempt_seq` increment.

**Abandoned command surfacing across invocations.** Gateway submission fails in invocation N after retry exhaustion. Atomic rollback (position, thesis, bracket records all removed). `command_abandoned` activity log entry written. N+1's guardrail state headers surface the abandoned command to its originating agent — failed OPENs to analyst, failed ADD/ADJUST/CLOSE/CANCEL to strategist — which either re-proposes against fresh data or lets the action lapse.

**Multi-invocation continuity.** Run invocation N, then N+1 against the DB state N produced. Thesis aging, `prior_status` transitions, activity log context, cumulative-drawdown HWM tracking — all behave correctly across invocation boundaries without test reset.

### Stub boundary

- **LLM invocation boundary** returns scripted responses keyed by (agent name, invocation ID, attempt sequence). Happy-path fixtures fall through; scripted-misbehavior variants short-circuit with the failure shape.
- **External data APIs** return canned payloads keyed by scenario, with freshness metadata for fresh-or-abort testing.
- **Alpaca** mocked via the same REST + websocket mock used in unit tests, scaled up with multi-order sequences and realistic event timing. Recorded-response fixtures from real Alpaca sandbox periodically validate mock accuracy.
- **Frozen clock** deterministic timestamps for invocation IDs, order timestamps, thesis ages, cooldowns.
- **RNG sources** seeded per the unit-plan convention; load-bearing where estimation logic (harness sub-models) participates in the flow.

### Preliminary scenario catalog

Preliminary. Grouped by concern.

| Group | Scenarios |
|---|---|
| Happy path | single-OPEN invocation (primary profile), multi-OPEN + multi-ADD invocation (full-system), CLOSE-only invocation, quiet invocation (zero proposals) |
| Cross-agent orchestration | analyst OPEN conflicts with strategist CLOSE on same underlying (pre-processor flags `entry_vs_close`), cumulative-capital deficit forces PM to drop low-conviction proposals, cumulative-exposure breach on combined set even though each proposal individually passes |
| Feature-flag variants | primary portfolio end-to-end, full-system end-to-end, analyst proposes disabled sector (rejected at validation tool), analyst proposes options on options-disabled profile (triple-layer defense) |
| Regime transitions | low-vol → normal mid-invocation, normal → elevated with existing positions requiring strategist remedies, low-vol → crisis triggering emergency invocation |
| Halt-mode propagation | daily drawdown triggers halt mid-invocation, next invocation agents receive halt context and behave in their respective halt modes, halt resets at next-day open |
| PM-originated feedback | PM sizing modification caught by T3 with synchronous rejection and retry, cumulative-state cascade across sequential OPENs (command N+1 sees reservation from command N) |
| Abandoned commands | broker submission exhausts retry window → rollback → next invocation's analyst context surfaces the abandoned OPEN; same for strategist and ADD/ADJUST/CLOSE/CANCEL |
| Multi-invocation | thesis aging across three invocations with `prior_status` transitions cited, emergency invocation during off-hours cadence, cumulative-drawdown HWM tracking across a week |

---

## Broker adapter contract compliance

Asserts adapter behavior against the Alpaca mock matches [broker-adapter.md](../05-execution-layer/broker-adapter.md) end-to-end. Mock fidelity against the real Alpaca API is validated separately with recorded-response fixtures from the sandbox; integration runs against the mock for determinism.

### Test concerns

**Order submission coverage.** Every `order_class` (simple, bracket, OCO, OTO, mleg) exercised with successful submission and fill. Every `type` (market, limit, stop, stop_limit, trailing_stop). Every supported TIF with expected lifecycle (day expires at session close, GTC persists across sessions, OPG fills at open auction).

**`trade_updates` event lifecycle.** Every event type in the adapter spec (`new`, `partial_fill`, `fill`, `canceled`, `expired`, `replaced`, `replace_rejected`, `stopped`, `rejected`, `done_for_day`) hit by at least one scenario. Terminal states verified — no further events after `fill`, `canceled`, `expired`, or `rejected`.

**PATCH replace semantics.** An order modified twice produces `original → replaced₁ → replaced₂` Alpaca IDs. OMS `client_order_id` stable across replacements. Fill events carry the current Alpaca ID; the adapter's mapping table resolves to the stable OMS ID.

**Race conditions.** Cancel followed by fill at original parameters → fill wins, order terminal. PATCH followed by fill at original parameters → `replace_rejected` arrives, original fill wins. Partial fill during PATCH — new quantity applies to total; if new quantity ≤ filled, remainder cancelled.

**Multi-leg strategies.** `mleg` orders submitted with 2, 3, 4 legs. Fills per-leg with shared parent strategy ID on `trade_updates`. Partial strategy states produce expected interim exposure and activity log entries.

**Short sale mechanics.** ETB shorts submit successfully. Attempted HTB short rejected at submission with Alpaca error surface. ETB→HTB overnight auto-closure: Alpaca emits `canceled` on the short's open order at next market open; OMS records involuntary closure.

**Fee reconciliation.** Fills arrive pre-fee. EOD `GET /v2/account/activities` returns `FEE`, `TAF`, `CAT`, `SEC`, `ORF`, `OCC` entries. OMS applies as a `fee_reconciliation` event attributed to that day's fills. Per-activity types tested.

### Stub boundary

- **Alpaca REST + websocket mock** with scripted responses (acknowledge, transient failure with N retries before success, persistent failure, race-condition fill arrival, out-of-order delivery). See the unit plan for the mock's construction; integration tests extend it with multi-order sequences.
- **Recorded-response fixtures** from the real Alpaca sandbox validate mock accuracy. Run periodically (not per-test), captured for the canonical order flows above.

### Additional failure-path scenarios

| Scenario | Assertion |
|---|---|
| Alpaca REST unreachable during Phase 1 reconciliation | pipeline aborts with stale-portfolio-state reason; no agents run |
| Alpaca REST unreachable during Phase 2 submit (retry succeeds) | command committed; activity log shows retry attempts |
| Alpaca REST unreachable during Phase 2 submit (retry exhausts) | full atomic rollback; `command_abandoned` event; originator surface at next invocation |
| `trade_updates` websocket disconnect mid-session | adapter reconnects, `GET /v2/orders` since-query recovers missed events, OMS state reconciles |
| Monitor restart mid-session | pending orders recovered from DB; reconnect-replay from Alpaca produces consistent state without double-processing |

---

## Two-process interaction

Pipeline and continuous monitor are separate processes coordinating through the shared database ([component-boundaries.md](../../architecture/component-boundaries.md)). These scenarios exercise the DB-mediated handoff in both directions under realistic timing.

### Test concerns

**Order round-trip.** Pipeline Phase 2 submits a limit OPEN via the adapter; Alpaca mock acknowledges. Mock emits `partial_fill` then `fill` on `trade_updates`. Monitor writes both to the fill buffer. Next pipeline Phase 1 drains the buffer, updates position state, activity log reflects chronology (Alpaca event timestamps vs. collection timestamp).

**Engine-originated protective CLOSE.** Monitor evaluates a live quote stream against portfolio state. Position-level max-loss breach detected. Monitor issues CLOSE via engine envelope with `MON.{session}.{trigger}.{ordinal}` ID and guardrail trigger reference. OMS processes through the same pipeline as PM envelopes. Next invocation's strategist context surfaces the `engine_originated_closure_signal` anti-pattern cue.

**Greeks refresh write-through.** Monitor detects 2%-underlying move on a ticker with open options. Fetches current IV, recomputes greeks via the same Black-Scholes model, writes refreshed values to the position record. Next invocation's guardrail validation reads refreshed greeks.

**Emergency invocation trigger.** Monitor observes one of four emergency trigger conditions (regime jump, multi-rule breach, drawdown velocity, margin call). Writes trigger record with type, timestamp, cooldown-gate result. Scheduler observes and starts a pipeline invocation outside the rolling cadence. Emergency context header reaches every agent. Cadence resets from the emergency timestamp.

**Cross-process activity log invariants.** PM-originated and engine-originated envelopes written to the same log. Total ordering by commit time holds. `cascade_id` linkage works across boundaries (monitor-originated CLOSE sharing a cascade ID with a margin-call event; next PM invocation sees both linked).

**Monitor restart resilience.** Monitor process restarted mid-session. On restart, recovers pending orders from DB, replays missed trigger evaluations via retroactive catch-up, resumes websocket subscription. A pipeline invocation during monitor downtime encounters gateway unavailability on Phase 1 and aborts per fail-closed.

### Stub boundary

- **Real SQLite database** — no mock. The shared-DB contract is the thing under test. In-memory or tmpfile per test.
- **Alpaca mock** drives the monitor's fill-stream consumption. Scripted event sequences deterministically emit fills at test-controlled timestamps.
- **Price websocket** stubbed with scripted tick sequences and connection-loss injection.
- **APScheduler** configured against the frozen clock, deterministic triggering, `max_instances=1` enforced.

### Preliminary scenario catalog

| Group | Scenarios |
|---|---|
| Order round-trip | pipeline writes limit OPEN → monitor triggers fill → next invocation collects fill and updates position |
| Engine-originated CLOSE | monitor detects position-level max-loss breach → issues protective CLOSE → next invocation's strategist reasons over the engine-originated closure |
| Greeks refresh | 2%-move trigger fires → refresh writes updated greeks → next invocation's validation reads refreshed values |
| Emergency trigger | regime jump detected → trigger record written → pipeline starts outside cadence → emergency context flows to agents |
| Cross-process invariants | activity log total ordering under concurrent writes; `cascade_id` linkage across PM + monitor envelopes |
| Monitor resilience | monitor restart recovers pending orders; retroactive catch-up produces missed fills; pipeline aborts during monitor-unavailable Phase 1 |

---

## Failure-mode propagation

Every documented failure mode in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md) and the error surface in [broker-adapter.md](../05-execution-layer/broker-adapter.md) gets one scripted fixture. Fixtures named after the taxonomy label. One fault-injection seam per boundary. Assertions focus on cross-component propagation, not detection logic (unit-tested).

### Test concerns

**LLM failure modes at the invocation boundary.** Each mode scripted at the LLM client seam — the same seam the pipeline's per-agent `asyncio.wait_for` wrapper and schema validator sit behind. One fixture per mode:

- `llm_timeout` — single retry with doubled budget, then abort
- `llm_malformed_output_abort` — corrective same-context retry, retry also malformed, abort
- `llm_malformed_output_recover` — corrective same-context retry succeeds, pipeline continues
- `llm_context_overflow` — no retry, immediate abort
- `llm_model_api_error` — bounded exponential backoff, abort after exhaustion
- `llm_tool_use_error_idempotent` — single retry on the tool, abort on second failure

For each: pipeline aborts at the failing agent's boundary, partial upstream outputs persisted as diagnostic records tagged with aborted invocation ID and failure point, no OMS commands submitted, monitor continues running, next scheduled trigger produces a fresh invocation against current data.

**Gateway failure modes at the gateway mock.** Each mode scripted per submitted order ID:

- `gateway_rejection_submit` — gateway rejects at submitOrder; activity log attribution `rejection_source: gateway` distinct from `rejection_source: guardrail`
- `gateway_unavailable_phase2_retry_succeed` — transient failure followed by success within retry window; command committed
- `gateway_unavailable_phase2_retry_exhaust` — persistent failure; retry window exhausts; atomic rollback; `command_abandoned` event; next-invocation surface to originating agent
- `gateway_unavailable_phase1` — collect fails; pipeline aborts with stale-portfolio-state reason
- `malformed_fill_report` — quarantined; reconciliation alert; portfolio state uncorrupted
- `out_of_sequence_fills` — processed normally; cost basis math holds
- `unsupported_amendment` — quarantined when gateway declares no amendment capability

**Fail-closed uniformity.** Every agent's failure produces identical pipeline-level behavior. Domain researcher failure and PM failure both abort the invocation; the abort pathway is parameterized on failure point but structurally identical. Integration-level confirmation that the "every agent is Critical" rule in [llm-agent-failure-handling.md](../llm-agent-failure-handling.md) holds operationally.

**Emergency invocation under failure.** An emergency invocation encountering an LLM failure aborts per the same rule — no relaxed validation, no elevated retry budget. Monitor's mechanical CLOSE authority continues independently; the failed emergency invocation does not disable the monitor.

### Stub boundary

- **LLM invocation boundary** with scripted-misbehavior modes keyed by (agent, invocation, attempt). Normal operation falls through to happy-path fixtures; scripted modes short-circuit with the failure shape.
- **Gateway mock** with scripted failure responses per submitted order ID; configurable retry-count-before-success; supports terminal-failure simulation.
- **Schema validator** runs real against scripted malformed fixtures — detection is the behavior being exercised end-to-end, not stubbed.

### Preliminary scenario catalog

| Failure mode | Seam | Assertion |
|---|---|---|
| `llm_timeout` | LLM invocation | abort after retry, diagnostics persisted, no commands, monitor continues |
| `llm_malformed_output_abort` | LLM invocation | corrective retry attempted, final abort, diagnostics persisted |
| `llm_malformed_output_recover` | LLM invocation | corrective retry succeeds, pipeline continues normally |
| `llm_context_overflow` | LLM invocation | immediate abort, no retry attempted |
| `llm_model_api_error` | LLM invocation | bounded exponential backoff, abort on exhaustion |
| `llm_tool_use_error_idempotent` | LLM invocation | single retry, abort on second failure |
| `gateway_rejection_submit` | gateway mock | activity log tagged `rejection_source: gateway`, distinct from guardrail rejection |
| `gateway_unavailable_phase2_retry_succeed` | gateway mock | retry within window, command committed |
| `gateway_unavailable_phase2_retry_exhaust` | gateway mock | atomic rollback, `command_abandoned` event, originator surface next invocation |
| `gateway_unavailable_phase1` | gateway mock | pipeline abort, stale-portfolio-state reason |
| `malformed_fill_report` | gateway mock | quarantined, reconciliation alert, state intact |
| `out_of_sequence_fills` | gateway mock | processed, cost basis correct |
| `unsupported_amendment` | gateway mock | quarantined when amendment capability declared absent |
| emergency-under-failure | both seams | emergency aborts per fail-closed, monitor CLOSE authority intact |
| fail-closed uniformity | LLM invocation | every agent's failure produces structurally identical pipeline behavior |

---

## Shared fixture inventory

Integration tests consume a superset of the unit-plan fixtures ([unit-test-plan.md §Shared fixture inventory](unit-test-plan.md#shared-fixture-inventory)). Unit fixtures are the foundation; integration adds pipeline-level orchestration fixtures. One catalog, one source of truth.

### LLM scripted-response library

Consumed by: every end-to-end pipeline scenario, every failure-mode propagation scenario.

A directory of fixture files keyed by (scenario name, agent name). Each file contains either a valid happy-path response conforming to the agent's output contract, or a scripted-misbehavior response tagged with the failure mode it represents. Fixture file names use the failure-taxonomy labels from [llm-agent-failure-handling.md](../llm-agent-failure-handling.md) to make drift detection trivial.

The library covers all LLM agents: domain researchers, qualitative researcher, adaptive researcher, synthesizer, analyst, strategist, PM.

### Data-layer scripted-response library

Consumed by: every end-to-end pipeline scenario.

Per-category canned payloads for the quantitative and qualitative data categories defined in [external/quantitative.md](../01-data-layer/external/quantitative.md) and [external/qualitative.md](../01-data-layer/external/qualitative.md). Freshness metadata attached to each payload for fresh-or-abort testing. Named presets mirror [scenario-tests.md](../06-risk-guardrails/scenario-tests.md) scenario inputs where the scenarios align.

### Scheduled-invocation fixture

Consumed by: emergency invocation scenarios, multi-invocation continuity scenarios, halt-mode propagation scenarios.

Builder generates a sequence of invocation triggers against a frozen clock with cron-anchored timestamps. Emergency triggers injectable at arbitrary points. APScheduler configured against the frozen clock, `max_instances=1`.

### Multi-invocation DB seed

Consumed by: aging-thesis scenarios, `prior_status` transition scenarios, abandoned-command surfacing scenarios.

Builder produces a DB at a specified state — positions at specified ages, theses at specified statuses, activity log entries at specified timestamps — as the starting point for an invocation under test. Removes the need to replay N prior invocations just to set up state for invocation N+1.

### Gateway scripted-response library

Consumed by: broker adapter contract compliance, broker-failure propagation.

Same structure as the LLM scripted-response library — fixture files keyed by (scenario name, submitted order ID). Covers the order-flow scenarios named in the Broker adapter contract compliance section plus the additional broker-failure scenarios.

---

## Resolved design questions

- **LLM failure injection location.** Single seam at the LLM invocation boundary. Rationale: happy-path fixtures stay clean; failure-mode coverage auditable against the spec taxonomy; adding a new documented failure mode requires changing exactly one place.

- **Scripted vs. recorded LLM fixtures.** Scripted. Real LLM failures are rare and non-deterministic; recording produces brittle tests. Scripted fixtures map 1:1 to the documented failure taxonomy.

- **Backtesting scope.** Out of scope. Historical replay against real market data is a Phase 4 concern covering strategy validation.

- **Broker adapter contract-test source of truth.** [broker-adapter.md](../05-execution-layer/broker-adapter.md) is authoritative. Drift between spec and test suite is a structural failure.

- **Live-API mock fidelity.** Recorded-response fixtures from the real Alpaca sandbox periodically validate mock accuracy. Integration runs against the mock for determinism.

- **Schema validator stubbing.** Not stubbed. Integration runs the real validator against scripted malformed outputs — the propagation path from "validator detects" to "pipeline aborts with correct diagnostics" is exactly what integration testing is for.

- **Emergency-trigger-during-invocation overlap.** An emergency trigger during a rolling invocation **interrupts and replaces** it. Rolling invocation cancelled (in-flight state discarded per [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)) and the emergency runs in its place. `max_instances=1` preserved. Canonical statement in [breach-behavior.md §Emergency invocation trigger](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger); integration exercises rather than defines.

- **Continuous monitor test isolation.** Thread-model. DB-mediated contract is the assertion target; OS-level process-boundary assertions aren't load-bearing for integration scope.

- **Multi-invocation DB seed construction.** Hand-constructed seeds. Builder API exposes positions, theses, activity log entries, drawdown state with sensible defaults. The win is test clarity and per-test state ownership.

- **Pre-processor spec dependency.** [proposal-pre-processor.md](../04-decision-layer/proposal-pre-processor.md) and [proposal-pre-processor-bundle-schema.md](../04-decision-layer/proposal-pre-processor-bundle-schema.md) define the §1/§2/§3 bundle structure and validation contract; cross-agent orchestration scenarios target these shapes directly.

---

## Cross-references

- Unit test plan (integration sits above it): [unit-test-plan.md](unit-test-plan.md)
- LLM agent failure taxonomy: [llm-agent-failure-handling.md](../llm-agent-failure-handling.md)
- Broker adapter (Alpaca) error surface and contract: [broker-adapter.md](../05-execution-layer/broker-adapter.md)
- Paper-evaluation harness: [paper-evaluation-harness.md](../05-execution-layer/paper-evaluation-harness.md)
- Mid-pipeline failure invariants: [mid-pipeline-failure-handling.md](../mid-pipeline-failure-handling.md)
- API failure handling (fresh-or-abort discipline): [01-data-layer/api-failure-handling.md](../01-data-layer/api-failure-handling.md)
- Two-process architecture: [component-boundaries.md](../../architecture/component-boundaries.md)
- Two-phase invocation model: [05-execution-layer/architecture.md](../05-execution-layer/architecture.md)
- Continuous monitor responsibilities: [05-execution-layer/architecture.md §4](../05-execution-layer/architecture.md)
- Proposal pre-processor integration seam: [04-decision-layer/proposal-pre-processor.md](../04-decision-layer/proposal-pre-processor.md)
- OMS command ID discipline: [oms-command-ids.md](../oms-command-ids.md)
- Design-validation walkthroughs (distinct from integration tests): [06-risk-guardrails/scenario-tests.md](../06-risk-guardrails/scenario-tests.md)
- Project tracker: [project-tracker.md](../../project-tracker.md)
