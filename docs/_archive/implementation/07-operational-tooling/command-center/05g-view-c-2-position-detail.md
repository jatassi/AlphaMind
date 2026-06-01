# 05g — View C-2 — Position detail

## Goal

Ship the per-position detail page from view group C: three tabs (state, thesis, history) per the design doc. Includes the Force Close action button gated by typed-token confirmation (proxies via `/api/control/force_close_position` from story 04a).

## Reading

* `docs/design/command-center.md` § Position detail — three-tab layout: state, thesis, history.
* `src/alphamind/state/tables/positions.py`, `theses.py`, `thesis_components.py`, `brackets.py`, `bracket_legs.py`, `fill_records.py` — table shapes.
* `src/alphamind/command_center/control/routes.py` (from [ALP-668](https://linear.app/alphamind-jatassi/issue/ALP-668/04a-apicontrol-proxy-audit)) — `/api/control/force_close_position` for the Force Close button.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-668](<https://linear.app/alphamind-jatassi/issue/ALP-668>) (04a) — `/api/control/force_close_position` endpoint for the Force Close button.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/portfolio.py` (extending 05f's module with `position_detail_*` functions). Frontend at `frontend/src/routes/_authed/portfolio/positions/$positionId.tsx` + `frontend/src/views/portfolio/position-detail/`. Tests at `tests/command_center/views/test_position_detail.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/portfolio/positions/{position_id}` — position row + bracket legs + fill history + linked thesis with all components (thesis_components joined) + activity_log filtered by `position_id` (uses the same shape as the activity log explorer endpoint from 05e, filter pre-applied).

### 2\. Frontend

`/portfolio/positions/$positionId` route — three tabs (TanStack Router child routes or Shadcn/UI Tabs):

* **State tab** — current position fields, bracket legs table, fill history table.
* **Thesis tab** — thesis summary; one card per component keyed by component type (entry rationale, target rationale, invalidation rationale per leg) with narrative + key assumptions + linked bracket-leg / order. Status transitions vertical timeline on the right with cited signal per transition.
* **History tab** — embeds the activity log explorer component (from 05e) with `position_id` filter pre-applied.

Force Close button in the page header — confirmation modal demanding the operator type the position ticker before submit. POSTs to `/api/control/force_close_position` with rationale; surfaces success / error toast.

### 3\. Vitest tests

Each tab renders documented fields. Force Close confirmation requires typed ticker match. Activity log embed pre-filters correctly.

### Out of scope

* Thesis-level edit / annotation — out of scope for v1.

## Acceptance criteria

- [ ] `GET /api/views/portfolio/positions/{position_id}` returns position + bracket legs + fills + thesis + components + activity_log entries.
- [ ] Frontend `/portfolio/positions/$positionId` renders three tabs with documented content.
- [ ] Force Close button gated by typed-token confirmation; success POSTs to `/api/control/force_close_position`; error envelope renders inline.
- [ ] Activity log tab pre-filters by `position_id`.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.