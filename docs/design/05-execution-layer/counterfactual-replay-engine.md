# Counterfactual replay engine

A deterministic offline simulator that walks PM-rejected and PM-modified proposals through historical underlying price data to produce hypothetical realized P/L. Substrate for measuring PM rejection accuracy, modification effectiveness, and per-anti-pattern detector accuracy in the feedback loop.

The engine is an analysis tool — reads PM decisions and price data, writes counterfactual replay records, sits outside the trading hot path. It estimates, not punishes; same-bar disambiguation carries a small documented bias (see Process Step 3), otherwise unbiased measurement (per the parity-over-simulation principle from [paper-evaluation-harness.md](paper-evaluation-harness.md)).

---

## Why the engine exists

Every analyst and strategist proposal passes through PM evaluation per the rubric in [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). Approved proposals become trades with observable outcomes; rejected and modified-away proposals leave no trail. Without recovering their counterfactual outcomes, PM evaluation quality is unmeasurable — we see *what* PM rejected but not whether the rejections were right.

The engine simulates what would have happened if a rejected proposal had been approved (or if a modified proposal had been approved in its original un-modified form), producing a hypothetical realized P/L joined to the originating PM envelope.

---

## Scope (v1)

- **Equity proposals only.** Options and multi-leg strategies are skipped with `replay_status = unevaluable`, `unevaluable_reason = unsupported_instrument`. A v2 with Black-Scholes-derived option pricing is a possible follow-up.
- **Rejections and modifications.** Rejections are replayed in the form the analyst originally proposed. Modifications are replayed in their *original un-modified form* alongside the actual modified-form trade — yielding the modification effectiveness measure (modified-form actual vs. original-form counterfactual).
- **Daily batch + on-demand.** An APScheduler job runs once daily after market close. On-demand invocation is available via the [command center](../command-center.md) or a feedback-review skill. On-demand runs are idempotent — replays already present are skipped.

---

## Inputs

