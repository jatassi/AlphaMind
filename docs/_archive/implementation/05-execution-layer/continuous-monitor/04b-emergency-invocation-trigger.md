# 04b — Emergency invocation trigger

## Goal

Detect emergency-pipeline-invocation conditions (regime jump, multi-rule breach, daily-drawdown velocity, margin call) and emit an `EMERGENCY_INVOCATION_REQUESTED` activity-log event carrying the trigger reason and cooldown bookkeeping. The receiver — `ALP-431` (Pipeline scheduler) — reads these events and autonomously dispatches the pipeline; this story writes the request side only. Enforces the 30-minute cooldown rule (`config.emergency_invocation_cooldown_minutes`) so multiple conditions firing within the window coalesce into one request, with the margin-call override that bypasses cooldown.

**Cooldown source — updated 2026-05-11.** The cooldown value lives canonically in `config/breach_behavior.yaml` as `emergency_invocation_cooldown_minutes: 30`, exposed on `BreachBehaviorConfig.emergency_invocation_cooldown_minutes`. The Pipeline scheduler's story 04b ([ALP-447](https://linear.app/alphamind-jatassi/issue/ALP-447/04b-emergency-invocation-receiver-eventtype-runtypeemergency-cooldown)) added the field to `breach_behavior.yaml` + the matching field on `BreachBehaviorConfig`. This story (the monitor's 04b) loads the cooldown from `BreachBehaviorConfig`, **not** from `ContinuousMonitorConfig`. The parent's decision (E) was updated to remove `emergency_invocation_cooldown_minutes` from the `continuous_monitor.yaml` knob list. Sources of confusion to avoid: the `EmergencyTriggerEvaluator` constructor receives the cooldown value (or a `BreachBehaviorConfig` to read it from), not a `ContinuousMonitorConfig`-only configuration.

## Reading

