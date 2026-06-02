# 01c — Monitor /control + /events HTTP surface

## Goal

Ship the FastAPI + Uvicorn loopback-bound HTTP surface inside the continuous monitor process that exposes the three `POST /control/*` verbs (cancel_order, force_close_position, set_halt_mode) and the `GET /events` SSE stream specified in [monitor-control-and-events-schema.md](<docs/design/monitor-control-and-events-schema.md>). The surface is the producer side of the command center's `/api/control/*` proxy (story 04a) and `/api/events` multiplexer (story 04b); deferred from [ALP-123](<https://linear.app/alphamind-jatassi/issue/ALP-123>). Loopback isolation (`127.0.0.1`) is the trust boundary — no authentication on this surface; the public-facing proxy in story 04a owns auth and audit.

## Reading

* `docs/design/monitor-control-and-events-schema.md` end-to-end — wire format, error envelopes, SSE framing, cross-field invariants, OMS boundary discipline.
* `docs/design/command-center.md` § Control surface and § Live event stream — prose contract for the monitor side.
* `docs/design/05-execution-layer/architecture.md` § Continuous monitor and § Fill report contract — the in-process surfaces the new HTTP layer wraps.
* `docs/design/05-execution-layer/engine-envelope-schema.md` — engine-originated envelope shape the cancel_order and force_close_position verbs synthesize.
* `docs/design/05-execution-layer/oms-command-ids.md` § Engine-originated command IDs — `MON.{monitor_session_id}.{trigger_id}` format the `envelope_id` follows.
* `src/alphamind/execution/continuous_monitor/__main__.py` and `continuous_monitor/session.py`, `continuous_monitor/supervisor.py`, `continuous_monitor/logging_setup.py` — process-lifetime + supervisor patterns to integrate with.
* `src/alphamind/execution/continuous_monitor/breach_loop/`, `fill_stream_consumer/`, `greeks_refresh/`, `emergency_trigger/`, `cascade_dispatch/` — emit points the SSE event emitter hooks into.
* `src/alphamind/portfolio_state/halt/` (or equivalent) — halt-mode state the `set_halt_mode` verb mutates; `halt_mode_engaged` portfolio state field per schema § Notes on cross-field invariants.

## Depends on

* (none — parallel with 01a and 01b)

## Scope

In scope, under `src/alphamind/execution/continuous_monitor/`. Tests at `tests/execution/continuous_monitor/control/`.

### 1\. New `continuous_monitor/control/` subpackage

Per-component depth scaffolding mirroring 01b's shape:

```
src/alphamind/execution/continuous_monitor/control/
├── __init__.py
├── app.py            # FastAPI app composition + lifespan
├── routes.py         # 3 POST /control/* + 1 GET /events route handlers
├── verbs.py          # synchronous verb implementations
├── events.py         # SSEEventEmitter + per-event-type Pydantic payloads
└── models.py         # Pydantic request / response / error models per the schema
```

### 2\. Pydantic boundary models (`models.py`)

One Pydantic model per request body, response envelope, error envelope, and per-event payload as defined in `monitor-control-and-events-schema.md` `$defs`. Mirror enum names verbatim. Pydantic v2 `model_config = ConfigDict(extra="forbid")`.

### 3\. FastAPI app + lifespan (`app.py`)

Mount routes; lifespan constructs the `SSEEventEmitter`, wires it into the existing breach / fill / greeks / cascade emit points (they currently log; add structural callbacks they can also invoke), and starts Uvicorn bound to `127.0.0.1:{monitor_control_port}`. Port from `ContinuousMonitorConfig` via a new `control_port: int` field — default 8766. On shutdown, cancels the Uvicorn task inside the supervisor's `TaskGroup`.

### 4\. Verb implementations (`verbs.py`)

Three verbs. Each synthesizes engine-originated envelopes per the schema's § Boundary with the OMS:

