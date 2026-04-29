---
status: in_progress
completed_date:
commit_id:
---

# 01e — Thesis performance review trigger

## Goal

Land the predicate function for the micro-profile-only **thesis performance review** described in `rules-and-limits.md § Profile: Micro`. The trigger fires when one of two conditions is met: (a) the system has completed 20 theses since the last review, or (b) cumulative realized losses since the last review exceed 15% of starting capital ($225 at the $1,500 micro tier). Not a mechanical halt — the design calls it a "system pause where the operator evaluates the track record." This story ships the *predicate*; the operator pause UI, the activity-log emission, and the post-review reset semantics are owned by the pipeline / command-center integration work that consumes the predicate.

The trigger applies *only to the micro profile* — it exists because micro's risk priority is "signal quality" (validating whether the analytical framework produces positive expectancy before scaling capital), and the review is the operator-checkpoint that converts trade outcomes into a graduate / iterate / abort decision. The other three profiles use drawdown-based safeguards instead; on those profiles the trigger is reported as not-applicable.

## Reading

- `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile: Micro — the trigger definition: "every 20 completed trades, or when cumulative realized losses exceed 15% of starting capital ($225), whichever comes first"
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile: Micro — purpose of the trigger ("Answers 'is the framework generating positive expectancy?' before too much capital is consumed")
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Sequential validation lifecycle — the trigger is named as part of the micro→small graduation gate (the operator's positive determination on the trigger satisfies one of the mechanical-floor criteria)
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile comparison summary — "Thesis review trigger" row showing micro fires it, the other three do not
- `docs/design/feedback-loop.md` — context for what an operator review consists of (process-quality metrics, conviction calibration, etc.); not directly consumed by this story but useful for downstream integration
- `src/alphamind/config/models/profiles.py` — `ProfileConfig.risk_priority: RiskPriority`. The micro profile carries `risk_priority: signal_quality`; the function keys off this (rather than the profile name) because the named identifier is what the design doc treats as the marker. **This story extends `ProfileConfig` with an optional `thesis_performance_review` block** — see § Scope below.
- `config/profiles/micro.yaml` — the shipped profile (confirms `risk_priority: signal_quality`); this story adds a `thesis_performance_review:` section to it.
- `config/profiles/{small,medium,large}.yaml` — the other three shipped profiles; remain without a `thesis_performance_review:` section (the field defaults to `None` for non-micro profiles).

## Depends on

None. The configuration-management feature has shipped `ProfileConfig` and `RiskPriority`. This story extends `ProfileConfig` with one new optional field; the extension is additive (no breaking change to existing callers).

## Scope

In scope:

### 1. `ProfileConfig` extension

Extend `src/alphamind/config/models/profiles.py` with one new optional field on `ProfileConfig`:

```python
class ThesisPerformanceReviewConfig(BaseModel):
    """Per-profile threshold pair for the thesis-performance-review trigger.

    Populated for the micro profile only; absent (None) on small/medium/large.
    The trigger fires for the operator-pause workflow when either gate is met.
    """
    model_config = ConfigDict(frozen=True)

    trade_count_threshold: int = Field(gt=0)              # design: 20 for micro
    loss_pct_threshold: float = Field(gt=0.0, le=1.0)     # design: 0.15 (= 15% of starting capital) for micro


class ProfileConfig(BaseModel):
    # ... existing fields ...
    thesis_performance_review: ThesisPerformanceReviewConfig | None = None
```

The field is optional and defaults to `None` so the configuration-management feature's existing semantics for non-micro profiles are preserved without YAML changes there.

### 2. Update `config/profiles/micro.yaml`

Add a `thesis_performance_review:` block:

```yaml
thesis_performance_review:
  trade_count_threshold: 20
  loss_pct_threshold: 0.15
