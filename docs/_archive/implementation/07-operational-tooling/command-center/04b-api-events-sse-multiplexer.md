# 04b — /api/events SSE multiplexer

## Goal

Ship the command-center backend's `/api/events` SSE surface: two long-lived background tasks subscribe to the pipeline `/events` and monitor `/events` SSE streams (from stories 01b and 01c); their event records flow into a shared async multiplexer; each connected browser gets a downstream SSE stream re-emitting both upstreams' records on a single connection. Upstream reconnect is independent — a pipeline drop does not kill the monitor subscription and vice versa. No `Last-Event-ID` resume — events are transient screen state per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (G). The multiplexer is also the event-source the alert engine (story 05a) subscribes to.

## Reading

* `docs/design/command-center.md` § Live event stream — multiplex contract + reconnect semantics.
* `docs/design/pipeline-control-and-events-schema.md` § Event schema — per-event payload shapes the pipeline upstream emits.
* `docs/design/monitor-control-and-events-schema.md` § Event schema — per-event payload shapes the monitor upstream emits.
* `src/alphamind/command_center/_kernel/events.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `PipelineEvent` / `MonitorEvent` frozen dataclasses + per-event-type payloads (extend the registry here).
* `src/alphamind/command_center/supervisor.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `asyncio.TaskGroup` the background SSE consumers join.
* `src/alphamind/command_center/auth/dependencies.py` (from [ALP-667](https://linear.app/alphamind-jatassi/issue/ALP-667/03-webauthn-sessions-csrf)) — `current_session` dependency the `/api/events` route gates behind.

## Depends on

* [ALP-664](<https://linear.app/alphamind-jatassi/issue/ALP-664>) (01b) — pipeline `/events` upstream.
* [ALP-665](<https://linear.app/alphamind-jatassi/issue/ALP-665>) (01c) — monitor `/events` upstream.
* [ALP-667](<https://linear.app/alphamind-jatassi/issue/ALP-667>) (03) — `current_session` dependency.

## Scope

In scope, under `src/alphamind/command_center/events/`. Tests at `tests/command_center/events/`.

### 1\. New `command_center/events/` subpackage

```
src/alphamind/command_center/events/
├── __init__.py
├── upstream_client.py      # base SSEUpstreamClient: connect, read frames, backoff-reconnect
├── pipeline_consumer.py    # bg TaskGroup task: subscribes to pipeline /events, parses, pushes to multiplexer
├── monitor_consumer.py     # bg TaskGroup task: subscribes to monitor /events, parses, pushes to multiplexer
├── multiplexer.py          # EventMultiplexer: async pubsub, per-subscriber asyncio.Queue
├── models.py               # Pydantic envelope: {source: "pipeline"|"monitor", event: <name>, data: <payload>}
└── routes.py               # GET /api/events SSE handler
```

### 2\. `SSEUpstreamClient` base (`upstream_client.py`)

Wraps `httpx.AsyncClient` for SSE consumption. Reconnect strategy: exponential backoff capped at 30 s, jitter. Detects upstream disconnect via idle-timeout > 30 s (the schema docs guarantee a 15 s heartbeat). Each parsed frame is a tuple `(event_name: str, data: dict)`.

### 3\. Pipeline / monitor consumers (`pipeline_consumer.py`, `monitor_consumer.py`)

Each consumer runs in its own `TaskGroup` task. Loop: connect → for each frame, parse against the matching Pydantic event payload → wrap in `PipelineEvent(source="pipeline", event_name, payload, received_at)` (or `MonitorEvent`) → push to multiplexer → repeat. On connection drop, reconnect independently of the sibling consumer. Failed parses are logged at WARNING and dropped (schema drift surfacing condition per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center)).

### 4\. `EventMultiplexer` (`multiplexer.py`)

```python
class EventMultiplexer:
    async def subscribe(self) -> AsyncIterator[CombinedEvent]:
        """Yields events from both upstreams in receipt order. Each subscriber
        has its own asyncio.Queue; broadcast publishes to all queues."""
  
    async def publish(self, event: CombinedEvent) -> None:
        """Push an event to every subscriber's queue. Queue full → drop oldest
        for that subscriber (slow-consumer protection); log at WARNING."""
```

`CombinedEvent` is a tagged union — `PipelineEvent | MonitorEvent` keyed by `source`. Subscribers (the SSE route + the alert engine in story 05a) consume via `async for event in mux.subscribe(): ...`. Story 05a will add a long-lived subscriber inside the supervisor TaskGroup.

### 5\. SSE route (`routes.py`)

`GET /api/events`. Gated by `Depends(current_session)`. Returns `StreamingResponse(media_type="text/event-stream")` that:

* Acquires a subscriber from the multiplexer.
* Emits SSE frames in the format `event: <pipeline|monitor>:<event_name>\ndata: <json payload>\n\n`. The dual `source:event_name` event name disambiguates pipeline `heartbeat` from monitor `heartbeat` at the browser.
* Emits its own 15 s heartbeat (`event: heartbeat\ndata: {}\n\n`) if no upstream events arrive — keeps the browser EventSource alive.
* Releases the subscriber on disconnect (browser tab close, network drop).

### 6\. Background tasks in supervisor

Story 02's supervisor exposes a `register_background_task(coro)` API; this story adds two registrations during app lifespan: `pipeline_consumer.run(...)` and `monitor_consumer.run(...)`. Both run inside the lifespan's `TaskGroup`; both shut down cleanly on app shutdown.

### Out of scope

* The alert engine that subscribes to this multiplexer — story 05a.
* Browser EventSource consumer + per-view live-update wiring — story 04c sets up the EventSource hook; per-view stories (05b Live ops) subscribe to it.

## Acceptance criteria

- [ ] `command_center/events/` subpackage exists with all six modules.
- [ ] `SSEUpstreamClient` connects to upstream `/events`, parses frames per the schema, reconnects on drop with exponential backoff.
- [ ] `pipeline_consumer` and `monitor_consumer` independently subscribe to their upstreams — a forced drop on one leaves the other connected.
- [ ] `EventMultiplexer` supports N subscribers; publishing pushes to all; slow-consumer protection drops oldest for that subscriber (verified by test).
- [ ] `GET /api/events` requires a valid session; gated by `Depends(current_session)`; returns SSE-framed records combining both upstreams.
- [ ] SSE event name is the `<source>:<event_name>` dual form (e.g. `pipeline:invocation_started`, `monitor:fill_received`); data payloads round-trip the upstream JSON unmodified.
- [ ] Backend 15 s heartbeat fires when no upstream events arrive within the window.
- [ ] Two background tasks register in the supervisor's TaskGroup at lifespan startup; both shut down cleanly on app shutdown.
- [ ] Tests under `tests/command_center/events/` cover: upstream parse-and-publish (each event type), independent reconnect, multiplexer fanout, slow-consumer drop, session gating on the SSE route, heartbeat cadence.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/events/ -n auto`. Real loopback wiring deferred to verify story 07.