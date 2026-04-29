---
status: not_started
completed_date:
commit_id:
---

# 05c — Emergency invocation trigger evaluation

## Goal

Land the deterministic primitives that evaluate the four emergency-invocation trigger conditions (regime jump, multi-rule breach, daily-drawdown velocity, margin call), apply the 30-minute cooldown rule with the margin-call exception, and produce an `EmergencyContext` when an emergency should fire. The continuous monitor (execution-layer work tree) calls these primitives at its evaluation cadence; the resulting `EmergencyContext` informs the scheduler to interrupt rolling invocations or cancel the next scheduled run and dispatch immediately. State-delivery's emergency-invocation header (story 06) renders the context into agent-facing text.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — the authoritative trigger contract:

  | Trigger | Threshold | Rationale |
  |---------|-----------|-----------|
  | Regime jump | Classification skips a level (e.g., low-vol → elevated, normal → crisis, low-vol → crisis) | Sudden severe shift requires immediate reasoning |
  | Multi-rule breach | 3+ deferred rules simultaneously breach between invocations | Coordinated market move requires coordinated agent response |
  | Daily drawdown velocity | Daily drawdown crosses 60% of limit within 30 minutes of last check | Acceleration outpacing the scheduled cadence |
  | Margin call | Broker issues a margin call | External non-negotiable deadline |

- `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — Cooldown:
  > Minimum 30-minute cooldown between triggers. Multiple conditions firing within the window coalesce into the in-progress emergency invocation. **Exception:** Margin calls override the cooldown.

- `config/guardrails.yaml` § `emergency_invocation` — the shipped trigger registry:
  ```yaml
  emergency_invocation:
    cooldown_minutes: 30
    triggers:
      - regime_jump
      - multi_rule_breach_count_min: 3
      - daily_drawdown_velocity_pct_in_minutes: { pct: 60, minutes: 30 }
      - margin_call
  ```

- `src/alphamind/config/models/guardrails.py` — `EmergencyInvocation` Pydantic model holding `cooldown_minutes` and the heterogeneous `triggers` list.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `EmergencyContext`, `EmergencyTrigger`, `RegimeLabel`, `DrawdownState`, `RiskBudgetConsumption` (re-exports).
- `src/alphamind/risk_guardrails/breach_behavior/zones.py` — `classify_zone` (story 04a) for the multi-rule breach evaluator's "rule in BLOCKED zone" check.
- `src/alphamind/risk_guardrails/breach_behavior/config.py` — `BreachBehaviorConfig.drawdown_velocity_window_minutes`, `drawdown_velocity_threshold_pct_of_daily_limit`, `multi_rule_breach_simultaneous_deferred_rules_count`.
- `docs/design/06-risk-guardrails/scenario-tests.md` § A10 (low-vol → crisis regime jump) — the canonical regime-jump worked example, showing skip-a-level behavior and the resulting emergency invocation.
- `docs/design/06-risk-guardrails/scenario-tests.md` § A7 (margin call cascade) — confirms margin-call triggers fire even during cooldown.

## Depends on

- 02 (package skeleton + config — for the velocity-window, threshold, and multi-rule-count parameters)
- 03 (canonical types — `EmergencyContext`, `EmergencyTrigger`, `DrawdownState`, `RiskBudgetConsumption`, `RegimeLabel`)
- 04a (zone classifier — for multi-rule breach evaluator)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/emergency_triggers.py`. Tests at `tests/risk_guardrails/breach_behavior/test_emergency_triggers.py`.

