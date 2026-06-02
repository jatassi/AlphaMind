# 05b — View A — Live run watcher + schedule preview

## Goal

Ship view group A from the design doc: the live run watcher (pipeline status pane + continuous monitor pane + active alerts banner) and the schedule preview (next several APScheduler triggers + pause/overlap state). Landing surface of the command center. Subscribes to the multiplexed `/api/events` SSE stream for live updates; falls back to one-shot `/api/views/live` reads on connection drop.

## Reading

* `docs/design/command-center.md` § A. Live operations — pane breakdown for live run watcher and schedule preview.
* `docs/design/pipeline-control-and-events-schema.md` § Per-event summary — pipeline events the watcher renders.
* `docs/design/monitor-control-and-events-schema.md` § Per-event summary — monitor events the watcher renders.
* `src/alphamind/command_center/events/multiplexer.py` (from [ALP-669](https://linear.app/alphamind-jatassi/issue/ALP-669/04b-apievents-sse-multiplexer)) — server-side multiplexer; this story's backend reads from it for one-shot state assembly.
* `src/alphamind/command_center/frontend/src/api/events.ts` (from [ALP-670](https://linear.app/alphamind-jatassi/issue/ALP-670/04d-frontend-foundation)) — `useEventStream()` hook the view consumes.
* `src/alphamind/command_center/control/routes.py` (from [ALP-668](https://linear.app/alphamind-jatassi/issue/ALP-668/04a-apicontrol-proxy-audit)) — the control verbs the watcher's action buttons call (Pause / Resume / Trigger emergency invocation).
* `src/alphamind/command_center/alerts/routes.py` (from [ALP-671](https://linear.app/alphamind-jatassi/issue/ALP-671/05a-alert-engine-discord-channel-acksnooze-apis)) — `/api/alerts` the active-alerts banner pane consumes.

## Depends on

* [ALP-668](<https://linear.app/alphamind-jatassi/issue/ALP-668>) (04a) — control verb buttons.
* [ALP-669](<https://linear.app/alphamind-jatassi/issue/ALP-669>) (04b) — SSE event source.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/live_operations.py`; frontend route + components at `command_center/frontend/src/routes/_authed/live.tsx` + `frontend/src/views/live/`. Tests Python at `tests/command_center/views/test_live_operations.py`, frontend at `frontend/src/views/live/__tests__/`. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend (`views/live_operations.py`)

`GET /api/views/live` — one-shot snapshot: current `invocations` row (if any in-flight; otherwise most-recent), monitor session state (websocket connected, time-since-connect, last fill timestamp, breach state, greeks freshness per underlying), active alerts (delegating to alerts.persistence.list_active), next scheduled trigger (read from APScheduler via a thin readback path on the pipeline control surface). All reads via `foreign_reader_session` from story 02 except alerts which uses `cc_writer_session`.

`GET /api/views/schedule` — next 5 trigger fires + pause state. Reads APScheduler state via the pipeline `/control/*` surface (add a `GET /control/schedule_preview` readback verb to story 01b's tree — or, more pragmatically, expose APScheduler's next-fire data via an in-process API since pipeline + command center are separate processes... the cleanest path is a new `next_trigger_changed` event-driven read: the backend caches the last seen `next_trigger_changed` event + serves it from cache).

Choose the cache-from-events approach. The cache lives in `command_center/views/live_operations.py` as a module-level dict updated by an EventMultiplexer subscriber.

### 2\. Frontend route (`routes/_authed/live.tsx`)

TanStack Router route under the `_authed` layout. Renders three panes side-by-side:

* **Pipeline status pane** — current invocation ID, run type, phase progress with per-agent latency-budget anchor, retry attempts. Subscribes to `pipeline:*` events from `useEventStream()`; falls back to `GET /api/views/live` on connect / reconnect.
* **Continuous monitor pane** — websocket state, time-since-connect, fill buffer depth, per-underlying greeks freshness, breach-detector state. Subscribes to `monitor:*` events.
* **Active alerts banner** — top-of-page sticky strip; renders `GET /api/alerts` results + subscribes to `cc:alert_fired` events; severity color-coded; click → alert detail.

Action buttons in the pipeline pane: Pause / Resume / Trigger emergency invocation. Each opens a confirmation modal (typed token for emergency invocation per § Operator actions) and POSTs to the matching `/api/control/*` endpoint.

### 3\. Schedule preview component (`views/live/schedule-preview.tsx`)

Small panel below the main three panes. Renders the next 5 trigger fires + pause state from `GET /api/views/schedule`. Refreshes on `pipeline:next_trigger_changed` events.

### 4\. Vitest tests

`live.tsx`, `pipeline-pane.tsx`, `monitor-pane.tsx`, `alert-banner.tsx`, `schedule-preview.tsx` each get a Vitest unit test using `@testing-library/react`: render with mocked `useEventStream()` + mocked TanStack Query state; assert documented field rendering + action-button behavior.

### Out of scope

* Per-invocation detail page — story 05d (clicking an invocation in the watcher deep-links to it).
* Position-level navigation — story 05g (clicking a position in the monitor pane's fill list deep-links to position detail).

## Acceptance criteria

- [ ] `GET /api/views/live` returns documented panes' data.
- [ ] `GET /api/views/schedule` returns next 5 triggers + pause state from the event-driven cache.
- [ ] Frontend route `/live` mounts under `_authed`; renders three panes + schedule preview + alert banner.
- [ ] SSE subscriptions live-update each pane; connection drop falls back to one-shot reads + auto-resumes.
- [ ] Pause / Resume / Trigger-emergency buttons gated by confirmation; emergency uses typed-token confirmation; success surfaces a toast; failure renders the upstream error envelope.
- [ ] Active-alerts banner reflects new alerts within 1 s of `cc:alert_fired` event; click navigates to alert detail (a stub route is fine if the detail view isn't implemented yet).
- [ ] Vitest tests pass for each component.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`. Manual smoke deferred to verify story 07.