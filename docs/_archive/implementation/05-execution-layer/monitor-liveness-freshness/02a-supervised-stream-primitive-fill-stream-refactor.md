# 02a — Supervised-stream primitive + fill-stream refactor

## Goal

Extract the connected-but-silent detection [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) built *inside* `subscribe_trade_updates` — a last-activity clock, an RTH-gated frame-staleness timeout that forces a budget-neutral reconnect, and a per-poll-slice heartbeat — into one reusable, transport-agnostic primitive, and refactor the fill stream to consume it with no behavior change. This is what makes the underlying-price stream (story 03) reuse the exact same logic instead of rebuilding it (the duplication this epic exists to prevent). Also migrate the fill consumer's heartbeat onto 01a's mechanism. This is the Defect A reuse step.

## Reading

* `src/alphamind/execution/broker_adapter/fill_stream.py` — `subscribe_trade_updates`: the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) poll loop (`asyncio.wait_for(queue.get(), poll_interval)`, `last_frame_at`, `FillStreamStalledError`, `beat`, `is_rth`, `frame_timeout`, `monotonic`). This inline logic is what gets extracted.
* `src/alphamind/execution/broker_adapter/__init__.py` — the `FillStreamStalledError` export (renamed by this story).
* `src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py` — `run_fill_stream_consumer`: the budget-neutral stalled-error reconnect (`attempt = 0; continue`), `is_rth` derivation, and `beat` wiring that consume the primitive.
* `src/alphamind/execution/continuous_monitor/underlying_stream/task.py` — `run_underlying_stream` / `_run_one_connection`: the `TaskGroup` + `run_task = stream._run_forever()` + `_periodic_subscription_diff` + `_handler` (`cache.update`) shape the primitive must *also* fit (story 03 feeds quote-arrival into it). Confirm the extracted kernel is transport-agnostic enough for both.
* `src/alphamind/execution/continuous_monitor/supervisor.py` — 01a's `beat` seam + per-cadence registration the fill consumer adopts.
* `config/continuous_monitor.yaml` — `fill_stream_stale_timeout_seconds` ([ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)).
* `ALP-819` — the behavior the refactor must preserve exactly.

## Depends on

* 01a ([ALP-826](https://linear.app/alphamind-jatassi/issue/ALP-826/01a-per-cadence-default-on-stall-watchdog)) — the `beat` seam + per-cadence registration the fill consumer adopts; this story must not reintroduce a hand-wired `supervisor.beat(...)` lambda.

## Scope

In scope: a new module `src/alphamind/execution/broker_adapter/stream_staleness.py` (continuous_monitor already imports from `broker_adapter`, so story 03 can import it); `src/alphamind/execution/broker_adapter/fill_stream.py`; `src/alphamind/execution/broker_adapter/__init__.py`; `src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py`. Tests at `tests/execution/broker_adapter/test_fill_stream.py` and `tests/execution/continuous_monitor/fill_stream_consumer/test_task.py`.

### 1\. The frame-staleness / heartbeat kernel

In `broker_adapter/stream_staleness.py`, add a transport-agnostic helper that, given an `is_rth` predicate, a `frame_timeout`, a `beat` callable, a `poll_interval`, and a `monotonic` clock, drives a poll loop that (a) beats every slice, (b) tracks last-activity, and (c) raises `StreamStalledError` when the market is open and no activity has arrived for longer than `frame_timeout` — resetting the clock off-hours so the closed-market gap is not charged against the first RTH window. The kernel does not know what an "activity" is: the caller signals activity (a dequeued fill frame; a handled quote).

### 2\. Rename the error type

Rename `FillStreamStalledError` to `StreamStalledError` (the canonical cross-stream name, defined in `stream_staleness.py`). Update its export in `broker_adapter/__init__.py`, the `except` clause in `run_fill_stream_consumer`, and every test reference. No backward-compat alias is kept.

### 3\. Refactor the fill stream onto the kernel

`subscribe_trade_updates` drives its `queue.get()` poll through the kernel, signalling activity on each dequeued frame. The run-task done-callback sentinel and the budget-neutral reconnect in `run_fill_stream_consumer` are unchanged — [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)'s tests stay green verbatim (modulo the error rename).

### 4\. Adopt the 01a heartbeat mechanism

Pass 01a's `beat` seam as the kernel's `beat` callable (replacing `beat=lambda: supervisor.beat("fill_stream_consumer")`) and declare the fill consumer's poll cadence to the watchdog at registration so its stall bound derives per-cadence. The reconnect-driven outer loop is not a fixed-cadence `supervised_loop`; its liveness comes from the kernel's per-slice beat. A starved consumer still trips the watchdog ([ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) behavior preserved).

### Out of scope

* Wiring the underlying stream to the kernel — story 03.
* Any freshness/cache-gate work — Defect B stories.
* If extraction shows the two stream lifecycles cannot share even this thin kernel (per the parent's surfacing condition), surface before over-abstracting — but the default and expected outcome is the shared kernel.

## Acceptance criteria

- [ ] `stream_staleness.py` exposes the kernel + `StreamStalledError`, both transport-agnostic (no `TradeUpdate` / alpaca-py coupling in their signatures).
- [ ] A connected-but-silent source during RTH causes the kernel to raise `StreamStalledError` after `frame_timeout`; off-hours it does not raise and resets its clock; it beats on every poll slice in both cases.
- [ ] `FillStreamStalledError` no longer exists anywhere (`git grep FillStreamStalledError` is empty); `subscribe_trade_updates` drives its drain through the kernel and the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) fill-stream tests pass (connected-but-silent detection; off-hours no-stall; budget-neutral reconnect) against `StreamStalledError`.
- [ ] The fill consumer beats via 01a's seam with a per-cadence bound; a starved consumer trips the watchdog; no hand-wired `supervisor.beat("fill_stream_consumer")` remains.
- [ ] `uv run pytest tests/execution/broker_adapter/test_fill_stream.py tests/execution/continuous_monitor/fill_stream_consumer/test_task.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Run the [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) fill-stream suite (updated for the rename) as the regression guard, plus new kernel unit tests with a `_StepClock`-style monotonic substitute asserting the RTH/off-hours staleness boundary and per-slice beats. Confirm `stream_staleness.py` has no broker-type imports and `git grep FillStreamStalledError` is empty.