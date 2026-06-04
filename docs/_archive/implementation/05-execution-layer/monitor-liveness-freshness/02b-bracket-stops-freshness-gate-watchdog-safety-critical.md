# 02b — bracket_stops freshness gate + watchdog (safety-critical)

## Goal

Close the most safety-critical exposure in this epic: `bracket_stops` evaluates 1s stop cycles against whatever spot sits in the cache with **no freshness gate** (the [ALP-825](https://linear.app/alphamind-jatassi/issue/ALP-825/monitor-liveness-and-freshness-invariants-alp-768770819-class-fix) symptom). If the price stream wedges, a protective stop that should fire on a real move never sees the move while the watcher reports healthy. This story migrates `bracket_stops`' spot read onto 01b's freshness-aware read so a position with a stale/missing spot is **skipped** (never fired or evaluated against a frozen spot), and drives the loop through 01a's `supervised_loop` at the watcher's true 1s cadence so the watchdog bound is tight rather than the old 1h global. Per decision (D), escalation is the cache-level global signal (02d) — this story skips and logs locally, it does not add its own escalation.

## Reading

* `src/alphamind/execution/continuous_monitor/bracket_stops/task.py` — `_spot_for_position` (`cache.get(ticker).price`, no freshness check); `_run_bracket_stop_cycle` (the `spot is None or spot <= 0.0: continue` skip the gate routes into); `run_options_bracket_watcher` (the `while True` + trailing `sleep` + [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)'s `beat`); `bracket_stop_evaluation_cadence_seconds` (the \~1s cadence).
* `src/alphamind/execution/continuous_monitor/bracket_stops/wiring.py` — `register_task` + the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) `beat=lambda: supervisor.beat("bracket_stops")`.
* `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` — 01b's `read` + `PriceRead` result type.
* `src/alphamind/config/models/continuous_monitor.py` — `underlying_price_max_age_seconds` (01b).
* `src/alphamind/execution/continuous_monitor/supervisor.py` — 01a's `supervised_loop` seam.
* `src/alphamind/execution/continuous_monitor/breach_loop/task.py` — the [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) stale-exclusion semantics to mirror (consistency of "stale excluded from enforcement").
* `ALP-825` (this symptom) · `ALP-770` (the gate behavior to mirror).

## Depends on

* 01a ([ALP-826](https://linear.app/alphamind-jatassi/issue/ALP-826/01a-per-cadence-default-on-stall-watchdog)) — `supervised_loop` seam + per-cadence watchdog.
* 01b ([ALP-827](https://linear.app/alphamind-jatassi/issue/ALP-827/01b-freshness-aware-underlyingpricecache-read-api)) — `read` + `PriceRead` + `underlying_price_max_age_seconds`.

## Scope

In scope: `src/alphamind/execution/continuous_monitor/bracket_stops/task.py` and `bracket_stops/wiring.py`. Tests at `tests/execution/continuous_monitor/bracket_stops/test_task.py`.

### 1\. Freshness-gated spot read

`_spot_for_position` resolves spot through `cache.read(ticker, as_of=now, max_age_seconds=config.underlying_price_max_age_seconds)`. Only a `FRESH` result yields a spot; `STALE` and `MISSING` yield no spot, so the position is skipped by the existing `spot is None` guard — a stop is never evaluated or fired against a stale or absent price. Log a skipped-stale position **once on its fresh→stale transition** (not every cycle), so a persistently stale ticker does not spam the 1s loop.

### 2\. Drive the loop through `supervised_loop`

Replace `run_options_bracket_watcher`'s `while True` + trailing `await sleep(cadence)` with `async for _ in supervisor.supervised_loop("bracket_stops", config.bracket_stop_evaluation_cadence_seconds): <cycle>`, and drop the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) `beat` lambda from the wiring. The \~1s cadence yields a tight watchdog bound — the explicit motivation in the parent: the 1s safety-critical loop must not inherit a 1h detection latency. The wedged-cycle-trips-watchdog behavior from [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) is preserved (now via the seam).

### Out of scope

* Any escalation/alert on stale spot — the single global-stale signal is emitted by `breach_loop` (02d) per decision (D). This story skips + logs only.
* P/L-trigger math, closer submission, fired-leg bookkeeping — unchanged.

## Acceptance criteria

- [ ] A position whose cached spot reads `STALE` (older than `underlying_price_max_age_seconds`) is not evaluated and no leg fires for it that cycle.
- [ ] A position whose ticker reads `MISSING` is skipped (unchanged from today, now via the gated read).
- [ ] A position with a `FRESH` spot evaluates and fires exactly as before (no behavior change on the happy path).
- [ ] The skipped-stale log emits once on the fresh→stale transition, not on every 1s cycle for a persistently stale ticker.
- [ ] The watcher runs via `supervised_loop` at `bracket_stop_evaluation_cadence_seconds`, is watched on a cadence-derived (tight) bound, and a wedged cycle trips the watchdog; no `beat` lambda remains in the wiring.
- [ ] `uv run pytest tests/execution/continuous_monitor/bracket_stops/test_task.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Unit tests over `_run_bracket_stop_cycle` with a cache seeded at controlled `as_of` offsets: a stale spot skips a leg that *would* fire on that price; a fresh spot fires it; a missing ticker skips; the skipped-stale log fires once across repeated stale cycles. A watchdog test asserts the watcher beats via `supervised_loop` and a stalled cycle trips on the cadence-derived bound.