```

The other three shipped profile YAMLs (`small.yaml`, `medium.yaml`, `large.yaml`) are not modified; the field defaults to `None` for them.

### 3. The predicate function

`src/alphamind/risk_guardrails/rules_and_limits/thesis_performance_review.py` defines:

- `ReviewTriggerCause` (StrEnum): members `trade_count` (the 20-completed-theses gate) and `cumulative_loss` (the 15%-of-starting-capital gate). The enum exists so callers (and the activity-log entry the consumer will write) can render the cause in a stable form.
- `ReviewTriggerSignal` (frozen, slots dataclass) with these fields:
  - `applicable: bool` — `True` iff `profile.thesis_performance_review is not None` AND `profile.risk_priority == RiskPriority.signal_quality`. The two checks are equivalent on the shipped profile set (only micro carries both); the function asserts the equivalence for defense-in-depth.
  - `triggered: bool` — `True` iff applicable and at least one cause fires. Always `False` when not applicable.
  - `causes: tuple[ReviewTriggerCause, ...]` — empty when not triggered; ordered as `(trade_count, cumulative_loss)` when both fire.
  - `completed_theses_since_last_review: int` — echo of input.
  - `cumulative_realized_loss_usd: float` — echo of input. Convention: positive value means a loss (e.g., `225.0` represents $225 of cumulative losses); zero means no losses; a negative value would represent a net realized gain — the function treats negative inputs as "no loss" for the cumulative-loss gate.
  - `starting_capital_usd: float` — echo of input.
  - `trade_count_threshold: int` — value sourced from `profile.thesis_performance_review.trade_count_threshold` when applicable; `0` when not applicable (the field is unused).
  - `loss_threshold_usd: float` — computed: `starting_capital_usd * profile.thesis_performance_review.loss_pct_threshold` when applicable; `0.0` when not applicable.
- `evaluate_thesis_performance_review_trigger(*, profile: ProfileConfig, completed_theses_since_last_review: int, cumulative_realized_loss_usd: float, starting_capital_usd: float) -> ReviewTriggerSignal` — the predicate. Notably **no override parameters for the thresholds**: callers wanting to test alternate thresholds construct synthetic `ProfileConfig` instances with custom `thesis_performance_review` blocks. This eliminates two function-signature defaults (`trade_count_threshold`, `loss_pct_threshold`) that previously hardcoded the design values per `feedback_avoid_numeric_anchors`.
- Predicate behavior:
  1. If `profile.thesis_performance_review is None` (non-applicable profile): return `ReviewTriggerSignal(applicable=False, triggered=False, causes=(), trade_count_threshold=0, loss_threshold_usd=0.0, ...)` with the input echoes preserved.
  2. Otherwise: applicable=True. Compute `loss_threshold_usd = starting_capital_usd * profile.thesis_performance_review.loss_pct_threshold`. Build the causes tuple by checking each gate in order:
     - If `completed_theses_since_last_review >= profile.thesis_performance_review.trade_count_threshold`: append `ReviewTriggerCause.trade_count`.
     - If `cumulative_realized_loss_usd >= loss_threshold_usd`: append `ReviewTriggerCause.cumulative_loss`.
  3. Return `triggered = bool(causes)` and the populated tuple.
- Validation:
  - Raise `ValueError` if `completed_theses_since_last_review < 0` (a negative trade count is structural).
  - Raise `ValueError` if `starting_capital_usd <= 0` (zero or negative starting capital invalidates the threshold computation).
  - Raise `ValueError` if `profile.thesis_performance_review is not None` AND `profile.risk_priority != RiskPriority.signal_quality` (defensive — the two markers should agree; a mismatch is a YAML configuration error worth surfacing).
  - Negative `cumulative_realized_loss_usd` is *not* an error — it represents a net gain. Treat as "no loss" for the gate (the gate fires when losses *exceed* the threshold; net gains never fire).
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `ReviewTriggerCause`, `ReviewTriggerSignal`, `evaluate_thesis_performance_review_trigger`. Combine with sibling stories' re-exports via the natural union merge.
### 4. Tests

Unit tests covering:

- **Non-micro profile** (`profile.thesis_performance_review is None`). `evaluate_thesis_performance_review_trigger(profile=<small>, completed_theses_since_last_review=20, cumulative_realized_loss_usd=300.0, starting_capital_usd=10000.0)` returns `applicable=False`, `triggered=False`, `causes=()`, `trade_count_threshold=0`, `loss_threshold_usd=0.0`. Repeat for medium and large.
- **Micro, no trigger.** `profile=<micro>` (with `thesis_performance_review.trade_count_threshold=20, loss_pct_threshold=0.15`), 19 trades, $0 loss, $1500 starting → `applicable=True`, `triggered=False`, `causes=()`.
- **Micro, trade_count fires.** 20 trades, $0 loss → `triggered=True`, `causes=(trade_count,)`.
- **Micro, trade_count fires above threshold.** 21 trades, $0 loss → `triggered=True`, `causes=(trade_count,)`.
- **Micro, cumulative_loss fires at exactly $225.** 19 trades, $225 loss, $1500 starting → `triggered=True`, `causes=(cumulative_loss,)`.
- **Micro, cumulative_loss does not fire below $225.** 19 trades, $224.99 loss → `triggered=False`, `causes=()`.
- **Micro, both fire.** 20 trades, $300 loss, $1500 starting → `triggered=True`, `causes=(trade_count, cumulative_loss)` in that order.
- **Micro, net realized gain.** 19 trades, `cumulative_realized_loss_usd = -100.0` (a gain), $1500 starting → `triggered=False`, `causes=()`.
- **Profile-sourced thresholds.** `ReviewTriggerSignal.trade_count_threshold == profile.thesis_performance_review.trade_count_threshold` and `loss_threshold_usd == starting_capital_usd * profile.thesis_performance_review.loss_pct_threshold`. Verified for the shipped micro profile (20, 0.15).
- **Custom thresholds.** A synthetic `ProfileConfig` with `thesis_performance_review=ThesisPerformanceReviewConfig(trade_count_threshold=10, loss_pct_threshold=0.10)` fires at 10 trades AND fires at 10% of starting capital. Demonstrates that operators tune the thresholds via YAML, not via function parameters.
- **Marker-mismatch defensive raise.** A synthetic `ProfileConfig` with `risk_priority=concentration_management` (small/medium/large) AND `thesis_performance_review=ThesisPerformanceReviewConfig(...)` (a configuration error — only micro should carry both) raises `ValueError` naming the mismatch.
- **Returned dataclass content.** Asserts every field of `ReviewTriggerSignal` carries the expected value (input echoes, computed `loss_threshold_usd`).
- **Validation errors.** `completed_theses_since_last_review = -1` raises `ValueError`. `starting_capital_usd = 0` raises. `starting_capital_usd = -100` raises.
- **`ThesisPerformanceReviewConfig` Pydantic validation.** Construction with `trade_count_threshold=0` or `loss_pct_threshold=-0.05` raises Pydantic `ValidationError` (the field constraints handle this — story does not duplicate at the predicate layer).
- **YAML round-trip.** The shipped `config/profiles/micro.yaml` parses into a `ProfileConfig` whose `thesis_performance_review` field carries the documented values; `small.yaml`, `medium.yaml`, `large.yaml` parse with `thesis_performance_review is None`.

Out of scope:

- Tracking `completed_theses_since_last_review` and `cumulative_realized_loss_usd` over time — those counters are owned by the consumer (likely the pipeline reading from the activity log). The predicate is stateless.
- The "system pause" semantics — what gets blocked, how the operator clears the pause, the resume action — owned by the pipeline / command-center integration.
- Activity-log entry emission for `thesis_performance_review_due` — owned by the pipeline integration.
- The post-review reset of the counters — owned by the consumer.
- Operator UI for the review (process-quality metrics, calibration spread, etc.) — owned by the feedback-loop dashboard.
- Extending the predicate to other profiles — the design explicitly scopes this to micro; if a future profile wants a similar trigger, that's a design-doc revision plus a new story.

## Notes

**Keying off `risk_priority`, not profile name.** `RiskPriority.signal_quality` is the design-doc-named marker for the micro tier ("Risk priority is signal quality"). Naming is more durable than the `Profile.micro` enum member — if a future profile rename or split occurs, the marker stays attached to the right semantic. This also keeps the function decoupled from the `Profile` enum's specific membership.

**Causes-as-tuple, not single cause.** The design says "whichever comes first" referring to the temporal order in which the gates fire as the system runs. The predicate is stateless and can't observe temporal order — it sees both gates' current state. Returning a tuple of causes lets the consumer render the right context: if both fire, the activity log can record both reasons; if only one, the cause name is the failure mode. The tuple is ordered `(trade_count, cumulative_loss)` to preserve a canonical rendering.

**Inclusive `>=` on both gates.** Exactly 20 trades fires; exactly $225 loss at $1500 starting fires. Matches the operator-natural reading of the design doc ("every 20 completed trades, or when cumulative realized losses exceed 15%"). The "exceed" is treated inclusively because the threshold values are the trigger boundaries, not interior values.

**Negative `cumulative_realized_loss_usd` as a net gain, not an error.** A net realized gain means the system is doing well; the loss-based gate trivially does not fire. Surfacing this case as `triggered=False` (rather than raising) lets the predicate be called uniformly across the system's lifecycle without consumers wrapping it in conditional logic ("only call if we're in losing territory").

**Thresholds in profile config, not function defaults.** Per `feedback_avoid_numeric_anchors`, the `20` and `0.15` thresholds live in `config/profiles/micro.yaml`'s `thesis_performance_review:` block, not as default values on the function signature. The function reads from `profile.thesis_performance_review` and the predicate has no override parameters. Operators or tests that want to dry-run with different values construct a synthetic `ProfileConfig` with a custom `ThesisPerformanceReviewConfig`. Lifting the values into config keeps the threshold surface uniform with the rest of the package (per-profile YAML is the canonical knob location for per-profile policy).

**Why no activity-log entry factory.** The profile-switch handler (story 01d) ships a factory because the activity-log entry is part of the operator action's contract — *every* switch produces an entry. The thesis-performance-review trigger is a *signal* the consumer interprets — sometimes by emitting an activity log entry, sometimes (depending on operator preferences and the integration design) by waiting for the next pipeline boundary. The shape of the eventual activity-log entry is the consumer's contract; this story ships only the predicate.

**`loss_pct_threshold = 0.15` as a float, not a percentage point.** The YAML field is a multiplicative fraction (`0.15` for 15%). The design doc writes "15%" because operator-readable percent renders are conventional; the function's threshold-computation arithmetic uses the fraction directly. The Pydantic field constraint `Field(gt=0.0, le=1.0)` keeps the value in fraction-space (rejecting an accidental `15.0` operator input).

## Acceptance criteria

- [ ] `src/alphamind/config/models/profiles.py` defines `ThesisPerformanceReviewConfig` (frozen Pydantic v2 model) and adds `thesis_performance_review: ThesisPerformanceReviewConfig | None = None` to `ProfileConfig`.
- [ ] `config/profiles/micro.yaml` carries a `thesis_performance_review:` block with `trade_count_threshold: 20` and `loss_pct_threshold: 0.15`.
- [ ] `config/profiles/{small,medium,large}.yaml` are not modified; they parse with `thesis_performance_review is None`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/thesis_performance_review.py` exists and defines `ReviewTriggerCause`, `ReviewTriggerSignal`, `evaluate_thesis_performance_review_trigger`.
- [ ] `ReviewTriggerSignal` is a `dataclass(frozen=True, slots=True)`.
- [ ] `ReviewTriggerCause` is a `StrEnum` with members `trade_count` and `cumulative_loss`.
- [ ] The predicate signature is `evaluate_thesis_performance_review_trigger(*, profile: ProfileConfig, completed_theses_since_last_review: int, cumulative_realized_loss_usd: float, starting_capital_usd: float) -> ReviewTriggerSignal` — no threshold-override parameters.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports the three names.
- [ ] A unit test asserts non-micro profiles (small, medium, large) return `applicable=False, triggered=False, causes=()` regardless of input.
- [ ] A unit test asserts micro at 19 trades / $0 loss returns `triggered=False`.
- [ ] A unit test asserts micro at 20 trades / $0 loss returns `triggered=True` with `causes=(ReviewTriggerCause.trade_count,)`.
- [ ] A unit test asserts micro at 19 trades / $225 loss / $1500 starting returns `triggered=True` with `causes=(ReviewTriggerCause.cumulative_loss,)`.
- [ ] A unit test asserts micro at 19 trades / $224.99 loss / $1500 starting returns `triggered=False`.
- [ ] A unit test asserts micro at 20 trades / $300 loss returns `triggered=True` with `causes=(ReviewTriggerCause.trade_count, ReviewTriggerCause.cumulative_loss)` in that order.
- [ ] A unit test asserts micro with a negative `cumulative_realized_loss_usd` (net gain) does not fire the cumulative-loss gate.
- [ ] A unit test asserts the predicate sources thresholds from `profile.thesis_performance_review` — synthetic `ProfileConfig` with `(10, 0.10)` fires at 10 trades and at 10% of starting capital.
- [ ] A unit test asserts the marker-mismatch defensive raise — `risk_priority != signal_quality` AND `thesis_performance_review is not None` raises `ValueError`.
- [ ] A unit test asserts `completed_theses_since_last_review = -1` raises `ValueError`.
- [ ] A unit test asserts `starting_capital_usd = 0` and `starting_capital_usd = -100` both raise `ValueError`.
- [ ] A unit test asserts `ThesisPerformanceReviewConfig(trade_count_threshold=0)` and `ThesisPerformanceReviewConfig(loss_pct_threshold=-0.05)` both raise Pydantic `ValidationError`.
- [ ] A unit test asserts the shipped `config/profiles/micro.yaml` round-trips into a `ProfileConfig` whose `thesis_performance_review` carries `(20, 0.15)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
