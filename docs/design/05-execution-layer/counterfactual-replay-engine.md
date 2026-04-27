# Counterfactual replay engine

A deterministic offline simulator that walks PM-rejected and PM-modified proposals through historical underlying price data to produce hypothetical realized P/L. Substrate for measuring PM rejection accuracy, modification effectiveness, and per-anti-pattern detector accuracy in the feedback loop.

An analysis tool — reads PM decisions and price data, writes counterfactual replay records, sits outside the trading hot path. Estimates, not punishes; same-bar disambiguation carries a small documented bias (see Process Step 3), option-replay simplifications carry their own bounded biases surfaced as confidence caveats (see [Option proposals](#option-proposals)), otherwise unbiased measurement (per the parity-over-simulation principle from [paper-evaluation-harness.md](paper-evaluation-harness.md)).

---

## Why the engine exists

Every analyst and strategist proposal passes through PM evaluation per [portfolio-manager.md](../04-decision-layer/portfolio-manager.md). Approved proposals become trades with observable outcomes; rejected and modified-away proposals leave no trail. Without recovering their counterfactual outcomes, PM evaluation quality is unmeasurable — we see *what* PM rejected but not whether the rejections were right.

The engine simulates what would have happened if a rejected proposal had been approved (or if a modified proposal had been approved in its original un-modified form), producing a hypothetical realized P/L joined to the originating PM envelope.

---

## Scope

- **Equity and single-leg option proposals.** Equity proposals replay via bar-level limit/market fill simulation. Single-leg option proposals (long call, long put, short call, short put) replay via underlying-bar trigger evaluation with [Black-Scholes pricing](../06-risk-guardrails/guardrail-evaluation.md#delta-adjusted-exposure) at entry and exit only — see [Option proposals](#option-proposals) below. Multi-leg strategies are skipped with `replay_status = unevaluable`, `unevaluable_reason = unsupported_instrument`.
- **Rejections and modifications.** Rejections are replayed as the analyst originally proposed. Modifications are replayed in their *original un-modified form* alongside the actual modified-form trade — yielding the modification effectiveness measure (modified-form actual vs. original-form counterfactual).
- **Daily batch + on-demand.** An APScheduler job runs once daily after market close. On-demand invocation is available via the [command center](../command-center.md) or a feedback-review skill. On-demand runs are idempotent — replays already present are skipped.

---

## Inputs

1. **Proposal queue.** PM decisions whose evaluation horizon has elapsed: activity log entries with `event_type = pm_decision`, verdict in (`reject`, `approve_with_modification`), and `evaluation_due_at < now`, where `evaluation_due_at = rejection_timestamp + entry_window_duration + max(time_stop_horizon, target_estimated_horizon)`. The engine maintains its own queue cursor so re-runs don't reprocess settled replays.
2. **Historical underlying price stream.** Minute-bar (or finer if available) data over the window `[rejection_timestamp, rejection_timestamp + entry_window + max_time_stop]`. The trigger decision evaluates against the underlying for both equity and option proposals — option brackets fire on the underlying per [orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying).
3. **Paper-evaluation harness primitives.** Spread, impact, and regulatory-fee estimation per [paper-evaluation-harness.md § Sub-models](paper-evaluation-harness.md#sub-models), reused so counterfactual P/L applies the same drag estimation as actual paper P/L.
4. **Black-Scholes pricing primitive.** Option proposals only. The [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) supplies the Black-Scholes implementation already used at OPEN/ADD validation and continuous-monitor [greeks refresh](architecture.md#4d-greeks-refresh-orchestration). Replay calls the primitive twice per proposal — once at the entry bar, once at the exit bar — supplying `(S, K, T, σ, r)` from the underlying bar's open, the proposal's strike, the bar's time-to-expiration, the IV-surface lookup, and the configured risk-free rate. The conservative delta buffer is disabled for replay (replay estimates outcomes; it does not gate decisions).
5. **Implied volatility surface.** Option proposals only. The data pipeline already persists IV surface snapshots per the [`ImpliedVolSurface`](../01-data-layer/schema/derivatives.py) schema produced by [programmatic distillation — quantitative](../02-distillation-layer/external.md). Replay looks up the surface at the entry timestamp and the exit timestamp (interpolating to the proposal's `(strike, expiration)`), reusing what's already persisted — no new bar-level surface persistence is required.

---

## Process per proposal

### Step 1 — Eligibility check

Record `replay_status = unevaluable` with the appropriate reason and stop:

- Instrument type is multi-leg strategy → `unsupported_instrument`. Per-leg pricing across a strategy under the entry-and-exit-only BS approach (see [Option proposals](#option-proposals)) loses too much fidelity to be worth the surface; deferred to a v3 with per-leg replay.
- Option proposal whose hard-backstop bracket leg is P/L-based on the option's own price (i.e., no underlying-anchored price leg or time-stop leg) → `unsupported_bracket_type`. v2 does not simulate per-bar option pricing; a P/L-on-option-price bracket cannot be evaluated without it. AlphaMind's bracket-construction discipline ([orders-and-brackets.md § Three invalidation types](orders-and-brackets.md#three-invalidation-types)) means most option proposals carry an underlying-anchored or time-based hard leg, so this exclusion is rare in practice.
- Historical price data not available over the window → `data_missing`. For option proposals, also fires when no IV-surface snapshot is available at or before the entry or exit timestamp within the data pipeline's regular persistence cadence (a snapshot older than the cadence implies the pipeline missed a refresh — distinct from a snapshot being merely lagged within a refresh interval, which is normal).
- Corporate action on the underlying fired within the replay window → `corporate_action_in_window`. Modeling cost-basis adjustment for splits, spin-offs, and mergers in flight is not worth the complexity at the 4–72h horizon.

### Step 2 — Entry simulation

**Equity proposals.** Walk forward through the underlying bar stream from `entry_window_start` to `entry_window_end`. For each bar:

- **Limit order** — long buy: bar low ≤ limit, fill at limit on first touch. Short sell: bar high ≥ limit, fill at limit on first touch.
- **Market order** — assume fill at the next bar's open price after the entry timestamp.

Limit orders fill in full at the touch; partial fills are not modeled.

**Option proposals.** Treat entry as a market-style fill at the bar following the proposal timestamp regardless of `entry_order.type`. The option fill price is BS-derived from the underlying bar's open, the IV-surface snapshot at the entry timestamp, the bar's time-to-expiration, and the configured risk-free rate. The proposal's `entry_order.limit_price` (when present) is recorded but does not gate fill timing — bar-level limit walks would require per-bar option pricing, which v2 explicitly does not model. Surface this as a confidence caveat (see [Step 5](#step-5--confidence-determination)), parallel to the same-bar disambiguation caveat.

Common to both. Apply entry slippage and fees per harness primitives. Record `entered = true`, `entry_price` (option premium for option proposals; underlying price for equity), `entry_timestamp`, `entry_slippage`, `entry_fees`.

If no fill within the entry window (equity only — option entries always fire at the proposal-following bar): record `entered = false`, `exit_leg = entry_window_expired_unfilled`, `realized_pl = 0`. Stop. The counterfactual answer is "would not have entered."

### Step 3 — Bracket simulation

Walk forward from the entry timestamp through the **underlying** bar stream — for both equity and option proposals — checking trigger conditions on each bar. Option brackets fire on the underlying per [orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying), so the trigger code path is the same for both asset types:

1. **Target hit** — long: underlying bar high ≥ target. Short: underlying bar low ≤ target.
2. **Stop hit** — long: underlying bar low ≤ stop. Short: underlying bar high ≥ stop.
3. **Time stop expired** — bar timestamp ≥ time-stop timestamp.

P/L-based bracket legs (whether on equity or option proposals) are *not* simulated in v2 alongside the underlying-anchored legs — only the underlying-anchored and time-based legs drive trigger evaluation. Per [orders-and-brackets.md § P/L-based bracket legs](orders-and-brackets.md#pl-based-bracket-legs-anchor-to-actual-fill-price), P/L legs re-anchor to the actual fill price; replay's BS-derived option fill price is itself an estimate, and recursively re-anchoring against an estimate compounds bias. The omission is surfaced as a confidence caveat (see [Step 5](#step-5--confidence-determination)).

**Same-bar target-and-stop ambiguity.** If both target and stop trigger within the same bar, the engine assumes the stop fills first — a conservative bias toward worse counterfactual P/L, surfaced as a metric caveat. Minute-bar resolution keeps the affected fraction small.

**Trigger price recording.**

- *Equity proposals.* Price-based exits record at the trigger price (target or stop level), not the bar's extreme — the order fills at the trigger as it's crossed. Time-stop exits record at the bar open at the time-stop timestamp.
- *Option proposals.* The underlying-bar trigger fires at the trigger price level, but the recorded `exit_price` is the option's BS-derived premium given the underlying at the trigger level, the IV-surface snapshot at the exit timestamp, the bar's time-to-expiration, and the risk-free rate. Time-stop exits record the BS-derived option price at the bar open at the time-stop timestamp.

Apply exit slippage and fees per harness primitives. Record `exit_leg`, `exit_price`, `exit_timestamp`, `exit_slippage`, `exit_fees`.

### Step 4 — P/L computation

```
realized_pl = (exit_price - entry_price) × quantity × multiplier × direction_sign
              - entry_slippage - entry_fees
              - exit_slippage - exit_fees
```

Where `direction_sign = +1` for long, `-1` for short. `quantity` is shares for equity, contracts for options. `multiplier` is 1 for equity and the option contract multiplier (100 for US listed options, the same multiplier used in the [`position_size.delta_adjusted_exposure`](../04-decision-layer/analyst-output-schema.md) computation `contract_count × multiplier × |delta| × underlying_price`). Shorts assume zero borrow rate, matching the harness primitives (universe is ETB-only).

### Step 5 — Confidence determination

Classify confidence using the same convention as the paper harness:

- **High** — full bar coverage; liquidity within typical-day envelope per the data layer's rolling baselines; spread within typical-day envelope; no same-bar trigger ambiguity. Option proposals additionally require: an IV-surface snapshot at both entry and exit timestamps within the data pipeline's normal refresh interval; no P/L-based bracket leg present alongside the underlying-anchored legs.
- **Medium** — minor data gaps that don't affect trigger determination; unusual but unambiguous price movement; same-bar ambiguity that doesn't change the exit_leg classification; option proposals where a P/L-based bracket leg was present but excluded from trigger evaluation (the underlying-anchored leg fired first and the P/L leg's omission cannot have changed the outcome).
- **Low** — significant data gaps; thin liquidity affecting trigger price reliability; same-bar ambiguity affecting the exit_leg classification; option proposals where the IV-surface snapshot is materially lagged at the entry or exit timestamp (older than typical within the pipeline's refresh interval, indicating the BS fill estimate degrades); or any condition introducing material uncertainty.

Low-confidence replays are persisted but **excluded from aggregated PM accuracy metrics at query time** — captured for diagnostic drill-down, kept out of trend signals.

---

## Option proposals

### Why the trigger evaluates on the underlying

Option-bracket trigger semantics in production already evaluate on the underlying ([orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying)). The price-based invalidation leg is a thesis statement about the underlying's behavior — "a move below $865 invalidates the thesis" — and the underlying equity stream provides clean, real-time data with no derivation error. The replay engine inherits this shape: the trigger code path is identical for equity and option proposals (Step 3), and the BS primitive is invoked only to translate the underlying-bar trigger into an option premium for entry and exit fill recording (Step 2 entry, Step 3 exit-price recording).

### Why BS runs only at entry and exit, not per bar

Per-bar option pricing would require either a dense historical IV surface persisted at every bar across the replay window (a real engineering cost) or per-bar BS computation against a cheaper-but-frozen IV (which produces a synthetic option-price stream that the trigger doesn't actually consume — option brackets fire on the underlying). Both are over-modeling for the question PM accuracy is asking: "would the PM-rejected option proposal have closed at a profit or loss given the underlying's path?" That question reduces to underlying-trigger evaluation plus BS-derived fill prices at two timestamps. The IV-surface lookup at entry and exit reuses what the data pipeline already persists for guardrail validation and continuous-monitor [greeks refresh](architecture.md#4d-greeks-refresh-orchestration), so no new persistence is introduced.

### v2 simplifications surfaced as confidence caveats

Three v2 simplifications are surfaced through the [Step 5](#step-5--confidence-determination) confidence classifier rather than as silent biases, parallel to how v1 surfaces same-bar target-and-stop disambiguation:

1. **Limit-on-option-price entry semantics not modeled.** Entry fires at the proposal-following bar regardless of `entry_order.type`; `limit_price` is recorded but does not gate fill timing. Surfaced as a baseline confidence caveat for option replays.
2. **P/L-based bracket legs not simulated alongside underlying-anchored legs.** A position with both leg kinds replays only the underlying-anchored and time-based legs; the omission is surfaced as a Medium-or-Low confidence demotion depending on whether the omission could have changed the exit_leg classification.
3. **Lagged IV-surface snapshots at entry or exit.** When the most recent snapshot at or before the entry or exit timestamp is older than the data pipeline's normal refresh interval, the BS fill estimate degrades materially and the replay is marked Low confidence (and thus excluded from aggregated PM accuracy metrics at query time).

The simplifications are bounded and localized — they affect option replays only, they do not propagate into equity replay accuracy, and they are surfaced as named caveats at query time rather than absorbed silently into the realized-P/L number.

### Multi-leg strategies remain out of scope

Strategy proposals (multi-leg structures with per-leg greeks summed to the strategy level — see [position-model.md](position-model.md)) require per-leg pricing across the entry and exit. Under the entry-and-exit-only BS approach, per-leg pricing is two BS calls per leg per replay, multiplied across the strategy's leg count, with cross-leg-correlated IV smile interpolation that the single-strike surface lookup doesn't capture. The fidelity-vs-complexity trade swings the other way — strategies are deferred to a v3 with per-leg replay rather than retrofitted into v2. Marked `unsupported_instrument` in v2.

---

## Output

A `counterfactual_replays` entity in [state-persistence.md](state-persistence.md), one record per replay attempt, joined to the originating PM envelope via `pm_decision_envelope_id`. Schema specified there.

One record per (proposal, replay_kind) tuple. Modifications produce a `modification_original_form` record ("what if PM had not modified"); rejections produce a `rejection` record ("what if PM had not rejected").

---

## On-demand invocation

Operator triggers:
- Command center action — runs a one-shot pass over the queue
- Feedback-review skill — invoked at session start to ensure counterfactuals are fresh

On-demand runs share the eligibility cursor and are idempotent. Re-evaluating with a newer engine version requires explicit purge.

---

## Engine versioning

The `replay_engine_version` field captures the algorithm version that produced each record. Versions advance when the algorithm changes — better same-bar disambiguation, a new confidence classifier, support for a new instrument type. Older replays remain valid records tagged with their version. Aggregation queries can filter to a single version when consistency matters.

v1 is equity-only. v2 adds single-leg option support per [Option proposals](#option-proposals); v1 records of equity replays remain valid (the equity logic is unchanged), and v2 produces both equity and option records. Cross-version filtering is needed only when an aggregation query is sensitive to whether option records are included; equity-only aggregations may safely mix v1 and v2 records.

---

## Activation

Runs in both paper and live modes, using harness primitives for slippage and fee drag. In live mode the slippage estimate represents what *would* have happened had the rejection been an approval.

Read-only on trading state — engine failure does not affect any pipeline invocation or monitor responsibility. Failed runs abort and retry on the next daily batch tick; operators are alerted if failures persist.

---

## Dependencies

- [Paper evaluation harness](paper-evaluation-harness.md) — provides the spread, impact, and regulatory-fee primitives reused for slippage and fee drag, ensuring counterfactual and actual P/L are computed on the same drag basis
- [State persistence](state-persistence.md) — defines the `counterfactual_replays` entity that the engine populates, the `pm_decision` activity log events that supply the proposal queue, and the `process_lifetimes` and `agent_calls` provenance the engine can join against for confounder analysis
- [Portfolio manager](../04-decision-layer/portfolio-manager.md) — defines the PM envelope structure (verdict, evaluation criteria, modifications, anti_patterns_identified) that supplies replay inputs
- [Guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) — provides the Black-Scholes pricing primitive shared with OPEN/ADD validation and continuous-monitor greeks refresh, called twice per option replay (entry + exit) with the conservative delta buffer disabled
- [Orders & brackets](orders-and-brackets.md) — defines the underlying-anchored trigger semantics for option brackets that the replay engine evaluates against the historical underlying bar stream
- [Programmatic distillation — quantitative](../02-distillation-layer/external.md) — produces the IV-surface snapshots persisted at the cadence the data pipeline already uses; replay looks up at entry and exit timestamps without requiring new persistence
- [Configuration management](../configuration-management.md) — provides the scheduler hook for the daily batch run (a new scheduled trigger in `scheduler.yaml` runs the engine once per day after close) and the risk-free rate consumed by BS pricing (shared with the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md)'s configuration surface)
- [Command center](../command-center.md) — surfaces the on-demand trigger and renders aggregated counterfactual results in the Quality and feedback view group
