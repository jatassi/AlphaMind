# Counterfactual replay engine

A deterministic offline simulator that walks PM-rejected and PM-modified proposals through historical (or forward-accumulated) underlying price data to produce hypothetical realized P/L. The output is the substrate for measuring PM rejection accuracy, modification effectiveness, and per-anti-pattern detector accuracy in the feedback loop.

The engine is purely an analysis tool. It reads PM decisions and price data, writes counterfactual replay records, and otherwise sits outside the trading hot path. The engine estimates, not punishes; same-bar disambiguation carries a small documented bias (see Process Step 3), but otherwise the goal is unbiased measurement (per the parity-over-simulation principle echoed from [paper-evaluation-harness.md](paper-evaluation-harness.md)).

---

## Why the engine exists

The PM is a critical decision point: every analyst and strategist proposal passes through PM evaluation, and PM rejects, modifies, or approves based on the structured rubric in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). Approved proposals become trades with observable outcomes; rejected and modified-away proposals leave no trail. Without a way to recover their counterfactual outcomes, PM's evaluation quality is unmeasurable — we can see *what* PM rejected but never whether the rejections were right.

The engine closes that gap by simulating what would have happened if a rejected proposal had been approved (or if a modified proposal had been approved in its original un-modified form), producing a hypothetical realized P/L that joins to the originating PM envelope and feeds into the feedback loop.

---

## Scope (v1)

- **Equity proposals only.** Options and multi-leg strategy proposals are skipped with `replay_status = unevaluable` and `unevaluable_reason = unsupported_instrument`. A v2 with Black-Scholes-derived option pricing from underlying + IV surface is a possible follow-up; v1 takes the simpler path.
- **Rejections and modifications.** Rejections are replayed in the form the analyst originally proposed (the form PM saw). Modifications are replayed in their *original un-modified form* alongside the actual modified-form trade — yielding the modification effectiveness measure (modified-form actual outcome vs. original-form counterfactual outcome).
- **Daily batch + on-demand.** A scheduled APScheduler job in the pipeline process runs the engine once daily after market close. The engine can also be invoked on-demand by the operator (via the [command center](../command-center.md) or a feedback-review skill) for ad-hoc analysis. On-demand runs are idempotent — replays already present are skipped.

---

## Inputs