The module exposes one public composer plus four per-trigger evaluators (small enough to live in one file). Each evaluator returns either an `EmergencyTrigger | None` (the trigger fires or it doesn't); the composer aggregates them with the cooldown rule and produces the final `EmergencyContext | None`.

### 1. Per-trigger evaluators

#### 1.1 Regime jump

```python
def evaluate_regime_jump(
    *,
    prior_regime_label: RegimeLabel,
    current_regime_label: RegimeLabel,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect whether the regime classification skipped a level.

    Regimes are ordered: LOW_VOL < NORMAL < ELEVATED < CRISIS. A "skip" is any transition
    where |new_index - prior_index| ≥ 2. Adjacent transitions (LOW_VOL → NORMAL,
    NORMAL → ELEVATED, etc.) do NOT trigger — they are the normal regime-adaptation path.

    Returns (EmergencyTrigger.REGIME_JUMP, descriptive_text) if a jump fires, else (None, None).

    Skip-detection cases that fire:
      LOW_VOL → ELEVATED      (skip NORMAL)
      LOW_VOL → CRISIS        (skip NORMAL, ELEVATED)
      NORMAL → CRISIS         (skip ELEVATED)
      ELEVATED → LOW_VOL      (rapid loosening; per design table, only "low-vol → elevated"
                               and "normal → crisis" and "low-vol → crisis" are listed but
                               the design's "Classification skips a level" wording covers
                               directionless skips; this primitive treats both directions
                               as triggers, since rapid recovery to LOW_VOL from ELEVATED
                               or CRISIS is also a sudden environment shift)
      CRISIS → NORMAL         (skip ELEVATED)
      CRISIS → LOW_VOL        (skip ELEVATED, NORMAL)

    Cases that do NOT fire (adjacent):
      LOW_VOL ↔ NORMAL
      NORMAL ↔ ELEVATED
      ELEVATED ↔ CRISIS
      same regime (no transition)

    The descriptive text is "Regime jump: {prior} → {current}". The continuous monitor
    augments with VIX values when available (e.g., "Regime jump: low-vol → crisis (VIX 12 → 38)");
    that augmentation is caller-side, not part of this primitive.
    """
```

Order encoding: a `_REGIME_INDEX` mapping locally translates `RegimeLabel` to integer rank for the abs-difference check.

#### 1.2 Multi-rule breach

```python
def evaluate_multi_rule_breach(
    *,
    risk_budget: RiskBudgetConsumption,
    config: BreachBehaviorConfig,
    rules_with_deferred_response: tuple[str, ...],
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect 3+ deferred-response rules simultaneously breaching.

    A "deferred-response rule" is any rule whose breach_response classification is
    deferred_to_pm (per the design's per-rule classification — sector concentration,
    net long, net short, gross, options delta, total short, etc.). The caller passes
    the canonical list rules_with_deferred_response derived from the rule registry's
    breach_response field (not duplicated here — single source of truth at the registry).

    A rule is "breaching" when its RiskBudgetEntry.zone is RiskZone.BLOCKED.

    Threshold: config.multi_rule_breach_simultaneous_deferred_rules_count (default 3).

    Returns (EmergencyTrigger.MULTI_RULE_BREACH, descriptive_text) if the count of breaching
    deferred rules ≥ threshold; else (None, None).

    Descriptive text: "Multi-rule breach: {count} deferred rules simultaneously over limit
    ({rule_id_1}, {rule_id_2}, {rule_id_3}, ...)".
    """
```

#### 1.3 Daily drawdown velocity

```python
def evaluate_daily_drawdown_velocity(
    *,
    drawdown_history: tuple[DrawdownSample, ...],
    daily_drawdown_limit_pct: float,
    config: BreachBehaviorConfig,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect rapid daily-drawdown acceleration crossing the threshold within the window.

    Trigger: daily drawdown crosses config.drawdown_velocity_threshold_pct_of_daily_limit (default 60%
    of the active daily limit) within config.drawdown_velocity_window_minutes (default 30) of the last
    check.

    Implementation: search drawdown_history for the most recent sample where intraday_drawdown_pct
    ≤ threshold (the "before-cross" sample) and the most recent sample where intraday_drawdown_pct
    ≥ threshold (the "after-cross" sample). If both exist and the time delta from before to after
    is ≤ window_minutes, the trigger fires.

    Threshold value: daily_drawdown_limit_pct × config.drawdown_velocity_threshold_pct_of_daily_limit / 100.0.

    drawdown_history is the time series of (timestamp, intraday_drawdown_pct) samples maintained
    by the continuous monitor; recent enough that the crossing is detectable. Caller bounds the
    history length (typically the last ~60 minutes of samples).

    Returns (EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY, descriptive_text) on fire, else (None, None).

    Descriptive text: "Daily drawdown velocity: crossed {threshold}% of daily limit ({threshold_pct}%
    drawdown) within {minutes_elapsed} minutes (window: {window_minutes} min)".
    """
```

The `DrawdownSample` typed input:

```python
class DrawdownSample(BaseModel):
    """A timestamped drawdown observation for the velocity-trigger evaluator."""

    model_config = ConfigDict(frozen=True)

    sampled_at: datetime                            # tz-aware
    intraday_drawdown_pct: float

    @field_validator("sampled_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "sampled_at must be timezone-aware"
            raise ValueError(msg)
        return v
```

#### 1.4 Margin call

```python
def evaluate_margin_call(
    *,
    margin_call_event: MarginCallEvent | None,
) -> tuple[EmergencyTrigger | None, str | None]:
    """Detect a broker-issued margin call.

    Trivial evaluator: if a MarginCallEvent is present (the broker has issued a call),
    the trigger fires. Otherwise it doesn't.

    The composer treats this trigger specially per the design's cooldown exception.

    Returns (EmergencyTrigger.MARGIN_CALL, descriptive_text) when margin_call_event is non-None,
    else (None, None).

    Descriptive text: "Margin call: ${amount} additional margin required".
    """
```

The `MarginCallEvent` typed input:

```python
class MarginCallEvent(BaseModel):
    """A broker-issued margin call awaiting handling."""

    model_config = ConfigDict(frozen=True)

    issued_at: datetime                             # tz-aware
    additional_margin_required_usd: float

    @field_validator("additional_margin_required_usd")
    @classmethod
    def _require_positive(cls, v: float) -> float:
        if v <= 0:
            msg = f"additional_margin_required_usd must be > 0; got {v}"
            raise ValueError(msg)
        return v

    @field_validator("issued_at")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "issued_at must be timezone-aware"
            raise ValueError(msg)
        return v
```

### 2. Composer

```python
def evaluate_emergency_invocation(
    *,
    now: datetime,                                   # tz-aware; the evaluation timestamp
    last_invocation_started_at: datetime,            # tz-aware; for minutes-since computation
    last_emergency_triggered_at: datetime | None,    # tz-aware or None; for cooldown check
    cooldown_minutes: int,                           # from config/guardrails.yaml emergency_invocation.cooldown_minutes
    normal_cadence_minutes: float,                   # from config/scheduler.yaml — populates EmergencyContext
    # Trigger inputs
    prior_regime_label: RegimeLabel,
    current_regime_label: RegimeLabel,
    risk_budget: RiskBudgetConsumption,
    rules_with_deferred_response: tuple[str, ...],
    drawdown_history: tuple[DrawdownSample, ...],
    daily_drawdown_limit_pct: float,
    margin_call_event: MarginCallEvent | None,
    config: BreachBehaviorConfig,
) -> EmergencyContext | None:
    """Evaluate all four triggers and return an EmergencyContext if one should fire.

    Cooldown rule: if last_emergency_triggered_at is non-None and (now - last_emergency_triggered_at)
    < cooldown_minutes, all triggers EXCEPT margin_call are suppressed.

    When multiple triggers fire simultaneously, priority order (highest first):
      1. MARGIN_CALL (bypasses cooldown; takes priority because it has an external deadline)
      2. REGIME_JUMP
      3. DAILY_DRAWDOWN_VELOCITY
      4. MULTI_RULE_BREACH

    The first firing trigger in priority order produces the EmergencyContext; subsequent firings
    are coalesced into the in-progress emergency per the design's coalescence rule (this primitive
    does not emit multiple contexts; the caller emits one emergency per evaluation cycle).

    Returns the EmergencyContext on fire; None when no trigger fires (or all triggers are
    suppressed by cooldown except margin_call which is absent).

    Raises:
        ValueError: when now <= last_invocation_started_at (zero or negative cadence delta —
            clock skew or scheduler bug); when timestamps are not tz-aware.
    """
```

Cooldown semantics:
- A margin call always fires regardless of cooldown.
- All other triggers fire only when `last_emergency_triggered_at is None` (no prior emergency this session) OR `now - last_emergency_triggered_at >= cooldown_minutes`.
- The "first emergency of a session" check uses `last_emergency_triggered_at is None`; the caller provides this — typically loaded from a session-state field maintained by the continuous monitor.

`minutes_since_last_invocation` is computed as `(now - last_invocation_started_at).total_seconds() / 60.0` and populates the `EmergencyContext.minutes_since_last_invocation` field.

### 3. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_emergency_triggers.py`:

#### Regime jump

- **Adjacent transitions do not fire:** LOW_VOL → NORMAL, NORMAL → ELEVATED, ELEVATED → CRISIS, NORMAL → LOW_VOL each return (None, None).
- **Skip-one-level fires:** LOW_VOL → ELEVATED, NORMAL → CRISIS, ELEVATED → LOW_VOL, CRISIS → NORMAL each return (REGIME_JUMP, descriptive_text). The text contains both regime labels.
- **Skip-multiple-levels fires:** LOW_VOL → CRISIS and CRISIS → LOW_VOL fire; descriptive text spans both.
- **Same-regime no-op:** prior == current returns (None, None).

#### Multi-rule breach

- **Below threshold (2 rules in BLOCKED) does not fire:** `risk_budget` has 2 entries with `zone=BLOCKED` and the rest in WARNING/PASS. Threshold default 3 → returns (None, None).
- **At threshold (3 rules in BLOCKED) fires:** 3 deferred rules in BLOCKED → returns (MULTI_RULE_BREACH, descriptive_text). Text lists all 3 rule_ids.
- **Above threshold (5 rules in BLOCKED) fires:** descriptive text lists all 5.
- **BLOCKED on a non-deferred rule does not count:** if `position_max_loss_equity_pct` is in BLOCKED but is `immediate_engine` classification, it does not contribute to the 3-rule count. (The `rules_with_deferred_response` argument filters; non-deferred rules in BLOCKED are excluded from the count.)
- **Custom threshold:** with `multi_rule_breach_simultaneous_deferred_rules_count=2`, 2 deferred rules in BLOCKED → fires.

#### Daily drawdown velocity

- **Crossing within window fires:** drawdown_history has samples [(t-25min, 0.4), (t-20min, 0.6), (t, 1.6)]; daily limit 2.5%; threshold = 2.5 × 0.6 = 1.5%. The sample at t-20 (0.6%) is below 1.5; the sample at t (1.6%) is above. Time delta 20 min ≤ 30 min window → fires. Descriptive text includes "1.5% within 20 minutes".
- **Crossing outside window does not fire:** before-cross at t-45min, after-cross at t. Delta 45 > 30 → does not fire.
- **No crossing does not fire:** all samples below threshold → no after-cross sample → does not fire.
- **Already above threshold for full window:** all samples ≥ threshold → no before-cross sample → does not fire (the trigger requires *crossing*, not steady-state above-threshold).
- **Custom window:** with `drawdown_velocity_window_minutes=15`, the 20-minute crossing in the first test case does NOT fire (20 > 15).
- **Custom threshold:** with `drawdown_velocity_threshold_pct_of_daily_limit=80`, threshold = 2.5 × 0.8 = 2.0%; crossing of 2.0 within window fires.

#### Margin call

- **Event present fires:** non-None `MarginCallEvent` → returns (MARGIN_CALL, descriptive_text containing the dollar amount).
- **Event absent does not fire:** None → returns (None, None).

#### Composer — single trigger

- **Single regime jump, no cooldown:** `last_emergency_triggered_at=None`, prior=LOW_VOL, current=CRISIS → returns EmergencyContext(trigger=REGIME_JUMP, ...).
- **Single multi-rule breach, no cooldown:** 3 BLOCKED deferred rules → MULTI_RULE_BREACH context.
- **Single drawdown velocity, no cooldown:** crossing within window → DAILY_DRAWDOWN_VELOCITY context.
- **Single margin call, no cooldown:** present → MARGIN_CALL context.

#### Composer — cooldown

- **Cooldown active, regime jump suppressed:** `last_emergency_triggered_at = now - 15 min`, cooldown = 30 → regime jump does not fire (None returned even though regime conditions met).
- **Cooldown elapsed, regime jump fires:** `last_emergency_triggered_at = now - 35 min` → fires.
- **Cooldown active, margin call fires anyway:** `last_emergency_triggered_at = now - 5 min`, margin call event present → fires (margin call bypasses cooldown).
- **First emergency of session (last_emergency_triggered_at=None):** all triggers eligible to fire.

#### Composer — priority

- **Margin call + regime jump simultaneously fire margin call:** returns context with `trigger=MARGIN_CALL` (margin call has highest priority).
- **Regime jump + multi-rule breach + drawdown velocity all fire:** returns context with `trigger=REGIME_JUMP` (priority 2).
- **Drawdown velocity + multi-rule breach:** returns context with `trigger=DAILY_DRAWDOWN_VELOCITY` (priority 3).
- **Multi-rule breach alone:** returns context with `trigger=MULTI_RULE_BREACH` (priority 4).

#### Composer — context fields

- **`minutes_since_last_invocation` computed correctly:** `now = last_invocation_started_at + 65 min` → `minutes_since_last_invocation = 65.0`. Tests use a fixed-tz-aware `datetime` pair.
- **`normal_cadence_minutes` passed through:** input `120` → context's field equals `120.0`.
- **`trigger_detail` matches the firing evaluator's descriptive text.**

#### Validation

- **Naive `now` rejected:** `datetime` without `tzinfo` raises `ValueError`.
- **`now <= last_invocation_started_at` rejected:** `ValueError` with the cadence-delta context.
- **Naive timestamps in `drawdown_history` rejected:** `DrawdownSample` post-validator catches.

#### Determinism

- **Pure function:** repeated calls with identical inputs produce equal results.
- **Frozen output:** assigning to a returned `EmergencyContext` field raises `ValidationError`.

#### Worked example — A10 reproduction

- **Crisis regime jump:** `prior=LOW_VOL`, `current=CRISIS`, no cooldown, no margin call, no multi-rule breach yet (the regime change is the first trigger). The composer returns `EmergencyContext(trigger=REGIME_JUMP, trigger_detail="Regime jump: low-vol → crisis", minutes_since_last_invocation=elapsed_minutes, normal_cadence_minutes=120.0)`.

Out of scope:
- The descriptive-text augmentation with VIX or other distillation-layer values — that's the continuous monitor's caller-side composition (it has access to the regime-classification inputs and can format richer detail strings).
- Persisting `last_emergency_triggered_at` across monitor restarts — the continuous monitor's session state owns this.
- Coalescing multiple triggers within the same evaluation cycle into a single emergency — the composer returns one trigger per call; the caller handles the "in-progress emergency receives all triggered conditions" logging at the activity-log layer.
- Emitting the emergency invocation to the scheduler — the continuous monitor or scheduler owns dispatch.
- Tracking which triggers fired-but-were-suppressed-by-cooldown for audit purposes — telemetry concern.

## Notes

**Why one composer + four evaluators rather than four separate primitives.** The four evaluators share data dependencies (timestamps, config, regime labels) and the cooldown / priority logic only makes sense in aggregate. A composer that calls the four evaluators internally keeps the public API simple (one call per evaluation cycle), and the per-evaluator functions remain individually testable without exposing a leaky internal-aggregation surface. Per `feedback_simplify_before_building.md`.

**Why margin-call has highest priority and bypasses cooldown.** The design is explicit:
> **Exception:** Margin calls override the cooldown — the broker's deadline is external and non-negotiable.

Any cooldown-suppressed margin call would let the broker's deadline pass without agent reasoning; that's worse than running an extra invocation. Priority 1 within concurrent triggers ensures the emergency context names the most-time-sensitive trigger.

**Why regime-jump is priority 2 (above velocity and multi-rule breach).** Regime jumps are the most informationally dense trigger — they signal a sudden environment shift that recontextualizes every position. Drawdown velocity and multi-rule breach are *symptoms* of a regime shift much of the time; firing the regime-jump label gives the agent the most useful prompt context.

**Per `feedback_avoid_numeric_anchors.md`,** all thresholds (60% drawdown velocity, 30-minute window, 3-rule breach count, 30-minute cooldown) come from configuration (`BreachBehaviorConfig` and `EmergencyInvocation` from `guardrails.yaml`). The function does not embed any numeric defaults.

**Per `feedback_no_inventing_component_names.md`,** the function names (`evaluate_regime_jump`, `evaluate_multi_rule_breach`, `evaluate_daily_drawdown_velocity`, `evaluate_margin_call`, `evaluate_emergency_invocation`) match the design's per-trigger and composer terminology directly. `MarginCallEvent` and `DrawdownSample` are package-internal value objects naming what they carry.

**Cross-feature integration with state-delivery.** State-delivery's story 06 currently defines `EmergencyTrigger` and `EmergencyContext` inline. When state-delivery dispatches after this story lands, those local declarations become imports from `breach_behavior.types`. The orchestrator surfaces this dependency at dispatch time. The schema and field set of the canonical types are designed to match exactly so the cutover is mechanical.

**Continuous-monitor session-state inputs.** The continuous monitor maintains `last_emergency_triggered_at` and the `drawdown_history` time series in its session state. The composer consumes these as typed inputs; this story does not own the session state or its persistence (that's execution-layer territory). The implementing subagent's tests use hand-constructed inputs.

**Why `multi_rule_breach` reads `RiskBudgetConsumption.zone` rather than re-classifying via `classify_zone`.** `RiskBudgetEntry.zone` is populated by the rules-and-limits production-populator (which calls `classify_zone` upstream). Re-classifying here would duplicate the work and risk drift between two callers. The primitive consumes the zone directly; the rules-and-limits work tree owns the source-of-truth classification.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/emergency_triggers.py` exists and defines:
  - `evaluate_regime_jump`
  - `evaluate_multi_rule_breach`
  - `evaluate_daily_drawdown_velocity`
  - `evaluate_margin_call`
  - `evaluate_emergency_invocation` (composer)
  - `DrawdownSample`, `MarginCallEvent` typed inputs
- [ ] All public functions and types are re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] All evaluators and the composer are pure functions (no I/O, no clock reads except inputs).
- [ ] Adjacent regime transitions (LOW_VOL ↔ NORMAL, NORMAL ↔ ELEVATED, ELEVATED ↔ CRISIS) do not fire `REGIME_JUMP`.
- [ ] Skip-one-level transitions in either direction (LOW_VOL → ELEVATED, NORMAL → CRISIS, ELEVATED → LOW_VOL, CRISIS → NORMAL) fire `REGIME_JUMP`.
- [ ] Skip-multiple-levels transitions (LOW_VOL → CRISIS, CRISIS → LOW_VOL) fire `REGIME_JUMP`.
- [ ] Same regime (no transition) does not fire.
- [ ] Multi-rule breach: 2 BLOCKED deferred rules with default threshold 3 → no fire; 3 → fire; 5 → fire (with all 5 rule_ids in description).
- [ ] Multi-rule breach excludes BLOCKED non-deferred rules from the count (only `rules_with_deferred_response` contribute).
- [ ] Drawdown velocity: crossing within window fires; crossing outside window does not; no crossing does not; sustained-above-threshold does not.
- [ ] Drawdown velocity uses configured window and threshold (custom values produce different fire conditions, verified by parametrized tests).
- [ ] Margin call event present fires; absent does not.
- [ ] Composer with `last_emergency_triggered_at=None` allows all triggers to fire.
- [ ] Composer with `last_emergency_triggered_at = now - 15 min` and cooldown 30 suppresses regime, drawdown velocity, and multi-rule breach but allows margin call.
- [ ] Composer cooldown elapsed (`now - last_emergency_triggered_at >= cooldown_minutes`) allows all triggers.
- [ ] Composer priority: margin call > regime jump > drawdown velocity > multi-rule breach. When multiple fire simultaneously, the highest-priority trigger populates the returned context.
- [ ] Composer's returned `EmergencyContext.minutes_since_last_invocation` equals `(now - last_invocation_started_at).total_seconds() / 60.0`.
- [ ] Composer rejects naive `now` (timezone-naive) with `ValueError`.
- [ ] Composer rejects `now <= last_invocation_started_at` with `ValueError`.
- [ ] `DrawdownSample` and `MarginCallEvent` enforce tz-aware timestamps; `MarginCallEvent.additional_margin_required_usd` enforces `> 0`.
- [ ] Returned `EmergencyContext` is frozen.
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs (or both `None`).
- [ ] A10 reproduction (LOW_VOL → CRISIS, no cooldown) returns an `EmergencyContext` with `trigger=REGIME_JUMP` and the documented descriptive-text format.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
