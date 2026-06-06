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
- **Analyst and strategist proposals.** New-entry analyst Recommendations replay via entry-and-bracket simulation (below). Strategist position-action proposals (CLOSE / REDUCE / ADD / ADJUST-BRACKET against an existing position) replay via a parallel path that reads the position's state at proposal time and simulates the proposed action's outcome against the actual underlying path forward — see [Strategist position-action replay](#strategist-position-action-replay).
- **Rejections and modifications.** Rejections are replayed as originally proposed. Modifications are replayed in their *original un-modified form* alongside the actual modified-form trade — yielding the modification effectiveness measure (modified-form actual vs. original-form counterfactual).
- **On-demand only.** The engine runs from a CLI (`python -m alphamind.execution.counterfactual_replay_engine.cli`) triggered on demand from the dev machine — there is no APScheduler daily-batch integration. Runs are idempotent: replays already present for `(envelope_id, replay_kind)` are skipped. A command-center button is deferred to [command center](../command-center.md).

---

## Inputs

1. **Proposal queue.** PM decisions whose evaluation horizon has elapsed: activity log entries with `event_type = pm_decision`, verdict in (`reject`, `approve_with_modification`), and `evaluation_due_at < now`. For analyst proposals `evaluation_due_at = proposal_timestamp + entry_window_duration + max(time_stop_horizon, target_estimated_horizon)`; strategist position-action proposals use a config-driven forward window (`strategist_default_forward_window_hours`). The proposal timestamp is the `pm_decision` activity-log entry's timestamp (the originating Recommendation / assessment carries none). The engine maintains its own queue cursor so re-runs don't reprocess settled replays.
2. **Historical underlying price stream.** Bars at the finest timeframe persisted in production — currently 15-minute (`_TIMEFRAMES` in `data_sources/polygon/equity.py`; minute bars are not collected) — over the window `[proposal_timestamp, proposal_timestamp + entry_window + max_time_stop]`. The trigger decision evaluates against the underlying for both equity and option proposals — option brackets fire on the underlying per [orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying).
3. **Paper-evaluation harness primitives.** Spread, impact, and regulatory-fee estimation per [paper-evaluation-harness.md § Sub-models](paper-evaluation-harness.md#sub-models), reused so counterfactual P/L applies the same drag estimation as actual paper P/L.
4. **Black-Scholes pricing primitive.** Option proposals only. The [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) supplies the Black-Scholes implementation already used at OPEN/ADD validation and continuous-monitor [greeks refresh](architecture.md#4d-greeks-refresh-orchestration). Replay calls the primitive twice per proposal — once at the entry bar, once at the exit bar — supplying `(S, K, T, σ, r)` from the underlying bar's open, the proposal's strike, the bar's time-to-expiration, the per-contract IV snapshot lookup (point 5), and the configured risk-free rate. The conservative delta buffer is disabled for replay (replay estimates outcomes; it does not gate decisions).
5. **Per-contract implied volatility.** Option proposals only. Replay reads `OptionsContractSnapshots.implied_volatility` directly — the same time-series the continuous-monitor greeks-refresh consumes — looking up the nearest snapshot at-or-before the entry timestamp and the exit timestamp for the contract (resolved by underlying + strike + expiration + call/put via the canonical OCC encoding). No `ImpliedVolSurface` interpolator is built and no new persistence is introduced. A snapshot whose `implied_volatility` is NULL is treated as a miss.

---

## Process per proposal

### Step 1 — Eligibility check

Record `replay_status = unevaluable` with the appropriate reason and stop:

- Instrument type is multi-leg strategy → `unsupported_instrument`. Per-leg pricing across a strategy under the entry-and-exit-only BS approach (see [Option proposals](#option-proposals)) loses too much fidelity to be worth the surface; deferred to a v3 with per-leg replay.
- `unsupported_bracket_type` is **reserved** in the persisted vocabulary for an option proposal whose only hard backstop is P/L-based on the option's own price, but v2 never produces it from analyst output: analyst `Recommendation` hard backstops are always underlying-anchored price legs (`InvalidationLeg.type = price`, with a required `underlying_trigger`) or time legs ([orders-and-brackets.md § Three invalidation types](orders-and-brackets.md#three-invalidation-types)), so the case is unreachable. P/L-anchored / option-price-triggered bracket legs exist only on an executed position's `BracketRecord` (the strategist path), which v2 walks underlying-anchored-legs-only.
- Historical price data not available over the window → `data_missing`. For option proposals, also fires when no per-contract IV snapshot (with a non-null `implied_volatility`) is available at or before the entry or exit timestamp within the data pipeline's regular persistence cadence (a snapshot older than the cadence implies the pipeline missed a refresh — distinct from a snapshot being merely lagged within a refresh interval, which is normal).
- Corporate action on the underlying fired within the replay window → `corporate_action_in_window`. Modeling cost-basis adjustment for splits, spin-offs, and mergers in flight is not worth the complexity at the 4–72h horizon.

### Step 2 — Entry simulation

**Equity proposals.** Walk forward through the underlying bar stream from `entry_window_start` to `entry_window_end`. For each bar:

- **Limit order** — long buy: bar low ≤ limit, fill at limit on first touch. Short sell: bar high ≥ limit, fill at limit on first touch.
- **Market order** — assume fill at the next bar's open price after the entry timestamp.

Limit orders fill in full at the touch; partial fills are not modeled.

**Option proposals.** Treat entry as a market-style fill at the bar following the proposal timestamp regardless of `entry_order.type`. The option fill price is BS-derived from the underlying bar's open, the per-contract IV snapshot at the entry timestamp, the bar's time-to-expiration, and the configured risk-free rate. The proposal's `entry_order.limit_price` (when present) is recorded but does not gate fill timing — bar-level limit walks would require per-bar option pricing, which v2 explicitly does not model. Surface this as a confidence caveat (see [Step 5](#step-5--confidence-determination)), parallel to the same-bar disambiguation caveat.

Common to both. Apply entry slippage and fees per harness primitives. Record `entered = true`, `entry_price` (option premium for option proposals; underlying price for equity), `entry_timestamp`, `entry_slippage`, `entry_fees`.

If no fill within the entry window (equity only — option entries always fire at the proposal-following bar): record `entered = false`, `exit_leg = entry_window_expired_unfilled`, `realized_pl = 0`. Stop. The counterfactual answer is "would not have entered."

### Step 3 — Bracket simulation

Walk forward from the entry timestamp through the **underlying** bar stream — for both equity and option proposals — checking trigger conditions on each bar. Option brackets fire on the underlying per [orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying), so the trigger code path is the same for both asset types:

1. **Target hit** — long: underlying bar high ≥ target. Short: underlying bar low ≤ target.
2. **Stop hit** — long: underlying bar low ≤ stop. Short: underlying bar high ≥ stop.
3. **Time stop expired** — bar timestamp ≥ time-stop timestamp.

P/L-based bracket legs are *not* simulated in v2 — only underlying-anchored and time-based legs drive trigger evaluation. Analyst proposals never carry a P/L-based hard leg (their stops are underlying-anchored), so there is nothing to omit there; P/L-anchored legs appear only on an existing position's `BracketRecord` (the strategist path), and v2 walks that position's underlying-anchored legs only. Per [orders-and-brackets.md § P/L-based bracket legs](orders-and-brackets.md#pl-based-bracket-legs-anchor-to-actual-fill-price), P/L legs re-anchor to the actual fill price; recursively re-anchoring against replay's own BS-derived estimate would compound bias, so v2 leaves them out.

**Same-bar target-and-stop ambiguity.** If both target and stop trigger within the same bar, the engine assumes the stop fills first — a conservative bias toward worse counterfactual P/L, surfaced as a confidence caveat. At the 15-minute resolution used in production the same-bar fraction is higher than at minute resolution, so the classifier demotes these to **Medium** baseline rather than Low (see [Step 5](#step-5--confidence-determination)) to avoid over-shrinking the High pool.

**Trigger price recording.**

- *Equity proposals.* Price-based exits record at the trigger price (target or stop level), not the bar's extreme — the order fills at the trigger as it's crossed. Time-stop exits record at the bar open at the time-stop timestamp.
- *Option proposals.* The underlying-bar trigger fires at the trigger price level, but the recorded `exit_price` is the option's BS-derived premium given the underlying at the trigger level, the per-contract IV snapshot at the exit timestamp, the bar's time-to-expiration, and the risk-free rate. Time-stop exits record the BS-derived option price at the bar open at the time-stop timestamp.

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

- **High** — full bar coverage; liquidity within typical-day envelope per the data layer's rolling baselines; spread within typical-day envelope; no same-bar trigger ambiguity. Option proposals additionally require a per-contract IV snapshot within the normal refresh interval at both the entry and exit timestamps.
- **Medium** — same-bar target-and-stop ambiguity (at 15-minute resolution this is the baseline demotion, not Low); minor data gaps that don't affect trigger determination; spread outside the typical-day envelope.
- **Low** — thin liquidity affecting trigger-price reliability; a coverage gap coinciding with same-bar ambiguity (the trigger determination is genuinely uncertain); option proposals where the per-contract IV snapshot is materially lagged at entry or exit (older than typical within the pipeline's refresh interval, so the BS fill estimate degrades materially).

Low-confidence replays are persisted but **excluded from aggregated PM accuracy metrics at query time** — captured for diagnostic drill-down, kept out of trend signals.

---

## Option proposals

### Why the trigger evaluates on the underlying

Option-bracket trigger semantics in production already evaluate on the underlying ([orders-and-brackets.md § Options price-based stops](orders-and-brackets.md#options-price-based-stops-trigger-on-the-underlying)). The price-based invalidation leg is a thesis statement about the underlying's behavior — "a move below $865 invalidates the thesis" — and the underlying equity stream provides clean, real-time data with no derivation error. The replay engine inherits this shape: the trigger code path is identical for equity and option proposals (Step 3), and the BS primitive is invoked only to translate the underlying-bar trigger into an option premium for entry and exit fill recording (Step 2 entry, Step 3 exit-price recording).

### Why BS runs only at entry and exit, not per bar

Per-bar option pricing would require either a dense historical IV surface persisted at every bar across the replay window (a real engineering cost) or per-bar BS computation against a cheaper-but-frozen IV (which produces a synthetic option-price stream that the trigger doesn't actually consume — option brackets fire on the underlying). Both are over-modeling for the question PM accuracy is asking: "would the PM-rejected option proposal have closed at a profit or loss given the underlying's path?" That question reduces to underlying-trigger evaluation plus BS-derived fill prices at two timestamps. The per-contract IV snapshot lookup at entry and exit reuses what the data pipeline already persists (`OptionsContractSnapshots`, the same source continuous-monitor [greeks refresh](architecture.md#4d-greeks-refresh-orchestration) reads), so no new persistence is introduced.

### v2 simplifications surfaced as confidence caveats

Two v2 simplifications on the option path are surfaced through the [Step 5](#step-5--confidence-determination) confidence classifier rather than as silent biases, parallel to how same-bar target-and-stop disambiguation is surfaced:

1. **Limit-on-option-price entry semantics not modeled.** Entry fires at the proposal-following bar regardless of `entry_order.type`; `limit_price` is recorded but does not gate fill timing. A baseline caveat for every option replay.
2. **Lagged IV snapshots at entry or exit.** When the most recent per-contract snapshot at or before the entry or exit timestamp is older than the data pipeline's normal refresh interval, the BS fill estimate degrades materially and the replay is marked Low confidence (and thus excluded from aggregated PM accuracy metrics at query time).

There is no P/L-bracket-leg simplification on the analyst path: analyst option proposals carry no P/L-on-the-option's-own-price leg — their hard backstops are the underlying-anchored price / time legs the walker already evaluates. The simplifications are bounded and localized — they affect option replays only, they do not propagate into equity replay accuracy, and they are surfaced as named caveats at query time rather than absorbed silently into the realized-P/L number.

### Multi-leg strategies remain out of scope

Strategy proposals (multi-leg structures with per-leg greeks summed to the strategy level — see [position-model.md](position-model.md)) require per-leg pricing across the entry and exit. Under the entry-and-exit-only BS approach, per-leg pricing is two BS calls per leg per replay, multiplied across the strategy's leg count, with cross-leg-correlated IV smile interpolation that the single-strike surface lookup doesn't capture. The fidelity-vs-complexity trade swings the other way — strategies are deferred to a v3 with per-leg replay rather than retrofitted into v2. Marked `unsupported_instrument` in v2.

---

## Strategist position-action replay

Strategist `PositionAssessment` proposals also pass through PM, so they also need counterfactual measurement. When PM rejects or modifies a strategist action, the engine simulates the proposed action's outcome against the actual underlying path forward, reading the position's state at the proposal timestamp from the existing `positions` / `fill_records` / `brackets` records (no new state capture). Equity and single-leg option positions are in scope; multi-leg strategy positions are `unsupported_instrument`. The position-state snapshot reconstructs net signed quantity and quantity-weighted average cost basis by folding the position's fills up to the proposal timestamp.

- **CLOSE / REDUCE** — a one-shot exit at the proposal-following bar (the counterfactual "what if PM had let the close / reduce through"). REDUCE uses the proposal's absolute reduce quantity; CLOSE uses the full position (or its `"all"` sentinel). `exit_leg = strategist_close_at_proposal`; P/L follows the Step 4 formula against the reconstructed cost basis.
- **ADJUST-BRACKET** — re-walks the bracket simulation from the proposal timestamp forward with the proposed absolute target / stop / time levels; `exit_leg` is the resulting `target_hit` / `stop_hit` / `time_stop_fired`.
- **ADD** — simulates the proposed add entry (reusing the analyst entry path) and walks the resulting brackets; P/L is on the added portion only.

A `PendingOrderAssessment` cancel replays whether the pending order would have filled and what it would have earned; modify is best-effort in v2 and otherwise recorded `unevaluable` / `strategist_position_action_not_supported`. No-action assessments (`hold` for a position, `maintain` for a pending order) are not replayed — `strategist_position_action_not_supported`. P/L-anchored or option-price-triggered legs on an existing bracket are not simulated in v2 (the underlying-anchored legs are walked), consistent with the analyst paths.

---

## Output

A `counterfactual_replays` entity in [state-persistence.md](state-persistence.md), one record per replay attempt, joined to the originating PM envelope via `pm_decision_envelope_id`. Schema specified there.

One record per (proposal, replay_kind) tuple. Modifications produce a `modification_original_form` record ("what if PM had not modified"); rejections produce a `rejection` record ("what if PM had not rejected").

---

## On-demand invocation

The engine is invoked from a CLI on demand — `python -m alphamind.execution.counterfactual_replay_engine.cli` — with optional `--since` (scope to a date range) and `--as-of` (the "now" used to gate the evaluation horizon) flags. There is no scheduled run. Re-runs are idempotent (records already present for `(envelope_id, replay_kind)` are skipped); re-evaluating with a newer engine version requires explicit purge. A command-center button and a feedback-review trigger are deferred to those features.

---

## Engine versioning

The `replay_engine_version` field captures the algorithm version that produced each record. Versions advance when the algorithm changes — better same-bar disambiguation, a new confidence classifier, support for a new instrument type. Older replays remain valid records tagged with their version. Aggregation queries can filter to a single version when consistency matters.

v1 is equity-only. v2 adds single-leg option support per [Option proposals](#option-proposals) and strategist position-action replay per [Strategist position-action replay](#strategist-position-action-replay); the version string names the algorithm released at that point, not the per-instrument scope. v1 records of equity replays remain valid (the equity logic is unchanged), and v2 produces equity, option, and strategist-action records. Cross-version filtering is needed only when an aggregation query is sensitive to whether option records are included; equity-only aggregations may safely mix v1 and v2 records. Per-leg multi-leg strategy support is reserved for v3.

---

## Activation

Runs in both paper and live modes, using harness primitives for slippage and fee drag. In live mode the slippage estimate represents what *would* have happened had the rejection been an approval.

Read-only on trading state — engine failure does not affect any pipeline invocation or monitor responsibility. A failed run aborts and the operator re-runs the CLI; individual per-proposal errors are counted and skipped so one malformed proposal doesn't abort the batch.

---

## Dependencies

- [Paper evaluation harness](paper-evaluation-harness.md) — provides the spread, impact, and regulatory-fee primitives reused for slippage and fee drag, ensuring counterfactual and actual P/L are computed on the same drag basis
- [State persistence](state-persistence.md) — defines the `counterfactual_replays` entity that the engine populates, the `pm_decision` activity log events that supply the proposal queue, and the `process_lifetimes` and `agent_calls` provenance the engine can join against for confounder analysis
- [Portfolio manager](../04-decision-layer/portfolio-manager.md) — defines the PM envelope structure (verdict, evaluation criteria, modifications, anti_patterns_identified) that supplies replay inputs
- [Guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md) — provides the Black-Scholes pricing primitive shared with OPEN/ADD validation and continuous-monitor greeks refresh, called twice per option replay (entry + exit) with the conservative delta buffer disabled
- [Orders & brackets](orders-and-brackets.md) — defines the underlying-anchored trigger semantics for option brackets that the replay engine evaluates against the historical underlying bar stream
- **Options contract snapshots** (`OptionsContractSnapshots`) — the per-contract IV time-series replay reads at the entry and exit timestamps (the same source the continuous-monitor greeks-refresh consumes); no new persistence is introduced
- [Configuration management](../configuration-management.md) — provides `config/replay_engine.yaml` (the IV-lag confidence threshold and the strategist forward-window default, loaded via `CounterfactualReplayEngineConfig`) and the risk-free rate consumed by BS pricing (shared with the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md)'s configuration surface)
- [Command center](../command-center.md) — surfaces the on-demand trigger and renders aggregated counterfactual results in the Quality and feedback view group
