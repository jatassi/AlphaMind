# 03 — Underlying-price stream adopts the supervised-stream primitive

## Goal

Close the [ALP-825](https://linear.app/alphamind-jatassi/issue/ALP-825/monitor-liveness-and-freshness-invariants-alp-768770819-class-fix) stream symptom: the underlying-price stream has the identical connected-but-silent blind spot the fill stream had. `run_underlying_stream` only reconnects when `_run_forever()` **raises**; if the `websockets` library reconnect-loops internally without raising or delivering quotes (the WinError 121 signature), the cache silently stops being written, the `TaskGroup` never exits, and — because the stream is not on the watchdog — nothing recovers it. This story drives the underlying stream's run-loop through 02a's frame-staleness/heartbeat kernel (quote arrival is the activity signal), forcing a budget-neutral reconnect on RTH quote-silence, and beats via 01a's seam so a wedge is recovered within a bounded window. It is the consumer that proves the 02a primitive generalizes.

## Reading

* `src/alphamind/execution/continuous_monitor/underlying_stream/task.py` — `run_underlying_stream` (the `while True` that only reconnects on raise; the [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) clean-exit→reconnect `else` branch to preserve); `_run_one_connection` (the `TaskGroup` with `run_task = stream._run_forever()` + `_periodic_subscription_diff`; the `_handler` that calls `cache.update` — the natural activity signal); the `max_reconnect_attempts` budget.
* `src/alphamind/execution/broker_adapter/stream_staleness.py` — 02a's frame-staleness/heartbeat kernel + `StreamStalledError` to consume.
* `src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py` — 02a's worked example of consuming the kernel (budget-neutral stalled reconnect; `is_rth`; kernel `beat`) to mirror.
* `src/alphamind/execution/continuous_monitor/underlying_stream/wiring.py` — `register_task` (no heartbeat today); how to wire an `is_market_open` predicate (mirror how [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) wired `calendar_cache.is_market_open` into the fill consumer in `__main__.py`).
* `src/alphamind/execution/continuous_monitor/supervisor.py` — 01a's `beat` seam + per-cadence registration.
* `config/continuous_monitor.yaml` — `fill_stream_stale_timeout_seconds` (the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) analogue) and `subscription_refresh_seconds`.
* `ALP-825` (this symptom) · `ALP-819` (the analogous fill-stream fix this mirrors).

## Depends on

* 02a ([ALP-828](https://linear.app/alphamind-jatassi/issue/ALP-828/02a-supervised-stream-primitive-fill-stream-refactor)) — the `stream_staleness.py` kernel + `StreamStalledError` (which in turn carries 01a's seam).

## Scope

In scope: `src/alphamind/execution/continuous_monitor/underlying_stream/task.py` and `underlying_stream/wiring.py`; a new `underlying_stream_stale_timeout_seconds` in `config/continuous_monitor.yaml` + `ContinuousMonitorConfig`. Tests at `tests/execution/continuous_monitor/underlying_stream/test_task.py`.

### 1\. Drive the run-loop through the 02a kernel

Feed each handled quote (`_handler` → `cache.update`) as the kernel's activity signal, and drive `_run_one_connection` so that RTH quote-silence beyond `underlying_stream_stale_timeout_seconds` raises `StreamStalledError`. `run_underlying_stream` catches it and forces a reconnect that is **budget-neutral** (does not consume `max_reconnect_attempts`) — a deliberate health refresh, mirroring the fill stream — so a persistently silent stream keeps recovering rather than exhausting the budget and exiting. The existing reconnect-on-raise and the [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) clean-exit→reconnect paths are preserved.

### 2\. RTH gating

Wire an `is_market_open` predicate into `run_underlying_stream` (mirroring the fill consumer's `is_rth` wiring) so the staleness check is engaged only during RTH — IEX quotes are legitimately sparse/absent off-hours — and the clock resets off-hours so the closed-market gap is not charged against the first RTH window.

### 3\. Frame-staleness timeout config

Add `underlying_stream_stale_timeout_seconds` to `ContinuousMonitorConfig`, sourced from `config/continuous_monitor.yaml`. Choose its default by the same rule `fill_stream_stale_timeout_seconds` used — it must exceed the longest legitimate RTH gap between activity — but because RTH equity quotes are continuous (unlike sparse fills) this is far tighter than the fill timeout; the yaml comment states that rule and that a needless reconnect during a quiet-but-healthy session is cheap (re-subscribe re-primes the cache).

### 4\. Beat via the kernel's per-slice heartbeat

The underlying stream's run-loop is reconnect/stream-driven (not a fixed-cadence loop), so it beats via the 02a kernel's per-slice `beat` — passing 01a's `beat` seam — exactly as the fill consumer does (not `supervised_loop`). Declare the kernel's `poll_interval` as the watchdog cadence at registration so a genuinely-blocked writer trips the watchdog → `os._exit(1)` → NSSM restart. This brings the underlying stream under coverage — closing the gap that the *writer* feeding every price consumer was the one task not on the safety net.

### Out of scope

* The freshness gate on the *reader* side — 01b/02b/02c/02d. This story keeps the cache fed; the readers gate what they consume.
* The subscription-diff logic (`_periodic_subscription_diff`) — unchanged except as needed to integrate the kernel.

## Acceptance criteria

- [ ] A connected-but-silent underlying stream during RTH (no quotes delivered, `_run_forever` never raises) forces a reconnect within `underlying_stream_stale_timeout_seconds` — not an indefinite silent cache freeze.
- [ ] That staleness-driven reconnect is budget-neutral: a persistently silent stream keeps reconnecting rather than exhausting `max_reconnect_attempts` and exiting.
- [ ] Off-hours, a silent stream does not force a reconnect and the clock resets (no spurious reconnect at the open).
- [ ] Reconnect-on-raise and the [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) clean-exit→reconnect behavior are preserved.
- [ ] The underlying stream beats via the kernel's per-slice `beat` (01a seam), is registered with the `poll_interval` cadence, and a genuinely-blocked writer trips the watchdog.
- [ ] `underlying_stream_stale_timeout_seconds` loads from `config/continuous_monitor.yaml`; `tests/config/test_snapshot.py` is re-pinned.
- [ ] `uv run pytest tests/execution/continuous_monitor/underlying_stream/test_task.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Unit tests with a fake `StockDataStream` whose `_run_forever` parks (alive, no exception) and delivers no quotes: assert a reconnect is forced within the threshold during RTH, the reconnect budget is not consumed, off-hours does not reconnect, and the stream beats each poll slice. Preserve the existing raise-driven and clean-exit reconnect tests. Re-pin the config snapshot.