* `cancel_order(order_id) -> ControlResult` — synthesizes a CANCEL envelope conforming to `engine-envelope-schema.md` with the monitor's session ID + a fresh trigger ID; submits via the existing OMS submit path. Maps `not_found` (404), `precondition_failed` (409, with `current_status`), `broker_error` (502, with `broker_message`) per the schema's per-verb error table.
* `force_close_position(position_id, rationale) -> ForceClosePositionResult` — synthesizes a CLOSE envelope with `close_rationale_type: risk_management`, `risk_management_subtype: pm_directed`, and the operator's `rationale` prefixed with operator-console attribution on `guardrail_trigger_record.position_selection_rationale`. Returns the synthesized `envelope_id` (the `MON.{session_id}.{trigger_id}` string). Same error map as cancel_order.
* `set_halt_mode(enabled, reason) -> ControlResult` — flips the monitor-local halt-mode flag and persists it to the `halt_mode_engaged` portfolio state field. Idempotent — same-value returns `accepted` with original `applied_at`. No error envelope beyond `validation_failed` (400).

### 5\. SSE event emitter (`events.py`)

Class that exposes structural callbacks the existing monitor loops invoke (`emit_websocket_connected()`, `emit_websocket_disconnected(reason)`, `emit_fill_received(order_id, position_id, fill_price, fill_qty)`, `emit_breach_detected(rule, current_value, limit, response_classification)`, `emit_emergency_invocation_triggered(reason)`, `emit_greeks_refreshed(underlying, refreshed_at)`). Each writes onto an `asyncio.Queue` per subscriber. The `GET /events` route consumes its subscriber's queue and writes SSE frames per the schema's framing spec. `heartbeat` fires every 15 s when no other event has emitted on a connection.

Wiring: add callback invocations at the relevant points in `breach_loop/`, `fill_stream_consumer/`, `greeks_refresh/`, `underlying_stream/`, `emergency_trigger/`, `cascade_dispatch/`. Keep the callbacks no-op-safe so a monitor running without the HTTP surface (degraded boot) still functions.

### 6\. Route handlers (`routes.py`)

Three `POST /control/*` handlers dispatching to `verbs.py`; one `GET /events` SSE handler with `media_type="text/event-stream"`, `Cache-Control: no-cache`, `Connection: keep-alive`.

### Out of scope

* The browser-facing `/api/control/*` proxy and `/api/events` multiplexer — story 04a and 04b inside the command center backend.
* Authentication on this surface — loopback isolation is the trust boundary.
* The `OperatorInvocationHandle` wrap on the caller side — story 02 + 04a.

## Acceptance criteria

- [ ] `src/alphamind/execution/continuous_monitor/control/` subpackage exists with `app.py`, `routes.py`, `verbs.py`, `events.py`, `models.py`.
- [ ] All Pydantic request / response / error / event-payload models exist with field shapes matching the schema's `$defs`.
- [ ] `ContinuousMonitorConfig.control_port: int` exists with default `8766`.
- [ ] The FastAPI app binds to `127.0.0.1:{control_port}` at monitor startup and is torn down cleanly on shutdown.
- [ ] `cancel_order` synthesizes a CANCEL envelope that the OMS accepts; the envelope's `guardrail_trigger_record.position_selection_rationale` carries operator-console attribution.
- [ ] `force_close_position` synthesizes a CLOSE envelope with the documented rationale + subtype fields; the response carries `envelope_id` matching the synthesized envelope's ID.
- [ ] `set_halt_mode` flips the flag, persists it to `halt_mode_engaged`, and returns idempotent `accepted` on same-value re-set.
- [ ] `GET /events` emits SSE-framed records for each of the seven event types when their producer fires; 15 s heartbeat when idle.
- [ ] `websocket_connected` and `websocket_disconnected` alternate (schema § Notes on cross-field invariants) — tests assert no two consecutive `connected` events without an intervening `disconnected`.
- [ ] `fill_received.order_id` resolves to a known OMS `client_order_id` before emission (the monitor's existing fill-stream resolution path).
- [ ] `breach_detected.response_classification: immediate` is followed by a corresponding engine-originated CLOSE envelope per the schema; `deferred` produces no envelope.
- [ ] Tests under `tests/execution/continuous_monitor/control/` cover: each verb's happy path + each documented error envelope; SSE framing of each of the seven event types; heartbeat cadence; bind-to-loopback only.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/execution/continuous_monitor/ -n auto`. Manual smoke: boot the monitor with `uv run python -m alphamind.execution.continuous_monitor` against a synthetic portfolio; `curl http://127.0.0.1:8766/control/set_halt_mode -d '{"enabled":true,"reason":"smoke"}'` returns `200`; `curl -N http://127.0.0.1:8766/events` shows SSE frames including heartbeat.