1. **Proposal queue.** PM decisions whose evaluation horizon has elapsed: activity log entries with `event_type = pm_decision`, verdict in (`reject`, `approve_with_modification`), and `evaluation_due_at < now`, where `evaluation_due_at = rejection_timestamp + entry_window_duration + max(time_stop_horizon, target_estimated_horizon)`. The engine maintains its own queue cursor so re-runs don't reprocess settled replays.
2. **Historical underlying price stream.** Minute-bar (or finer if available) data over the window `[rejection_timestamp, rejection_timestamp + entry_window + max_time_stop]`.
3. **Paper-evaluation harness primitives.** Spread, impact, and regulatory-fee estimation per [paper-evaluation-harness.md § Sub-models](paper-evaluation-harness.md#sub-models), reused so counterfactual P/L applies the same drag estimation as actual paper P/L.

---

## Process per proposal

### Step 1 — Eligibility check

Record `replay_status = unevaluable` and stop with the appropriate reason:

- Instrument type not equity → `unsupported_instrument`
- Historical price data not available over the window → `data_missing`
- Corporate action on the underlying fired within the replay window → `corporate_action_in_window`. Modeling cost-basis adjustment for splits, spin-offs, and mergers in flight is not worth the complexity at the 4–72h horizon.

### Step 2 — Entry simulation

Walk forward through the price stream from `entry_window_start` to `entry_window_end`. For each bar:

- **Limit order** — long buy: bar low ≤ limit, fill at limit on first touch. Short sell: bar high ≥ limit, fill at limit on first touch.
- **Market order** — assume fill at the next bar's open price after the entry timestamp.

Limit orders fill in full at the touch; partial fills are not modeled. Apply entry slippage and fees per harness primitives. Record `entered = true`, `entry_price`, `entry_timestamp`, `entry_slippage`, `entry_fees`.

If no fill occurs within the entry window: record `entered = false`, `exit_leg = entry_window_expired_unfilled`, `realized_pl = 0`. Stop. The proposal is fully evaluated — the counterfactual answer is "would not have entered."

### Step 3 — Bracket simulation

Walk forward from the entry timestamp. For each bar, check trigger conditions:

1. **Target hit** — long: bar high ≥ target. Short: bar low ≤ target.
2. **Stop hit** — long: bar low ≤ stop. Short: bar high ≥ stop.
3. **Time stop expired** — bar timestamp ≥ time-stop timestamp.

**Same-bar target-and-stop ambiguity.** If both target and stop trigger within the same bar, the engine assumes the stop fills first — a conservative bias toward worse counterfactual P/L, surfaced as a metric caveat. Affects only narrow same-bar overlap cases; minute-bar resolution keeps the fraction small.

**Trigger price recording.** Price-based exits record at the trigger price (target or stop level), not the bar's extreme — the assumption is the order fills at the trigger as it's crossed. Time-stop exits record at the bar open at the time-stop timestamp.

Apply exit slippage and fees per harness primitives. Record `exit_leg`, `exit_price`, `exit_timestamp`, `exit_slippage`, `exit_fees`.

### Step 4 — P/L computation

```
realized_pl = (exit_price - entry_price) × quantity × direction_sign
              - entry_slippage - entry_fees
              - exit_slippage - exit_fees
```

Where `direction_sign = +1` for long, `-1` for short. Shorts assume zero borrow rate, matching the harness primitives (universe is ETB-only).

### Step 5 — Confidence determination

Classify confidence using the same convention as the paper harness:

- **High** — full bar coverage; liquidity within typical-day envelope per the data layer's rolling baselines; spread within typical-day envelope; no same-bar trigger ambiguity.
- **Medium** — minor data gaps that don't affect trigger determination; unusual but unambiguous price movement; or same-bar ambiguity that doesn't change the exit_leg classification.
- **Low** — significant data gaps; thin liquidity affecting trigger price reliability; same-bar ambiguity affecting the exit_leg classification; or any condition introducing material uncertainty.

Low-confidence replays are persisted but **excluded from aggregated PM accuracy metrics at query time** — captured for diagnostic drill-down, kept out of trend signals.

---

## Output

A `counterfactual_replays` entity in [state-persistence.md](state-persistence.md), one record per replay attempt, joined to the originating PM envelope via `pm_decision_envelope_id`. Schema specified there.

One record per (proposal, replay_kind) tuple. Modifications produce one `modification_original_form` record ("what if PM had not modified"); rejections produce one `rejection` record ("what if PM had not rejected").

---

## On-demand invocation

The operator can trigger the engine via:
- Command center action — runs a one-shot pass over the queue
- Feedback-review skill — invoked at session start to ensure counterfactuals are fresh

On-demand runs share the eligibility cursor and are idempotent. Re-evaluating with a newer engine version requires explicit purge.

---

## Engine versioning

The `replay_engine_version` field captures the algorithm version that produced each record. Versions advance when the algorithm changes — better same-bar disambiguation, a new confidence classifier, support for a new instrument type. Older replays remain valid records tagged with their version. Aggregation queries can filter to a single version when consistency matters.

---

## Activation

The engine runs in both paper and live modes, using the harness primitives for slippage and fee drag in both. In live mode the slippage estimate represents what *would* have happened had the rejection been an approval.

The engine is read-only on trading state — engine failure does not affect any pipeline invocation or monitor responsibility. Failed runs abort and retry on the next daily batch tick; operators are alerted via the standard channel if failures persist.

---

## Dependencies

- [Paper evaluation harness](paper-evaluation-harness.md) — provides the spread, impact, and regulatory-fee primitives reused for slippage and fee drag, ensuring counterfactual and actual P/L are computed on the same drag basis
- [State persistence](state-persistence.md) — defines the `counterfactual_replays` entity that the engine populates, the `pm_decision` activity log events that supply the proposal queue, and the `process_lifetimes` and `agent_calls` provenance the engine can join against for confounder analysis
- [Portfolio manager](../04-decision-layer/portfolio-manager.md) — defines the PM envelope structure (verdict, evaluation criteria, modifications, anti_patterns_identified) that supplies replay inputs
- [Configuration management](../configuration-management.md) — provides the scheduler hook for the daily batch run (a new scheduled trigger in `scheduler.yaml` runs the engine once per day after close)
- [Command center](../command-center.md) — surfaces the on-demand trigger and renders aggregated counterfactual results in the Quality and feedback view group
