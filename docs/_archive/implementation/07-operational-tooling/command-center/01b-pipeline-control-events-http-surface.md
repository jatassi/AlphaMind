# 01b — Pipeline /control + /events HTTP surface

## Goal

Ship the FastAPI + Uvicorn loopback-bound HTTP surface inside the pipeline scheduler process that exposes the five `POST /control/*` verbs (pause, resume, trigger_emergency_invocation, switch_profile, run_universe_validation) and the `GET /events` SSE stream specified in [pipeline-control-and-events-schema.md](<docs/design/pipeline-control-and-events-schema.md>). The surface is the producer side of the command center's `/api/control/*` proxy (story 04a) and `/api/events` multiplexer (story 04b); both deferred from [ALP-431](<https://linear.app/alphamind-jatassi/issue/ALP-431>) per its Pre-resolved decision (B). Loopback isolation (`127.0.0.1`) is the trust boundary — no authentication on this surface; the public-facing proxy in story 04a owns auth and audit.

## Reading

* `docs/design/pipeline-control-and-events-schema.md` end-to-end — wire format, error envelopes, SSE framing, cross-field invariants, evolution discipline.
* `docs/design/command-center.md` § Control surface and § Live event stream — prose contract these endpoints implement.
* `src/alphamind/scheduler/__main__.py` — current scheduler entrypoint; the FastAPI app + Uvicorn boot binds alongside APScheduler in the same process.
* `src/alphamind/scheduler/supervisor.py`, `scheduler/session.py`, `scheduler/logging_setup.py` — process-lifetime + supervisor patterns the new HTTP surface must integrate with.
* `src/alphamind/scheduler/orchestrator.py` — phase-transition + agent-lifecycle emit points the SSE event emitter hooks into (`ProgressEmitter` Protocol is already wired through here).
* `src/alphamind/scheduler/driver.py` — APScheduler driver; pause/resume mutate the scheduler-pause flag this exposes.
* `src/alphamind/scheduler/emergency.py` — emergency-invocation trigger path; the new `trigger_emergency_invocation` verb routes through it.
* `src/alphamind/_kernel/progress.py` — `ProgressEmitter` Protocol; the SSE event emitter implements it alongside the existing `NoOpProgressEmitter` and (debug-e2e) `JsonlProgressEmitter`.
* `scripts/validate_universe.py` — the script `run_universe_validation` synchronously invokes; the report shape feeds the response envelope.
* `src/alphamind/config/control_handlers/profile_switch.py` — `switch_active_profile()` the `switch_profile` verb wraps.

## Depends on

* (none — parallel with 01a and 01c)

## Scope

In scope, under `src/alphamind/scheduler/`. Tests at `tests/scheduler/control/`.

### 1\. New `scheduler/control/` subpackage

Per-component depth scaffolding:

```
src/alphamind/scheduler/control/
├── __init__.py
├── app.py            # FastAPI app composition + lifespan (lifespan integrates with supervisor.py)
├── routes.py         # 5 POST /control/* + 1 GET /events route handlers
├── verbs.py          # synchronous verb implementations (pause/resume/...) over driver/emergency/etc.
├── events.py         # SSEEventEmitter implementing ProgressEmitter + per-event-type Pydantic payloads
└── models.py         # Pydantic request / response / error models per the schema
```

### 2\. Pydantic boundary models (`models.py`)

One Pydantic model per request body, response envelope, error envelope, and per-event payload as defined in `pipeline-control-and-events-schema.md` `$defs`. Mirror enum names verbatim. Use Pydantic v2 `model_config = ConfigDict(extra="forbid")` for request bodies to match `additionalProperties: false` on empty-body verbs.

### 3\. FastAPI app + lifespan (`app.py`)

Compose the FastAPI app, mount `routes.py`, register a lifespan context that:

* Constructs the `SSEEventEmitter` and wires it into the orchestrator's `ProgressEmitter` injection (the supervisor already accepts a `ProgressEmitter`).
* Starts the Uvicorn server bound to `127.0.0.1:{port}` (port from `MainConfig` via a new `pipeline.control_port: int` field — default 8765; reuse `MainConfig` add-fields pattern).
* On shutdown, cancels the Uvicorn task inside the supervisor's `TaskGroup`.

### 4\. Verb implementations (`verbs.py`)

