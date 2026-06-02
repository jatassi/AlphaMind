# 05c — View B-1 — Run history + failure filter

## Goal

Ship the paginated, filterable list of past pipeline invocations from view group B's "Run history" and "Failure and abort log" entries in the design doc. One view; "Failure and abort log" is a preset filter (status ∈ {failed, partial}). Row click deep-links to per-invocation detail (story 05d).

## Reading

* `docs/design/command-center.md` § B. History and diagnostics — Run history and Failure and abort log entries (the failure log is folded into this story as a preset filter per the operator's scope fold).
* `src/alphamind/state/tables/invocations.py` — `invocations` table shape this view reads.
* `src/alphamind/command_center/persistence/session.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `foreign_reader_session()` factory.
* `src/alphamind/command_center/frontend/src/api/queries.ts` (from [ALP-670](https://linear.app/alphamind-jatassi/issue/ALP-670/04d-frontend-foundation)) — TanStack Query hook convention.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session, CommandCenterConfig.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/history.py` (the `run_history_*` functions). Frontend route + components at `frontend/src/routes/_authed/history/index.tsx` + `frontend/src/views/history/run-list.tsx`. Tests at `tests/command_center/views/test_run_history.py` + frontend Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

`GET /api/views/history/runs?date_from=&date_to=&run_type=&status=&commands_gt=&has_errors=&page=&page_size=` — paginated list of `invocations` rows. Filters per the design doc: date range, run type, status (`completed | failed | partial`), command-count-greater-than, has-errors. Default sort: started_at DESC. Page size capped at 100. Reads via `foreign_reader_session`.

`GET /api/views/history/runs/preset/failure-log?...` — convenience preset with status ∈ {failed, partial} pre-applied. Same query-param shape minus the `status` filter.

### 2\. Frontend route + components

`/history` route under `_authed/history/index.tsx` — TanStack Table over `GET /api/views/history/runs`. Column set per the design doc: `invocation_id`, started_at, duration, run type, status, # commands, # rejections, abort reason. Filter controls in the table header (date pickers, multi-select chips). Row click → `navigate({ to: '/history/$invocationId', params: { invocationId } })`.

`/history/failures` route — same component reusing the preset endpoint; nav-bar entry "Failure log" links here.

### 3\. Vitest tests

Component renders the documented columns; filter state round-trips through URL search params (TanStack Router's search-params API); pagination works.

### Out of scope

* Per-invocation detail page — story 05d.

## Acceptance criteria

- [ ] `GET /api/views/history/runs` returns paginated invocations with all documented filter dimensions; verified by tests against an in-memory DB seeded with diverse rows.
- [ ] `GET /api/views/history/runs/preset/failure-log` returns rows with `status ∈ {failed, partial}` only.
- [ ] Frontend `/history` route renders the documented columns; filter controls work; row click navigates to per-invocation detail route (a stub destination is fine if 05d isn't done).
- [ ] Filter state persists in URL search params; back/forward navigation preserves filters.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.