# 05h — View C-3 — Theses dashboard + thesis detail

## Goal

Ship the theses dashboard (filterable list of active / resolved / cancelled theses) and the thesis detail page (full thesis with summary, all components, status history with cited signals, resolution outcome if closed). Both from view group C in the design doc.

## Reading

* `docs/design/command-center.md` § Theses dashboard and § Thesis detail.
* `docs/design/05-execution-layer/thesis-model.md` — thesis model: components, statuses, status transitions, resolution categories.
* `src/alphamind/state/tables/theses.py`, `thesis_components.py` — table shapes.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/portfolio.py` (extending with `theses_*` and `thesis_detail_*` functions). Frontend at `frontend/src/routes/_authed/portfolio/theses/index.tsx` + `theses/$thesisId.tsx` + `frontend/src/views/portfolio/theses/`. Tests at `tests/command_center/views/test_theses.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/portfolio/theses?status=&classification=&sector=&age_days_gt=&resolution_category=&page=&page_size=` — paginated theses with documented filter dimensions.
* `GET /api/views/portfolio/theses/{thesis_id}` — thesis row + components + status history (extracted from activity_log entries with `thesis_id` filter) + resolution outcome (per-component + thesis-level) if resolved.

### 2\. Frontend

`/portfolio/theses` route — TanStack Table with columns per the design doc: summary, current status with `prior_status` arrow on recent transitions, linked position chip, age, P/L of underlying position. Filter controls in header.

`/portfolio/theses/$thesisId` route — full thesis renders: summary; all components with type + narrative; status history vertical timeline with cited signal per transition (reference-ID chips clickable → brief viewer side panel from 05d); resolution outcome if closed.

### 3\. Vitest tests

Each view renders documented fields. Status filter chips toggle. Reference-ID chips deep-link to brief viewer.

## Acceptance criteria

- [ ] `GET /api/views/portfolio/theses` returns paginated theses with all documented filters.
- [ ] `GET /api/views/portfolio/theses/{thesis_id}` returns thesis + components + status history + resolution.
- [ ] Frontend `/portfolio/theses` and `/portfolio/theses/$thesisId` render documented content.
- [ ] Status transitions vertical timeline renders cited signals with clickable reference-ID chips.
- [ ] Resolution outcome renders for closed theses.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.