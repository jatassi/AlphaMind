# 03b — Breach evaluation loop

## Goal

The periodic guardrail loop. Every `breach_evaluation_cadence_seconds` (default 60) the task reads portfolio state via the repository, calls the canonical `compose_phase_1_enforcement` to derive the `ActiveRiskParameterSet`, builds `MarketInputs` from the underlying-price cache (story 02b) + the latest options-position greeks (refreshed by story 03a), evaluates every rule through the guardrail-evaluation library, and classifies each rule's zone via `classify_zone`. The loop is the *gatekeeper* for two downstream paths:

* Hard-block breaches with immediate-action classification → dispatch via story 04a's cascade dispatcher.
* Per-cycle inputs to the emergency-invocation evaluator → consumed by story 04b.

This story also owns daily / cumulative drawdown halt-onset detection: on transitions, it emits the additive `HALT_ACTIVATED` / `HALT_LIFTED` activity-log events.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4b — periodic evaluation contract
* `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model, § Forced reduction policy, § Per-rule breach response classification, § Drawdown halt mode — per-rule classification + zone thresholds + halt model the loop implements
* `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload — the `ActiveRiskParameterSet` contract the loop consumes
* `src/alphamind/risk_guardrails/guardrail_evaluation/` — `MarketInputs`, `LibraryConfig`, `FeatureFlagsView`, and the rule-evaluation entry point (read its signature; the loop calls it)
* `src/alphamind/risk_guardrails/breach_behavior/` — `classify_zone`, `compute_halt_state`, `classify_cumulative_drawdown_tier`, and the per-rule classification mapping (immediate-action vs deferred-to-PM)
* `src/alphamind/execution/guardrail_enforcement/` — `compose_phase_1_enforcement` + `Phase1EnforcementResult`; the same primitive story 02a wires into the pipeline
* `src/alphamind/portfolio_state/repository.py` — read-side: open positions, drawdown state, sector exposure, directional exposure
* `src/alphamind/portfolio_state/records/capital.py` — `ActiveRiskParameterSet`, `DrawdownState`
* `src/alphamind/portfolio_state/events/activity_log.py` — `EventType` enum the additive `HALT_ACTIVATED` / `HALT_LIFTED` values join; `EVENT_DETAIL_TYPES` mapping
* Parent issue `ALP-123` § Pre-resolved decision (H) — halt-state activation logging behavior
* `ALP-432` (01), `ALP-433` (02a), `ALP-434` (02b) — supervisor + Phase 1 wiring + underlying-price cache

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config
* [ALP-433](https://linear.app/alphamind-jatassi/issue/ALP-433/02a-wire-compose-phase-1-enforcement-into-the-decision-pipeline) (02a) — canonical `compose_phase_1_enforcement` entry the loop calls
* [ALP-434](https://linear.app/alphamind-jatassi/issue/ALP-434/02b-live-underlying-price-stream-task-alpaca-stockdatastream-iex) (02b) — `UnderlyingPriceCache` for `MarketInputs.underlying_prices`

## Scope

Source under `src/alphamind/execution/continuous_monitor/breach_loop/` (new sub-package). Tests at `tests/execution/continuous_monitor/breach_loop/`.

### 1\. `BreachLoopResult` typed record

`src/alphamind/execution/continuous_monitor/breach_loop/result.py`:

```python
@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_id: str
    current_value: float
    limit_value: float
    overage: float           # signed; > 0 when current_value exceeds limit_value
    zone: RiskZone           # NORMAL / WARNING / CRITICAL / HARD_BLOCK
    classification: BreachResponse | None  # immediate / deferred / etc.; None if not breaching

@dataclass(frozen=True, slots=True)
class BreachLoopResult:
    as_of: datetime
    phase1_result: Phase1EnforcementResult
    rule_evaluations: tuple[RuleEvaluation, ...]
    halt_state: HaltState
    immediate_action_breaches: tuple[RuleEvaluation, ...]   # subset where classification == IMMEDIATE
    drawdown_velocity_sample: DrawdownSample                # for story 04b
```

Frozen dataclass; pure value object.

### 2\. Loop orchestrator

`src/alphamind/execution/continuous_monitor/breach_loop/task.py`:

```python
async def run_breach_loop(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    session_factory: Callable[[], AsyncSession],
    on_immediate_breach: Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]],
    on_emergency_input: Callable[[BreachLoopResult], Awaitable[None]],
) -> None:
    """Run-forever task. Every breach_evaluation_cadence_seconds:

    1. Read open positions + DrawdownState + regime resolution + progressive_tiers.
    2. Call compose_phase_1_enforcement(...) → Phase1EnforcementResult.
    3. Build MarketInputs(underlying_prices=cache.get_all(), risk_free_rate=…,
       iv_provider=…, as_of=now).
    4. Build LibraryConfig from phase1_result.active_risk_parameters
       (effective_limits, escalation_zones, feature_flags, active_sectors,
       active_regime, active_profile, conservative_buffer_pct).
    5. Call the guardrail-evaluation rule-evaluator → per-rule LibraryOutput.
    6. For each rule, classify_zone(current, limit, escalation_zones) →
       RiskZone; map to BreachResponse per breach-behavior's per-rule
       classification.
    7. Build the BreachLoopResult.
    8. Detect halt transitions:
         - On halt-onset (compute_halt_state crosses INACTIVE → ACTIVE),
           emit HALT_ACTIVATED activity-log event with details.
         - On halt-lift (ACTIVE → INACTIVE), emit HALT_LIFTED.
    9. For each rule with classification IMMEDIATE: await
       on_immediate_breach(result, rule). The callback is wired by 04a.
    10. Await on_emergency_input(result). The callback is wired by 04b.
    11. Sleep until the next tick.
    """
```

Step 9 / 10 use callbacks so this story does not depend on 04a / 04b being landed — the supervisor wires real callbacks once those stories ship; tests inject fakes.

### 3\. Halt-state activity-log events

Add `EventType.HALT_ACTIVATED = "HALT_ACTIVATED"` and `EventType.HALT_LIFTED = "HALT_LIFTED"` (additive enum values) and Pydantic details:

```python
class HaltActivatedDetail(BaseModel):
    model_config = ConfigDict(frozen=True)
    halt_type: Literal["daily_drawdown", "cumulative_drawdown_tier3"]
    current_drawdown_pct: float
    limit_pct: float
    detected_at: datetime  # tz-aware UTC

class HaltLiftedDetail(BaseModel):
    model_config = ConfigDict(frozen=True)
    halt_type: Literal["daily_drawdown", "cumulative_drawdown_tier3"]
    current_drawdown_pct: float
    lifted_at: datetime
```

Update `EVENT_DETAIL_TYPES`. Additive only — existing tests pass unchanged.

### 4\. Last-halt tracking

`src/alphamind/execution/continuous_monitor/breach_loop/halt_tracker.py`:

```python
class HaltTransitionTracker:
    """In-memory tracker that remembers the last seen HaltState so the loop
    only emits HALT_ACTIVATED on the inactive→active transition (not on every
    cycle while halt is in effect).
    """
    def observe(self, halt_state: HaltState) -> Iterable[ActivityLogEntry]: ...
```

On every loop tick, the orchestrator calls `observe(halt_state)`; the tracker emits zero, one, or two activity-log entries per call (e.g., both daily and cumulative tier-3 may transition simultaneously).

### 5\. Supervisor registration

Extend `__main__.py` to construct the `on_immediate_breach` and `on_emergency_input` callbacks (initially stubs that no-op; 04a / 04b replace them with real handlers) and register the loop task as `"breach_loop"`.

### 6\. Tests at `tests/execution/continuous_monitor/breach_loop/`

* `test_result.py` — `BreachLoopResult` and `RuleEvaluation` shapes; frozen invariants.
* `test_halt_tracker.py` — emits `HALT_ACTIVATED` on first observation in active state, no event while halt persists, `HALT_LIFTED` on transition back.
* `test_task.py` — using fakes for the repository, cache, regime resolver, and `compose_phase_1_enforcement`:
  * Loop runs; for a fixture portfolio in `NORMAL` zones, no immediate breaches; `on_immediate_breach` callback is not invoked; `on_emergency_input` is invoked with the result.
  * For a fixture portfolio with a `HARD_BLOCK` on `per_position_max_loss` (classified IMMEDIATE), `on_immediate_breach` is invoked exactly once with the breaching `RuleEvaluation`.
  * For a deferred-classification breach (e.g., `sector_concentration` in `HARD_BLOCK`), `on_immediate_breach` is NOT invoked; the breach is still listed in `result.rule_evaluations`.
  * Halt-onset: `DrawdownState.current_drawdown_pct` crossing the daily limit triggers `HALT_ACTIVATED`; persisting at the limit on the next cycle emits no event; recovering below triggers `HALT_LIFTED`.
  * Cumulative tier 3: same pattern as daily halt.
  * `asyncio.CancelledError` exits the loop cleanly.
  * Determinism: identical inputs → identical `BreachLoopResult`.

### Out of scope

* The cascade dispatch logic that builds engine envelopes and submits CLOSEs — story 04a.
* The emergency-invocation trigger evaluator (`evaluate_emergency_invocation`) — story 04b.
* Options bracket-stop evaluation — story 04c.
* New rule definitions — the loop calls the existing guardrail-evaluation library; rule registry stays at risk-guardrails/rules-and-limits ownership.

## Acceptance criteria

- [ ] `BreachLoopResult`, `RuleEvaluation`, and `HaltTransitionTracker` are importable from `alphamind.execution.continuous_monitor.breach_loop`.
- [ ] `run_breach_loop` calls `compose_phase_1_enforcement` once per cycle and uses its `active_risk_parameters` to build the `LibraryConfig` passed into the evaluator.
- [ ] `MarketInputs` is built per cycle from `UnderlyingPriceCache.get_all()` + the configured `risk_free_rate` + an `iv_provider` resolving from the latest `OptionsPositionDetails.greeks.iv_used` per position.
- [ ] For an immediate-action rule in `HARD_BLOCK` (e.g., `per_position_max_loss`), `on_immediate_breach(result, rule)` is awaited exactly once per cycle the rule fires.
- [ ] For a deferred rule in `HARD_BLOCK` (e.g., `sector_concentration`), `on_immediate_breach` is NOT invoked; the rule's evaluation is recorded in `result.rule_evaluations`.
- [ ] `on_emergency_input(result)` is awaited exactly once per cycle (regardless of breach state) so story 04b can run its evaluators.
- [ ] `HaltTransitionTracker` emits `EventType.HALT_ACTIVATED` exactly once per inactive→active transition and `HALT_LIFTED` exactly once per active→inactive transition; no events while halt persists in either state.
- [ ] `EventType.HALT_ACTIVATED`, `EventType.HALT_LIFTED`, `HaltActivatedDetail`, and `HaltLiftedDetail` are added additively; `EVENT_DETAIL_TYPES` is updated; existing event-mapping tests still pass.
- [ ] `breach_evaluation_cadence_seconds` from `ContinuousMonitorConfig` controls the loop period; the loop sleeps approximately that many seconds between ticks (allow ±5% scheduling jitter).
- [ ] Outside market hours (per venue calendar), the loop pauses — no evaluation, no events.
- [ ] `asyncio.CancelledError` exits the loop cleanly.
- [ ] Tests at `tests/execution/continuous_monitor/breach_loop/` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/breach_loop/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/breach_loop/ -n auto -v`.
* `uv run pytest -n auto` — full suite green.
* Manual smoke during story 05's e2e verification: start the monitor against a fixture portfolio with a synthetic `HARD_BLOCK` on a deferred rule, observe the rule appearing in `result.rule_evaluations` without triggering `on_immediate_breach`; then synthetically flip an immediate-action rule's state and observe the callback fire.
* Lint clean per CLAUDE.md.