Five verbs. Each is a pure-ish function over injected dependencies:

* `pause(driver, reason) -> ControlResult` — sets scheduler-pause flag; returns `accepted` (idempotent on already-paused; returns original `applied_at`).
* `resume(driver) -> ControlResult` — clears scheduler-pause flag.
* `trigger_emergency_invocation(emergency_trigger, reason) -> TriggerEmergencyInvocationResult` — invokes `emergency.trigger(reason=..., source="operator_console")`; returns the assigned `invocation_id`. Surfaces `cooldown_active` and `precondition_failed` (already-running) as error envelopes per the schema's per-verb error table.
* `switch_profile(profile_name) -> ControlResult` — wraps `config.control_handlers.profile_switch.switch_active_profile`; raises `not_found` (404) if `profile_name` doesn't match a `config/profiles/*.yaml` file.
* `run_universe_validation() -> RunUniverseValidationResult` — synchronously runs `scripts/validate_universe.py` programmatically (not via subprocess — import the script's `main()` if it exposes one, or factor the validation entrypoint behind a function the script + this verb both call). Returns the typed report inline.

### 5\. SSE event emitter (`events.py`)

Class implementing the existing `ProgressEmitter` Protocol from `_kernel/progress.py`, additionally emitting the nine event types from the schema (`invocation_started`, `phase_transition`, `agent_started`, `agent_succeeded`, `agent_retrying`, `agent_failed`, `invocation_ended`, `next_trigger_changed`, `heartbeat`) onto an `asyncio.Queue` per subscriber. The `GET /events` route handler consumes its subscriber's queue and writes SSE frames per the schema's framing spec. `heartbeat` fires every 15 s when no other event has emitted on a connection. `next_trigger_changed` hooks into APScheduler's `next_run_time` recomputation in `driver.py`.

### 6\. Route handlers (`routes.py`)

Five `POST /control/*` handlers dispatching to `verbs.py`; one `GET /events` SSE handler. Use FastAPI's `StreamingResponse` for SSE with `media_type="text/event-stream"` and the required `Cache-Control: no-cache` + `Connection: keep-alive` headers. No request-id header (per schema § Scope).

### Out of scope

* The browser-facing `/api/control/*` proxy and `/api/events` multiplexer — story 04a and 04b inside the command center backend.
* Authentication on this surface — loopback isolation is the trust boundary; the proxy in story 04a owns auth.
* The `OperatorInvocationHandle` wrap on the caller side — story 02 + 04a.

## Acceptance criteria

- [ ] `src/alphamind/scheduler/control/` subpackage exists with `app.py`, `routes.py`, `verbs.py`, `events.py`, `models.py`.
- [ ] All Pydantic request / response / error / event-payload models exist with field shapes matching the schema's `$defs`.
- [ ] `pipeline.control_port: int` exists on the appropriate config model with default `8765`.
- [ ] The FastAPI app binds to `127.0.0.1:{control_port}` at scheduler startup and is torn down cleanly on shutdown.
- [ ] All five `POST /control/*` verbs respond per the per-verb HTTP status + body table in the schema doc.
- [ ] `GET /events` emits SSE-framed records with the documented `event:` + `data:` shape and a 15 s heartbeat when idle.
- [ ] `next_trigger_changed` events emit when the APScheduler `next_run_time` for a relevant job changes (pause, resume, emergency consumption).
- [ ] `invocation_started` carries `run_type: emergency` for emergency-triggered invocations and the same `invocation_id` returned by `trigger_emergency_invocation_response`.
- [ ] All cross-field invariants in the schema's § Notes on cross-field invariants are observable by a test (e.g., `phase_transition` is forward-only; `agent_succeeded` and `agent_failed` are mutually exclusive per `(invocation_id, agent_name)`).
- [ ] Tests under `tests/scheduler/control/` cover: each verb's happy path + each documented error envelope; SSE framing of each of the nine event types; heartbeat cadence; bind-to-loopback only (a TCP probe to a non-loopback IP fails).
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/scheduler/ -n auto`. Manual smoke: boot the scheduler with `uv run python -m alphamind.scheduler` against a synthetic invocation; in a second terminal, `curl http://127.0.0.1:8765/control/pause -d '{"reason":"smoke"}'` returns `200` with the documented body; `curl -N http://127.0.0.1:8765/events` shows SSE frames.