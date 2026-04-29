---
status: not_started
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
- `src/alphamind/config/models/profiles.py` — `ProfileConfig.risk_priority: RiskPriority`. The micro profile carries `risk_priority: signal_quality`; the function keys off this (rather than the profile name) because the named identifier is what the design doc treats as the marker.
- `config/profiles/micro.yaml` — the shipped profile (confirms `risk_priority: signal_quality`)

## Depends on

None. The configuration-management feature has shipped `ProfileConfig` and `RiskPriority`.

## Scope

In scope:

- `src/alphamind/risk_guardrails/rules_and_limits/thesis_performance_review.py` defining:
  - `ReviewTriggerCause` (StrEnum): members `trade_count` (the 20-completed-theses gate) and `cumulative_loss` (the 15%-of-starting-capital gate). The enum exists so callers (and the activity-log entry the consumer will write) can render the cause in a stable form.
  - `ReviewTriggerSignal` (frozen, slots dataclass) with these fields:
    - `applicable: bool` — `True` iff the profile's `risk_priority == RiskPriority.signal_quality`. The other profiles return `applicable=False` and `triggered=False` regardless of inputs.
    - `triggered: bool` — `True` iff applicable and at least one cause fires. Always `False` when not applicable.
    - `causes: tuple[ReviewTriggerCause, ...]` — empty when not triggered; ordered as `(trade_count, cumulative_loss)` when both fire.
    - `completed_theses_since_last_review: int` — echo of input.
    - `cumulative_realized_loss_usd: float` — echo of input. Convention: positive value means a loss (e.g., `225.0` represents $225 of cumulative losses); zero means no losses; a negative value would represent a net realized gain — the function treats negative inputs as "no loss" for the cumulative-loss gate.
    - `starting_capital_usd: float` — echo of input.
    - `trade_count_threshold: int` — threshold used (default 20).
    - `loss_threshold_usd: float` — computed: `starting_capital_usd * loss_pct_threshold`.
  - `evaluate_thesis_performance_review_trigger(*, profile: ProfileConfig, completed_theses_since_last_review: int, cumulative_realized_loss_usd: float, starting_capital_usd: float, trade_count_threshold: int = 20, loss_pct_threshold: float = 0.15) -> ReviewTriggerSignal` — the predicate.
  - Predicate behavior:
    1. If `profile.risk_priority != RiskPriority.signal_quality`: return `ReviewTriggerSignal(applicable=False, triggered=False, causes=(), ...)` with the input echoes preserved.
    2. Otherwise: applicable=True. Compute `loss_threshold_usd = starting_capital_usd * loss_pct_threshold`. Build the causes tuple by checking each gate in order:
       - If `completed_theses_since_last_review >= trade_count_threshold`: append `ReviewTriggerCause.trade_count`.
       - If `cumulative_realized_loss_usd >= loss_threshold_usd`: append `ReviewTriggerCause.cumulative_loss`.
    3. Return `triggered = bool(causes)` and the populated tuple.
  - Validation:
    - Raise `ValueError` if `completed_theses_since_last_review < 0` (a negative trade count is structural).
    - Raise `ValueError` if `starting_capital_usd <= 0` (zero or negative starting capital invalidates the threshold computation).
    - Raise `ValueError` if `trade_count_threshold <= 0` or `loss_pct_threshold <= 0`.
    - Negative `cumulative_realized_loss_usd` is *not* an error — it represents a net gain. Treat as "no loss" for the gate (the gate fires when losses *exceed* the threshold; net gains never fire).
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `ReviewTriggerCause`, `ReviewTriggerSignal`, `evaluate_thesis_performance_review_trigger`. Combine with sibling stories' re-exports via the natural union merge.
- Unit tests covering:
  - **Non-micro profile.** `evaluate_thesis_performance_review_trigger(profile=<small>, completed_theses_since_last_review=20, cumulative_realized_loss_usd=300.0, starting_capital_usd=10000.0)` returns `applicable=False`, `triggered=False`, `causes=()`. Repeat for medium and large.
  - **Micro, no trigger.** `profile=<micro>`, 19 trades, $0 loss, $1500 starting → `applicable=True`, `triggered=False`, `causes=()`.
  - **Micro, trade_count fires.** 20 trades, $0 loss → `triggered=True`, `causes=(trade_count,)`.
  - **Micro, trade_count fires above threshold.** 21 trades, $0 loss → `triggered=True`, `causes=(trade_count,)`.
  - **Micro, cumulative_loss fires at exactly $225.** 19 trades, $225 loss, $1500 starting → `triggered=True`, `causes=(cumulative_loss,)`.
  - **Micro, cumulative_loss does not fire below $225.** 19 trades, $224.99 loss → `triggered=False`, `causes=()`.
  - **Micro, both fire.** 20 trades, $300 loss, $1500 starting → `triggered=True`, `causes=(trade_count, cumulative_loss)` in that order.
  - **Micro, net realized gain.** 19 trades, `cumulative_realized_loss_usd = -100.0` (a gain), $1500 starting → `triggered=False`, `causes=()`.
  - **Default thresholds match the design.** `trade_count_threshold` defaults to 20; `loss_pct_threshold` defaults to 0.15. Asserted by an explicit test that calls the function without overriding either argument and confirms the resulting `ReviewTriggerSignal.trade_count_threshold == 20` and `loss_threshold_usd == starting_capital_usd * 0.15`.
  - **Custom thresholds.** Passing `trade_count_threshold=10` fires at 10 trades; passing `loss_pct_threshold=0.10` fires at 10% of starting capital.
  - **Returned dataclass content.** Asserts every field of `ReviewTriggerSignal` carries the expected value (input echoes, computed `loss_threshold_usd`).
  - **Validation errors.** `completed_theses_since_last_review = -1` raises `ValueError`. `starting_capital_usd = 0` raises. `starting_capital_usd = -100` raises. `trade_count_threshold = 0` raises. `loss_pct_threshold = -0.05` raises.

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