1. **Proposal queue.** PM decisions whose evaluation horizon has elapsed: activity log entries with `event_type = pm_decision`, verdict in (`reject`, `approve_with_modification`), and `evaluation_due_at < now`, where `evaluation_due_at = rejection_timestamp + entry_window_duration + max(time_stop_horizon, target_estimated_horizon)`. The engine maintains its own queue cursor so re-runs don't reprocess settled replays.
2. **Historical underlying price stream.** Minute-bar (or finer if available) data for the proposal's underlying ticker, sourced from the data layer over the window `[rejection_timestamp, rejection_timestamp + entry_window + max_time_stop]`.
3. **Paper-evaluation harness primitives.** Spread, impact, and regulatory-fee estimation per [paper-evaluation-harness.md § Sub-models](paper-evaluation-harness.md#sub-models), reused so counterfactual P/L applies the same drag estimation as actual paper P/L. This makes the two directly comparable in aggregated metrics.

---

## Process per proposal

### Step 1 — Eligibility check

If the proposal's instrument type is not equity: record `replay_status = unevaluable`, `unevaluable_reason = unsupported_instrument`, and stop.

If historical price data for the underlying is not available over the required window: record `replay_status = unevaluable`, `unevaluable_reason = data_missing`, and stop.

If a corporate action on the underlying fired within the replay window: record `replay_status = unevaluable`, `unevaluable_reason = corporate_action_in_window`, and stop. Modeling cost-basis adjustment for spin-offs, splits, and mergers in counterfactual flight is not worth the complexity given the rarity at the 4–72h horizon.

### Step 2 — Entry simulation

Walk forward through the price stream from `entry_window_start` to `entry_window_end`. For each bar:

- **Limit order** — long buy: bar low ≤ limit, fill at limit on first touch. Short sell: bar high ≥ limit, fill at limit on first touch.
- **Market order** — assume fill at the next bar's open price after the entry timestamp.

Limit orders fill in full at the touch; partial fills are not modeled. Apply entry slippage and fees per harness primitives. Record `entered = true`, `entry_price`, `entry_timestamp`, `entry_slippage`, `entry_fees`.

If no fill occurs within the entry window: record `entered = false`, `exit_leg = entry_window_expired_unfilled`, `realized_pl = 0`. Stop. The proposal is fully evaluated — the counterfactual answer is "would not have entered."

### Step 3 — Bracket simulation

Walk forward from the entry timestamp through the price stream. For each bar, check trigger conditions:

1. **Target hit** — long: bar high ≥ target. Short: bar low ≤ target.
2. **Stop hit** — long: bar low ≤ stop. Short: bar high ≥ stop.
3. **Time stop expired** — bar timestamp ≥ time-stop timestamp.

**Same-bar target-and-stop ambiguity.** If both target and stop trigger conditions are met within the same bar, the engine assumes the stop fills first. This is a conservative bias toward worse counterfactual P/L. The bias is acknowledged in the engine documentation and surfaced as a metric caveat — it only matters in narrow same-bar overlap cases that are themselves rare. The bar resolution we work at (minute, or finer) keeps the affected fraction small.

**Trigger price recording.** Price-based exits record at the trigger price (target or stop level), not the bar's actual extreme — the assumption is the order fills at the trigger as it's crossed. Time-stop exits record at then-current price (bar open at the time-stop timestamp).

Apply exit slippage and fees per harness primitives. Record `exit_leg`, `exit_price`, `exit_timestamp`, `exit_slippage`, `exit_fees`.

### Step 4 — P/L computation

```
realized_pl = (exit_price - entry_price) × quantity × direction_sign
              - entry_slippage - entry_fees
              - exit_slippage - exit_fees
```

Where `direction_sign = +1` for long, `-1` for short. Shorts assume zero borrow rate, matching the harness primitives the engine reuses (universe is ETB-only).

### Step 5 — Confidence determination

Classify the replay's confidence using the same convention as the paper harness:

- **High** — full bar coverage over entry and bracket windows; liquidity within typical-day envelope per the data layer's rolling baselines; spread within typical-day envelope; no same-bar trigger ambiguity.
- **Medium** — minor data gaps that don't affect trigger determination; or unusual but unambiguous price movement; or same-bar ambiguity that doesn't change the exit_leg classification.
- **Low** — significant data gaps; thin liquidity affecting trigger price reliability; same-bar ambiguity that does affect the exit_leg classification; or any condition that introduces material uncertainty in the realized P/L.

Low-confidence replays are persisted but **excluded from aggregated PM accuracy metrics at query time**. The persistence captures them for diagnostic review (the operator can drill into a low-confidence replay to understand why it was unreliable); the aggregation discipline keeps noise out of trend signals.

---

## Output

A `counterfactual_replays` entity in [state-persistence.md](state-persistence.md), one record per replay attempt, joined to the originating PM envelope via `pm_decision_envelope_id`. Schema authoritatively specified there.

The engine writes one record per (proposal, replay_kind) tuple. Modifications produce one `modification_original_form` record per modification (the "what if PM had not modified" question); rejections produce one `rejection` record (the "what if PM had not rejected" question).

---

## On-demand invocation

The operator can trigger the engine on-demand via:
- Command center action — runs a one-shot pass over the queue
- Feedback-review skill — invoked at session start to ensure counterfactuals are fresh before opening a review

On-demand runs share the same eligibility cursor as the daily batch and are idempotent: replays already present in the table are skipped. Re-evaluating a replay with a newer engine version requires explicit purge; the engine never silently overwrites.

---

## Engine versioning

The `replay_engine_version` field on each record captures the algorithm version that produced it. Versions advance when the algorithm itself changes — better same-bar disambiguation, a new confidence classifier, support for a new instrument type. Older replays remain valid records of what the previous-version engine computed but are tagged with that version. The feedback loop's aggregation queries can filter to a single version when consistency matters; cross-version comparisons are an explicit operator choice, not an accident.

---

## Activation

The engine runs in both paper and live modes. In paper mode, it uses the harness primitives for slippage and fee drag (matching how actual paper P/L is computed). In live mode, it uses the same primitives — the slippage estimate is now an estimate of what *would* have happened if the rejection had been an approval, which is the correct semantics regardless of mode.

The engine is read-only on the trading state, so there is no fail-closed concern: an engine failure does not affect any pipeline invocation or monitor responsibility. A failed run aborts and is retried on the next daily batch tick; the operator is alerted via the standard alerting channel if failures persist.

---

## Dependencies

- [Paper evaluation harness](paper-evaluation-harness.md) — provides the spread, impact, and regulatory-fee primitives reused for slippage and fee drag, ensuring counterfactual and actual P/L are computed on the same drag basis
- [State persistence](state-persistence.md) — defines the `counterfactual_replays` entity that the engine populates, the `pm_decision` activity log events that supply the proposal queue, and the `process_lifetimes` and `agent_calls` provenance the engine can join against for confounder analysis
- [Portfolio manager](../04-decision-layer/portfolio-manager.md) — defines the PM envelope structure (verdict, evaluation criteria, modifications, anti_patterns_identified) that supplies replay inputs
- [Configuration management](../configuration-management.md) — provides the scheduler hook for the daily batch run (a new scheduled trigger in `scheduler.yaml` runs the engine once per day after close)
- [Command center](../command-center.md) — surfaces the on-demand trigger and renders aggregated counterfactual results in the Quality and feedback view group