* `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — trigger conditions table, cooldown rules, margin-call exception, relationship to scheduled invocations, agent context modifications
* `docs/design/05-execution-layer/architecture.md` § 4c Emergency invocation triggering
* `docs/design/05-execution-layer/state-persistence.md` § Invocation table — `trigger_type ∈ {scheduled, emergency, manual}` and `trigger_source` fields the future scheduler will populate from the events this story writes
* `src/alphamind/risk_guardrails/breach_behavior/emergency_triggers.py` — every evaluator this story composes: `evaluate_regime_jump`, `evaluate_multi_rule_breach`, `evaluate_daily_drawdown_velocity`, `evaluate_margin_call`, `evaluate_emergency_invocation` (+ `DrawdownSample`, `MarginCallEvent`)
* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` (story 03b) — `BreachLoopResult` the callback receives; `result.drawdown_velocity_sample` is the cycle's `DrawdownSample`
* `src/alphamind/portfolio_state/events/activity_log.py` — `EventType` enum + `EVENT_DETAIL_TYPES` mapping the additive `EMERGENCY_INVOCATION_REQUESTED` entry joins. **Note:** `EventType.EMERGENCY_INVOCATION_REQUESTED` was already added to the enum + `EVENT_TYPE_TO_DETAIL` mapping by the Pipeline scheduler's story 04b ([ALP-447](https://linear.app/alphamind-jatassi/issue/ALP-447/04b-emergency-invocation-receiver-eventtype-runtypeemergency-cooldown)); the detail Pydantic class is `EmergencyInvocationRequestedDetail` with fields `trigger_type: Literal[...]`, `trigger_reason: str`, `cooldown_remaining_seconds: int`. This story's writer constructs that existing class — DO NOT redefine it.
* `src/alphamind/config/models/breach_behavior.py` — `BreachBehaviorConfig.emergency_invocation_cooldown_minutes` (the cooldown source per the note above)
* Parent issue `ALP-123` § Pre-resolved decision (D) — dispatch shape (activity-log event + deferred receiver in [ALP-431](https://linear.app/alphamind-jatassi/issue/ALP-431/pipeline-scheduler)); § Pre-resolved decision (E) — knobs in `continuous_monitor.yaml` (excluding the cooldown which lives in `breach_behavior.yaml` per the 2026-05-11 update)
* `ALP-431` (Pipeline scheduler, landed) — downstream consumer that reads `EMERGENCY_INVOCATION_REQUESTED` entries and dispatches `run_invocation(trigger_type="emergency", ...)`

## Depends on

* [ALP-431](https://linear.app/alphamind-jatassi/issue/ALP-431/pipeline-scheduler) — Pipeline scheduler (hard gate; landed). The scheduler's story 04b ([ALP-447](https://linear.app/alphamind-jatassi/issue/ALP-447/04b-emergency-invocation-receiver-eventtype-runtypeemergency-cooldown)) already shipped the `EventType.EMERGENCY_INVOCATION_REQUESTED` vocabulary + detail class + `BreachBehaviorConfig.emergency_invocation_cooldown_minutes` field; this story consumes those primitives.
* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config
* [ALP-437](https://linear.app/alphamind-jatassi/issue/ALP-437/03b-breach-evaluation-loop) (03b) — `on_emergency_input` callback the loop awaits

## Scope

Source under `src/alphamind/execution/continuous_monitor/emergency_trigger/` (new sub-package). Tests at `tests/execution/continuous_monitor/emergency_trigger/`.

### 1\. Activity-log event — already shipped

`EventType.EMERGENCY_INVOCATION_REQUESTED` + `EmergencyInvocationRequestedDetail` were added additively by [ALP-447](https://linear.app/alphamind-jatassi/issue/ALP-447/04b-emergency-invocation-receiver-eventtype-runtypeemergency-cooldown) (Pipeline scheduler story 04b). The detail's fields are:

```python
class EmergencyInvocationRequestedDetail(BaseModel):
    model_config = ConfigDict(frozen=True)
    trigger_type: Literal[
        "regime_jump", "multi_rule_breach",
        "drawdown_velocity", "margin_call",
    ]
    trigger_reason: str
    cooldown_remaining_seconds: int  # ge=0; 0 when cooldown satisfied or bypassed by margin_call
```

This story constructs this existing class — do NOT redefine. If you need additional fields (`monitor_session_id`, `trigger_id`, `requested_at`, `cooldown_bypassed`) per the original story spec below, surface to the orchestrator: those would be additive changes to the existing class and require coordination with the scheduler's reader to avoid breaking it.

### 2\. Cooldown tracker

`src/alphamind/execution/continuous_monitor/emergency_trigger/cooldown.py`:

```python
class CooldownTracker:
    """In-memory tracker of the last emergency request's timestamp.

    The tracker accepts a (now, trigger_type) tuple and returns whether
    this trigger may fire. Margin-call triggers ALWAYS pass (the broker's
    deadline is non-negotiable per breach-behavior); other triggers are
    suppressed for cooldown_minutes after a prior emit.
    """
    def __init__(self, *, cooldown_minutes: int) -> None: ...
    def may_fire(self, *, now: datetime, trigger_type: str) -> bool: ...
    def record_fire(self, *, now: datetime, trigger_type: str) -> None: ...
```

The constructor receives `cooldown_minutes: int` — load this value from `BreachBehaviorConfig.emergency_invocation_cooldown_minutes` in the caller (the evaluator's constructor below), not from `ContinuousMonitorConfig`. Pure in-memory tracker; reset across monitor restarts (a restart implies the prior emergency context is gone).

### 3\. Evaluator wrapper

`src/alphamind/execution/continuous_monitor/emergency_trigger/evaluator.py`:

```python
class EmergencyTriggerEvaluator:
    def __init__(
        self,
        *,
        session: MonitorSession,
        monitor_config: ContinuousMonitorConfig,
        breach_behavior_config: BreachBehaviorConfig,
        cooldown: CooldownTracker,
        trigger_ids: TriggerIdGenerator,
        session_factory: Callable[[], AsyncSession],
    ) -> None: ...

    async def handle_emergency_input(self, result: BreachLoopResult) -> None:
        \"\"\"The on_emergency_input callback signature.

        1. Maintain a per-session rolling window of regime classifications
           and DrawdownSample observations (needed by evaluate_regime_jump
           and evaluate_daily_drawdown_velocity respectively).
        2. Call evaluate_emergency_invocation(...) with:
             - regime_history: rolling window updated this cycle.
             - rule_evaluations: result.rule_evaluations.
             - drawdown_samples: rolling window updated this cycle.
             - margin_call_event: result.margin_call_event (if present).
        3. The evaluator returns an EmergencyContext describing fire / no-fire
           plus the triggering condition.
        4. On fire:
             - Check cooldown.may_fire(...). If suppressed, log a debug line
               and return without emitting.
             - If margin_call, set cooldown_bypassed=True regardless.
             - Build EmergencyInvocationRequestedDetail and write a
               EMERGENCY_INVOCATION_REQUESTED activity-log entry via the
               session_factory.
             - Call cooldown.record_fire(...).
        \"\"\"
```

`breach_behavior_config` is the loaded `BreachBehaviorConfig` from `config/breach_behavior.yaml`; the constructor reads `breach_behavior_config.emergency_invocation_cooldown_minutes` to construct the `CooldownTracker`. `monitor_config` is still threaded for the other knobs (breach cadence, greeks refresh, etc).

### 4\. Margin-call event sourcing

`evaluate_margin_call(...)` requires a `MarginCallEvent`. Source it from the OMS reconciliation surface — the broker adapter's `GET /v2/account` query (or whatever account-state query surfaces margin-call status) is consulted on each loop tick. If the broker adapter already exposes a margin-call observer in `broker_adapter/account_state.py`, reuse it; otherwise the breach loop (story 03b) extends to surface `margin_call_event: MarginCallEvent | None` on `BreachLoopResult`. Coordinate with story 03b's author if this extension is needed — surface the question early.

### 5\. Supervisor wiring

Replace story 03b's no-op `on_emergency_input` stub with `EmergencyTriggerEvaluator.handle_emergency_input`. Trigger-ID generator is shared with the cascade dispatcher (story 04a) so envelope and emergency-request trigger IDs come from the same monotonic sequence.

### 6\. Tests at `tests/execution/continuous_monitor/emergency_trigger/`

* `test_cooldown.py` — non-margin triggers within the cooldown window are suppressed; margin-call always passes; record-fire advances the window.
* `test_evaluator.py` — using fakes for the activity-log writer:
  * Regime jump: a regime history showing `low_vol → crisis` triggers an emit with `trigger_type="regime_jump"`.
  * Multi-rule breach: ≥ 3 deferred-rule HARD_BLOCKs in `result.rule_evaluations` triggers `trigger_type="multi_rule_breach"`.
  * Daily-drawdown velocity: a rolling sample showing 60% of the daily limit crossed within 30 minutes triggers `trigger_type="drawdown_velocity"`.
  * Margin call: a `MarginCallEvent` triggers `trigger_type="margin_call"` and `cooldown_remaining_seconds=0` (bypass) even if a prior emit was within the window.
  * Cooldown suppression: a second regime-jump trigger within `cooldown_minutes` is suppressed.
  * No fire: a result with no qualifying trigger emits nothing.
  * Determinism: identical inputs across two cycles produce identical fire / no-fire decisions (modulo the cooldown clock).
  * Cooldown source: the evaluator's constructor receives a `BreachBehaviorConfig` and constructs the `CooldownTracker` from `breach_behavior_config.emergency_invocation_cooldown_minutes` — exercise with a fixture that sets a non-default value (e.g., `45`) to prove the right config is consulted.

### Out of scope

* Building or running the pipeline scheduler that reads these events — `ALP-431` (already landed).
* Modifying `evaluate_*` primitives — they live in `breach_behavior/emergency_triggers.py`.
* Surfacing emergency-triggered invocations to operator dashboards — covered by existing `scripts/report_emergency_invocations.py` ([ALP-99](https://linear.app/alphamind-jatassi/issue/ALP-99/15-emergency-invocation-review-report)); no new operator script in this story.

## Acceptance criteria

- [ ] `EventType.EMERGENCY_INVOCATION_REQUESTED` and `EmergencyInvocationRequestedDetail` (already shipped by [ALP-447](https://linear.app/alphamind-jatassi/issue/ALP-447/04b-emergency-invocation-receiver-eventtype-runtypeemergency-cooldown)) are consumed by this story's writer; existing event-mapping tests still pass.
- [ ] `CooldownTracker` constructor's `cooldown_minutes` value is sourced from `BreachBehaviorConfig.emergency_invocation_cooldown_minutes`, not from `ContinuousMonitorConfig`.
- [ ] `CooldownTracker` enforces `cooldown_minutes` for non-margin triggers; `margin_call` triggers bypass the cooldown and record `cooldown_remaining_seconds=0`.
- [ ] `EmergencyTriggerEvaluator.handle_emergency_input` satisfies the `on_emergency_input: Callable[[BreachLoopResult], Awaitable[None]]` signature consumed by story 03b's loop.
- [ ] A `regime_jump` condition produces exactly one `EMERGENCY_INVOCATION_REQUESTED` activity-log entry with `trigger_type="regime_jump"` and a human-readable `trigger_reason`.
- [ ] A `multi_rule_breach` condition (≥ 3 deferred-rule HARD_BLOCKs) produces an entry with `trigger_type="multi_rule_breach"`.
- [ ] A `drawdown_velocity` condition (drawdown crosses 60% of limit within 30 minutes — per `breach-behavior.md`) produces an entry with `trigger_type="drawdown_velocity"`.
- [ ] A `margin_call` event produces an entry with `trigger_type="margin_call"` and `cooldown_remaining_seconds=0` even when prior emits exist within the cooldown window.
- [ ] A second non-margin trigger within `cooldown_minutes` is suppressed and does NOT produce an entry; a log line records the suppression.
- [ ] Tests at `tests/execution/continuous_monitor/emergency_trigger/` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/emergency_trigger/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/emergency_trigger/ -n auto -v`.
* `uv run pytest -n auto` — full suite green.
* Manual smoke during story 05's e2e verification: drive a fixture into a `regime_jump` condition, observe one `EMERGENCY_INVOCATION_REQUESTED` row in `activity_log` with the expected detail payload; advance the clock < `cooldown_minutes`, observe a second jump is suppressed; flip a `margin_call` and observe the cooldown is bypassed (`cooldown_remaining_seconds=0`).
* Cross-check: with `breach_behavior.yaml` set to `emergency_invocation_cooldown_minutes: 45`, the suppression window is 45 minutes rather than 30 (proves the right config source is consulted).
* Lint clean per CLAUDE.md.