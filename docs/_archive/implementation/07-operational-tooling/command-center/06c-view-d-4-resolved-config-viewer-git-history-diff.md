# 06c — View D-4 — Resolved config viewer + git history + diff

## Goal

Ship the two read-only configuration diagnostic views from view group D: the resolved-config viewer (the composed profile × regime × overlays × mode bundle the most recent invocation consumed, side-by-side with the source files) and the config history + diff viewer (per-file git history with diff rendering for tracked files, plus uncommitted-changes warning for untracked).

## Reading

* `docs/design/command-center.md` § Resolved config viewer and § Config history and diff.
* `docs/design/configuration-management.md` § Composition resolver — the resolver path that produces the bundle the viewer renders.
* `src/alphamind/state/invocation_context/` — where the per-invocation resolved-config snapshot is written (the pipeline scheduler writes one at invocation start).
* `git` CLI — `git log --oneline -- <path>` and `git diff <sha1>..<sha2> -- <path>` shell-outs the diff view wraps.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session, CommandCenterConfig.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/configuration.py` (extending 05i's module with `resolved_*` and `git_history_*` functions). Frontend at `frontend/src/routes/_authed/config/resolved.tsx` + `config/history.tsx` + `frontend/src/views/configuration/resolved/` + `history/`. Tests at `tests/command_center/views/test_resolved_config.py` + `test_git_history.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/config/resolved?invocation_id=` — returns the composed config bundle for the named invocation (or the most-recent if absent) plus the source files (profile base, regime multipliers, active overlays, mode behavioral transform) side-by-side. Reads the per-invocation snapshot file the composition resolver writes.
* `GET /api/views/config/resolved/diff?from_invocation_id=&to_invocation_id=` — returns the diff between two invocations' resolved configs (default: previous vs most-recent).
* `GET /api/views/config/git/history?file=` — git log for the named config file: list of commits with `sha`, `date`, `author`, `message`.
* `GET /api/views/config/git/diff?file=&from_sha=&to_sha=` — text diff between two SHAs for the file.
* `GET /api/views/config/git/status?file=` — whether the file is git-tracked + whether uncommitted changes exist (for the warning indicator).

Git operations shell out to `git` CLI (subprocess) — the alphamind.db file is git-tracked at the repo level, so this works in dev + prod (the prod machine ships the repo). Operations are read-only; no `git commit` per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (I).

### 2\. Frontend

`/config/resolved` route — side-by-side panes: resolved bundle on the left, source files (collapsible per profile / regime / overlay / mode) on the right. Diff-with-previous button → opens diff against the previous invocation in a modal.

`/config/history` route — file picker + commits list + diff viewer (Monaco editor with diff mode). For untracked files, renders "uncommitted changes" warning per the design doc; for tracked files with local mods, renders the uncommitted-since-last-commit diff in addition to the history.

### 3\. Vitest tests

Each view renders documented fields. Diff modal opens with correct from/to invocation IDs. Git operations don't shell-out in unit tests — mocked at the API client level.

## Acceptance criteria

- [ ] `GET /api/views/config/resolved` returns the bundle + source files for the named (or most-recent) invocation.
- [ ] `GET /api/views/config/resolved/diff` returns a structured diff between two invocations.
- [ ] `GET /api/views/config/git/history` returns commits for a tracked config file.
- [ ] `GET /api/views/config/git/diff` returns text diff between two SHAs.
- [ ] `GET /api/views/config/git/status` indicates tracked / untracked + uncommitted-changes state.
- [ ] Frontend `/config/resolved` renders side-by-side; diff-with-previous modal works.
- [ ] Frontend `/config/history` renders file picker + commits + diff in Monaco; uncommitted-changes warning visible for untracked or locally-modified files.
- [ ] No `git commit` writes — verified by reviewing the backend code for forbidden subprocess invocations.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.