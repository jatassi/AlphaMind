# 05d — View B-2 — Per-invocation detail + source-brief viewer

## Goal

Ship the per-invocation detail page from view group B — a single navigable page rendering the full graph for one `invocation_id`: header (run type, durations, agent metrics), distillation outputs, analysis briefs, analyst output, strategist output, pre-processor bundle, PM envelopes, commands and fills. Plus the source-brief retrieval store viewer (folded into this story per the operator's view-fold), reachable both as a sub-route and via deep links from reference IDs (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N`) rendered inline.

## Reading

* `docs/design/command-center.md` § Per-invocation detail (the full pane table) and § Source-brief retrieval store viewer.
* `docs/design/03-analysis-layer/synthesizer.md` — reference-ID taxonomy (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N`) and source-brief retrieval store layout.
* `docs/design/04-decision-layer/pm-envelope-schema.md` — PM envelope shape for the PM envelopes pane.
* `docs/architecture/infrastructure.md` § Layer 2 — `%USERPROFILE%/AlphaMind/archive/<date>/<time>_<run_type>/` archive directory layout.
* `src/alphamind/state/tables/invocations.py` — invocations row shape.
* `src/alphamind/state/tables/activity_log.py` — activity_log shape (for the commands + fills pane filter).

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/history.py` (extending the module from 05c with `invocation_detail_*` functions + `brief_retrieval_*` functions). Frontend at `frontend/src/routes/_authed/history/$invocationId.tsx` + `frontend/src/views/history/invocation-detail/` (one component per pane) + `frontend/src/views/history/brief-viewer.tsx`. Tests at `tests/command_center/views/test_invocation_detail.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/history/runs/{invocation_id}` — invocation header + per-pane data: archive-file paths + parsed-content readiness flags, pm_envelope activity_log entries, command + fill activity_log entries filtered by `invocation_id`.
* `GET /api/views/history/runs/{invocation_id}/archive/{section}/{filename}` — streams the raw markdown / JSON archive file (distillation outputs, analysis briefs, etc.). Path-traverses safely (the URL params must resolve under `%USERPROFILE%/AlphaMind/archive/{date}/{time}_{run_type}/{section}/{filename}` with `..` rejected).
* `GET /api/views/brief-retrieval?invocation_id=&ref_prefix=` — returns brief sections keyed by reference-ID prefix from the existing synthesizer source-brief retrieval store.

### 2\. Frontend route + components

`/history/$invocationId` route — single-page render with vertical sections per the design doc's pane table. Header pinned at top; sections collapsible. Each PM envelope renders as a card with verdict pill, criterion pass/fail list, anti-pattern tags, rationale narrative — rejected and approved envelopes render with equal prominence per the design doc.

Reference-ID chips (`SA-TECH-1`, `QR-3`, etc.) inline in narrative text are clickable; click opens the brief viewer in a side panel + scrolls to the matching section.

`/history/brief-viewer?invocation_id=...&ref_prefix=SA-TECH` — standalone brief-viewer route for direct linking. Same component the side panel uses.

### 3\. Vitest tests

Each pane component renders documented fields against fixture data. Reference-ID chip click opens side panel. PM envelope cards render rejected variants prominently.

### Out of scope

* `agent_calls` table data (system_prompt_path / git_sha / snapshot_reference) — F-group; the "active prompt per invocation viewer" the design references is deferred.

## Acceptance criteria

- [ ] `GET /api/views/history/runs/{invocation_id}` returns documented per-pane data.
- [ ] `GET /api/views/history/runs/{invocation_id}/archive/{section}/{filename}` streams archive files with path-traversal protection.
- [ ] `GET /api/views/brief-retrieval` returns brief sections keyed by reference-ID prefix from the source-brief retrieval store.
- [ ] Frontend `/history/$invocationId` renders all design-doc-named panes.
- [ ] Reference-ID chips open the brief viewer side panel.
- [ ] Rejected PM envelopes render with the same prominence as approved.
- [ ] Direct deep-link `/history/brief-viewer?...` renders the brief viewer standalone.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.