**Default thresholds in the function signature.** Matching the design's "20 completed trades" and "15% of starting capital." Operators or tests that want to dry-run with different values pass the override; the defaults make the common-case call site terse.

**Why no activity-log entry factory.** The profile-switch handler (story 01d) ships a factory because the activity-log entry is part of the operator action's contract — *every* switch produces an entry. The thesis-performance-review trigger is a *signal* the consumer interprets — sometimes by emitting an activity log entry, sometimes (depending on operator preferences and the integration design) by waiting for the next pipeline boundary. The shape of the eventual activity-log entry is the consumer's contract; this story ships only the predicate.

**`loss_pct_threshold = 0.15` as a float, not a percentage point.** The function's argument is the multiplicative fraction (`0.15` for 15%). The design doc writes "15%" because operator-readable percent renders are conventional; the function's threshold-computation arithmetic uses the fraction directly. Tests assert `loss_threshold_usd == starting_capital_usd * 0.15`.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/rules_and_limits/thesis_performance_review.py` exists and defines `ReviewTriggerCause`, `ReviewTriggerSignal`, `evaluate_thesis_performance_review_trigger`.
- [ ] `ReviewTriggerSignal` is a `dataclass(frozen=True, slots=True)`.
- [ ] `ReviewTriggerCause` is a `StrEnum` with members `trade_count` and `cumulative_loss`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports the three names.
- [ ] A unit test asserts non-micro profiles (small, medium, large) return `applicable=False, triggered=False, causes=()` regardless of input.
- [ ] A unit test asserts micro at 19 trades / $0 loss returns `triggered=False`.
- [ ] A unit test asserts micro at 20 trades / $0 loss returns `triggered=True` with `causes=(ReviewTriggerCause.trade_count,)`.
- [ ] A unit test asserts micro at 19 trades / $225 loss / $1500 starting returns `triggered=True` with `causes=(ReviewTriggerCause.cumulative_loss,)`.
- [ ] A unit test asserts micro at 19 trades / $224.99 loss / $1500 starting returns `triggered=False`.
- [ ] A unit test asserts micro at 20 trades / $300 loss returns `triggered=True` with `causes=(ReviewTriggerCause.trade_count, ReviewTriggerCause.cumulative_loss)` in that order.
- [ ] A unit test asserts micro with a negative `cumulative_realized_loss_usd` (net gain) does not fire the cumulative-loss gate.
- [ ] A unit test asserts default `trade_count_threshold == 20` and `loss_pct_threshold == 0.15` produce `loss_threshold_usd == starting_capital_usd * 0.15` in the returned signal.
- [ ] A unit test asserts custom `trade_count_threshold=10` fires at 10 trades against the micro profile.
- [ ] A unit test asserts custom `loss_pct_threshold=0.10` fires at 10% of starting capital against the micro profile.
- [ ] A unit test asserts `completed_theses_since_last_review = -1` raises `ValueError`.
- [ ] A unit test asserts `starting_capital_usd = 0` and `starting_capital_usd = -100` both raise `ValueError`.
- [ ] A unit test asserts `trade_count_threshold = 0` and `loss_pct_threshold = -0.05` both raise `ValueError`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
