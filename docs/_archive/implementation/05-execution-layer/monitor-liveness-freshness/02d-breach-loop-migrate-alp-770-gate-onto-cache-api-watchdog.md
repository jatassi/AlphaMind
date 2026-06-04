# 02d — breach_loop migrate [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) gate onto cache API + watchdog

## Goal

Converge the one consumer that already gates freshness — `breach_loop` — onto the shared 01b API so the staleness logic lives in one place, and make `breach_loop` the single emitter of the global-stale signal (decision D). Today `_run_one_tick` hand-rolls the gate inline (iterate `cache.get_all()`, compute age vs a local `max_price_age_seconds` default, build `stale_tickers`/`missing_tickers`) and `run_breach_loop` hand-rolls the consecutive-stale DEGRADED escalation ([ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during)). This story moves the per-position gate onto 01b's `read_all` + shared threshold, keeps [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during)'s escalation as the single consumer escalation (02b/02c deliberately add none), distinctly labels the writer-wedged "whole feed cold" case via 01b's `global_staleness` detector, and drives the loop through 01a's `supervised_loop`. [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during)**'s observable behavior must be preserved exactly.**

## Reading

* `src/alphamind/execution/continuous_monitor/breach_loop/task.py` — `_run_one_tick` (the inline gate: `cache.get_all()`, the `age > max_price_age_seconds` loop, `stale_tickers`/`missing_tickers`, `expected_tickers`, `had_stale_or_missing` return); `run_breach_loop` (the `while True` + `beat()` + market-open skip + trailing sleeps; the `stale_consecutive_count`/`stale_degraded` escalation; the `max_price_age_seconds` default param; `on_health_signal`).
* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` — `BreachLoopHealthSignal`.
* `src/alphamind/execution/continuous_monitor/breach_loop/wiring.py` — `register_task`, the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) `beat` lambda, `max_price_age_seconds` wiring.
* `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` — 01b's `read_all` + `global_staleness` detector + its signal type.
* `src/alphamind/config/models/continuous_monitor.py` — `underlying_price_max_age_seconds` (01b); `breach_loop_consecutive_failure_alert_threshold`; `breach_evaluation_cadence_seconds`.
* `src/alphamind/execution/continuous_monitor/supervisor.py` — 01a's `supervised_loop` seam.
* `ALP-770` — the exact behavior to preserve · decision (D) in the parent.

## Depends on

* 01a ([ALP-826](https://linear.app/alphamind-jatassi/issue/ALP-826/01a-per-cadence-default-on-stall-watchdog)) — `supervised_loop` seam + per-cadence watchdog.
* 01b ([ALP-827](https://linear.app/alphamind-jatassi/issue/ALP-827/01b-freshness-aware-underlyingpricecache-read-api)) — `read_all`, shared threshold, `global_staleness` detector + signal.

## Scope

In scope: `src/alphamind/execution/continuous_monitor/breach_loop/task.py` and `breach_loop/wiring.py`. Tests at `tests/execution/continuous_monitor/breach_loop/test_task.py`.

### 1\. Migrate the per-position gate onto 01b

Replace the inline age computation in `_run_one_tick` with `cache.read_all(expected_tickers, as_of=as_of, max_age_seconds=config.underlying_price_max_age_seconds)`: `FRESH` entries populate `underlying_prices`; `STALE`/`MISSING` are excluded from enforcement exactly as today. Remove the local `max_price_age_seconds` default parameter and read `config.underlying_price_max_age_seconds`. The `had_stale_or_missing` return that drives escalation is preserved.

### 2\. Preserve the [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) escalation as the single consumer escalation

Keep `run_breach_loop`'s consecutive-stale tracking and DEGRADED/RECOVERED `on_health_signal` emissions byte-for-byte in behavior (escalate after `breach_loop_consecutive_failure_alert_threshold` consecutive stale cycles; recover when fresh; the debounce against the tick-failure escalation). 02b and 02c add no escalation of their own, so this remains the one stale escalation.

### 3\. Distinctly label the writer-wedged case

When `cache.global_staleness(...)` reports the whole feed cold (writer wedged, not just one lagging ticker), emit through the *same* `on_health_signal` channel a signal whose `last_error` is the distinct string `"underlying price feed globally stale — writer wedged"` (vs the per-ticker-lag escalation's existing message), so the operator can tell "one ticker lagging / subscription lag" from "price stream dead." This is the single global-stale emitter (decision D) — no second emitter elsewhere.

### 4\. Drive the loop through `supervised_loop`

Replace `run_breach_loop`'s `while True` + the market-closed `sleep`+`continue` + the trailing `sleep` with `async for _ in supervisor.supervised_loop("breach_loop", config.breach_evaluation_cadence_seconds):` (the seam paces the cadence and beats at the top of every iteration — *before* the market-open check — so the loop keeps beating on closed-market ticks, preserving [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)). Drop the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) `beat` lambda from the wiring.

### Out of scope

* `bracket_stops`/`greeks_refresh` gates — 02b/02c.
* The breach-evaluation math, halt-transition logic, immediate/emergency callbacks, and tick-failure (non-stale) escalation — unchanged.

## Acceptance criteria

- [ ] A cache quote older than `underlying_price_max_age_seconds` is excluded from `breach_loop` enforcement; an absent open-position ticker is treated as missing — neither is enforced against ([ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) preserved, now via `read_all`).
- [ ] The local `max_price_age_seconds` default parameter is gone; the loop reads `config.underlying_price_max_age_seconds`.
- [ ] Consecutive stale/missing cycles escalate to DEGRADED after the configured threshold and recover when fresh — the existing [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) tests pass unchanged.
- [ ] When `global_staleness` fires, the emitted signal's `last_error` is the distinct `"underlying price feed globally stale — writer wedged"` string, separable from the per-ticker-lag escalation.
- [ ] `breach_loop` runs via `supervised_loop` at `breach_evaluation_cadence_seconds`, beats on every tick including closed-market ticks, and a wedged loop trips the watchdog; no `beat` lambda remains in the wiring.
- [ ] `uv run pytest tests/execution/continuous_monitor/breach_loop/test_task.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Run the existing [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) breach-loop suite unchanged (regression guard for stale exclusion + escalation). New tests: the loop reads `config.underlying_price_max_age_seconds` (not a local default); a whole-feed-cold scenario emits the distinct `last_error`; `breach_loop` beats via `supervised_loop` on a closed-market tick. Confirm no other module calls `global_staleness` / emits a global-stale signal.