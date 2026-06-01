# 05e — View B-3 — Activity log explorer

## Goal

Ship the activity log explorer: filterable view over the full `activity_log` table, the workhorse "what happened and why" surface. Filter dimensions per the design doc: event type (\~26 entries), `invocation_id`, position/thesis/order ID, source subsystem (including the saved-filter preset "Operator actions" with `source=operator_console`), time range. Result table is event type + timestamp + primary entity link + one-line summary; row expansion reveals full event detail JSON.

## Reading

* `docs/design/command-center.md` § Activity log explorer.
* `docs/design/05-execution-layer/state-persistence.md` — activity_log event type taxonomy + detail_json shapes.
* `src/alphamind/state/tables/activity_log.py` — table shape.
* `src/alphamind/portfolio_state/events/__init__.py` — `EVENT_TYPE_TO_DETAIL_CLASS` registry for typed detail decoding.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/activity_log.py`. Frontend at `frontend/src/routes/_authed/activity-log.tsx` + `frontend/src/views/activity-log/`. Tests at `tests/command_center/views/test_activity_log.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/activity-log?event_type=&invocation_id=&position_id=&thesis_id=&order_id=&source=&time_from=&time_to=&page=&page_size=` — paginated activity_log rows with multi-select filter dimensions. Reads via `foreign_reader_session`. Page size capped at 200.
* `GET /api/views/activity-log/event-types` — returns the EventType enum values for filter chip population.
* `GET /api/views/activity-log/saved-filters` — returns built-in saved filters; v1 ships one: "Operator actions" with `source=operator_console`. (Custom saved filters defer to F-group; not in this story.)

### 2\. Frontend

`/activity-log` route — TanStack Table over the endpoint; filter controls in the header (multi-select chips for event_type + source, free-text inputs for the entity IDs, date pickers for time range). Row expansion renders the detail_json (decoded via the typed `EventType → Detail` mapping shipped by the backend in the response payload). Saved-filter sidebar shows "Operator actions" preset.

Entity-ID chips in the row (position_id, thesis_id, order_id, envelope_id) are clickable — deep-link to the matching detail view (position detail, thesis detail, etc.; stub destinations are fine for views that aren't built yet).

### 3\. Vitest tests

Component renders documented columns; filter chips toggle; row expansion shows detail_json; entity-ID chips navigate.

### Out of scope

* Custom saved filters (operator-named, persisted) — F-group; the `saved_queries` table defers per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Scope.

## Acceptance criteria

- [ ] `GET /api/views/activity-log` returns paginated activity_log rows respecting all documented filter dimensions.
- [ ] `GET /api/views/activity-log/event-types` returns the EventType enum.
- [ ] `GET /api/views/activity-log/saved-filters` returns the "Operator actions" preset.
- [ ] Frontend `/activity-log` route renders the documented table + filter controls + saved-filter sidebar.
- [ ] Row expansion shows the typed-decoded detail_json.
- [ ] Entity-ID chips deep-link to the matching detail view route (stub destinations OK for unbuilt views).
- [ ] Filter state persists in URL search params.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.