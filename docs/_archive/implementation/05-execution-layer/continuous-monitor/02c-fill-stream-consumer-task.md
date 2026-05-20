# 02c — Fill stream consumer task

## Goal

Wrap the broker-adapter primitive `subscribe_trade_updates(stream) -> AsyncIterator[FillReport]` in a long-lived asyncio task that translates each yielded `FillReport` into a `FillRecord`, persists it via `append_fill_record(session, fill)`, and on disconnect invokes `recover_missed_fills_since(...)` to fetch missed events through Alpaca's REST `GET /v2/orders` surface before resuming the websocket loop. The fill-buffer write per the design (`architecture.md § 4a`) is the monitor's job; the broker-adapter primitive ([ALP-384](https://linear.app/alphamind-jatassi/issue/ALP-384/02f-trade-updates-fill-stream-subscriber-fill-report-translator)) and the persistence helper ([ALP-119](https://linear.app/alphamind-jatassi/issue/ALP-119/state-persistence)) are both shipped — this story is the lifecycle wrapper that ties them together.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4a — Alpaca fill-stream consumption contract; disconnect-recovery requirement.
* `docs/design/05-execution-layer/broker-adapter.md` § Fill stream — event types and `trade_updates` channel; § Fill buffer model — durable write semantics; § Re-subscription on disconnect.
* `docs/design/05-execution-layer/state-persistence.md` § Tier 2 Lifecycle entities — `FillRecord` shape and `append_fill_record` invariants.
* `src/alphamind/execution/broker_adapter/fill_stream.py` — `subscribe_trade_updates`, `FillReport`, `translate_trade_update` ([ALP-384](https://linear.app/alphamind-jatassi/issue/ALP-384/02f-trade-updates-fill-stream-subscriber-fill-report-translator)).
* `src/alphamind/execution/broker_adapter/recovery.py` § `recover_missed_fills_since`, `order_snapshot_to_fill_reports` ([ALP-389](https://linear.app/alphamind-jatassi/issue/ALP-389/03d-disconnect-recovery)) — REST-side disconnect recovery.
* `src/alphamind/execution/state_persistence/write_paths/fill_persistence.py` — `append_fill_record(session, fill: FillRecord)` — owns its own transaction; silently skips duplicates by `(client_order_id, alpaca_execution_id)`.
* `src/alphamind/execution/state_persistence/write_paths/records.py` — `FillRecord` Pydantic shape this story translates into. Pay attention to per-leg vs parent shape for mleg fills.
* `src/alphamind/scripts/verify_regt_margin_attribution.py` (`_buy_fill_record()` builder) — example `FillRecord` construction the translator can mirror.
* Parent issue `ALP-123` § Cross-feature dependencies — "Composed primitives" entry naming the two upstream primitives.
* `ALP-432` (01) — `MonitorSupervisor.register_task` is the entry point this story wires into.

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config + logging

## Scope

Source under `src/alphamind/execution/continuous_monitor/fill_stream_consumer/` (new sub-package). Tests at `tests/execution/continuous_monitor/fill_stream_consumer/`. No changes to broker_adapter or state_persistence beyond consumption.

### 1\. `fill_report_to_fill_record` translator

`src/alphamind/execution/continuous_monitor/fill_stream_consumer/translation.py`:

```python
def fill_report_to_fill_record(report: FillReport) -> FillRecord | None:
    """Translate a single FillReport to a FillRecord suitable for append_fill_record.

    Returns None for FillReport events that do not produce a fill (e.g., event_type
    in {"new", "canceled", "expired", "rejected", "replaced", "replace_rejected",
    "done_for_day"}) — those events still flow through the activity log but do
    not append to fill_records.

    For mleg fills:
    * The parent FillReport (no parent_client_order_id) translates to None
      (the parent strategy fill is not a per-position fill).
    * Each per-leg child FillReport (parent_client_order_id populated) translates
      to one FillRecord against the strategy position's leg id.
    """
```

Use `FillRecord(...)` from `state_persistence/write_paths/records.py` as the output type. Field mapping (informative, not exhaustive; the implementer must verify against the FillRecord shape):

| `FillRecord` field | Source on `FillReport` |
| -- | -- |
| `client_order_id` | `report.client_order_id` (for equity/options) or `report.parent_client_order_id` (for mleg leg children) |
| `alpaca_execution_id` | derived from `report.alpaca_order_id` + `report.fill_timestamp` (the alpaca-py event payload has the execution id; check `report.raw_event_payload`) |
| `fill_timestamp` | `report.fill_timestamp` |
| `fill_price` | `report.fill_price` (asserted non-None for fill events) |
| `fill_quantity` | `report.fill_quantity` |
| `event_type` | `report.event_type` (constrained to `{filled, partially_filled, stopped}` for translator-returning-non-None) |
| `execution_venue` | `report.execution_venue` |
| `occ_symbol` | `report.occ_symbol` |
| `position_intent` | `report.position_intent` |

Pure synchronous function; no I/O.

### 2\. Run-forever consumer task

`src/alphamind/execution/continuous_monitor/fill_stream_consumer/task.py`:

```python
async def run_fill_stream_consumer(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    session_factory: Callable[[], AsyncSession],  # default_session_factory()
    stream_factory: TradingStreamFactory,  # alpaca-py TradingStream builder; mockable
    trading_client_factory: TradingClientFactory,  # for REST recovery; mockable
) -> None:
    """Run-forever task. Owns the lifecycle of subscribe_trade_updates:

    1. Build the trading stream and a REST client from factories.
    2. On startup, run recover_missed_fills_since(since=<last fill ts in DB>) to
       catch up on anything missed between the last shutdown and now.
    3. Open subscribe_trade_updates(stream) and drain the iterator:
       - For each FillReport, call fill_report_to_fill_record(report).
       - If non-None, open an AsyncSession and call append_fill_record(session, record).
       - Whether or not it appended, emit a one-line debug log of (event_type,
         client_order_id, fill_timestamp).
    4. On websocket exception, call recover_missed_fills_since(since=<last fill
       ts in DB>) to fetch missed events via GET /v2/orders, persist each via
       append_fill_record, then sleep with exponential backoff up to
       config.max_reconnect_attempts and reconnect.
    5. On asyncio.CancelledError, await graceful stream close and exit.
    """
```

Implementation notes:

* The startup recovery (step 2) handles the "monitor was down for N minutes" gap. Use the latest `fill_records.fill_timestamp` query as the `since` bound; on first-ever boot (no rows) skip recovery.
* `append_fill_record` is idempotent on `(client_order_id, alpaca_execution_id)` — recovery and live-stream cannot produce duplicates.
* Per-fill writes use their own short-lived transaction (no batching) so a crash mid-batch never leaves the cache + DB inconsistent.

### 3\. Supervisor registration

Extend `src/alphamind/execution/continuous_monitor/__main__.py` (in addition to story 02b's registration): construct the `session_factory`, `stream_factory`, and `trading_client_factory`; register the fill-stream consumer task under the name `"fill_stream_consumer"`.

### 4\. Tests at `tests/execution/continuous_monitor/fill_stream_consumer/`

* `test_translation.py` —
  * Equity `fill` event translates to a single `FillRecord` with the documented field mapping.
  * Single-leg-options `partial_fill` translates to a `FillRecord` with `occ_symbol` populated and `event_type="partially_filled"`.
  * mleg parent event returns `None`; per-leg children translate to one `FillRecord` each with `client_order_id` set to the parent's id.
  * Non-fill events (`canceled`, `expired`, `replaced`, `rejected`, `done_for_day`, `new`, `replace_rejected`) return `None`.
* `test_task.py` — using fake `TradingStreamFactory` and `TradingClientFactory`:
  * Happy path: synthetic FillReport injected; `append_fill_record` is called with the correct translated record; activity-log emission is observed (or the equivalent test-side hook).
  * Startup recovery: simulated last-fill timestamp in DB triggers a `recover_missed_fills_since(since=...)` call before the websocket loop opens.
  * Disconnect: synthetic websocket exception triggers recovery + backoff + reconnect; cache survives.
  * Budget exhaustion: when `max_reconnect_attempts` is exhausted, the task raises and the supervisor's exit logging records the failure.
  * Idempotency: same FillReport delivered twice (e.g., via live stream + recovery overlap) produces only one row.
  * Cancellation: `asyncio.CancelledError` triggers stream close and clean exit.

### Out of scope

* Phase 1 fill integration (consuming `fill_records` to update positions / brackets / cash) — already shipped in state-persistence ([ALP-365](https://linear.app/alphamind-jatassi/issue/ALP-365/07-phase-1-write-path-fill-integration) etc.); the monitor only writes.
* Live-execution-estimate annotation (`live_execution_estimate` on `FillReport`) — paper-evaluation-harness work tree ([ALP-130](https://linear.app/alphamind-jatassi/issue/ALP-130/paper-evaluation-harness)).
* Per-leg fill correlation (strategy-level activation when all legs filled) — owned by Phase 1 fill integration.

## Acceptance criteria

- [ ] `fill_report_to_fill_record` is importable from `alphamind.execution.continuous_monitor.fill_stream_consumer`; returns `None` for non-fill event types.
- [ ] `fill_report_to_fill_record` maps every field documented in the Scope table; for mleg per-leg children, `client_order_id` resolves to `parent_client_order_id`.
- [ ] `run_fill_stream_consumer` builds the trading stream via `stream_factory(session.mode)` and the REST client via `trading_client_factory(session.mode)`.
- [ ] On startup, `recover_missed_fills_since(since=<latest fill ts>)` is invoked when prior fills exist in `fill_records`; the function is NOT invoked on a fresh DB.
- [ ] Each yielded `FillReport` that translates to a non-`None` `FillRecord` is persisted via `append_fill_record` in its own transaction.
- [ ] A duplicate `(client_order_id, alpaca_execution_id)` pair (e.g., delivered twice through live + recovery) does not create a second `fill_records` row.
- [ ] Synthetic websocket disconnect triggers recovery + exponential-backoff reconnect; budget exhaustion propagates the exception out.
- [ ] `asyncio.CancelledError` closes the trading stream and the task returns cleanly.
- [ ] The supervisor in `__main__.py` registers this task as `"fill_stream_consumer"`.
- [ ] Tests at `tests/execution/continuous_monitor/fill_stream_consumer/` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/fill_stream_consumer/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in `tests/execution/state_persistence/`, `tests/execution/broker_adapter/`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/fill_stream_consumer/ -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: spot-check that a paper-account fill event (via Alpaca paper trading sandbox) flows through the monitor and into the `fill_records` table during story 05's e2e verification.
* Lint clean per CLAUDE.md.