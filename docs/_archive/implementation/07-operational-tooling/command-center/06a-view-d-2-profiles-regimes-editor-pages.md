# 06a — View D-2 — Profiles + regimes editor pages

## Goal

Instantiate the config-editor framework from story 05i for the two operator-tunable config-file families that drive most adjustments: `config/profiles/*.yaml` and `config/regimes/*.yaml`. Both families feature object-array `rule_values` / multiplier tables that benefit most from the framework's `ObjectArrayTableEditor` control. Apply `ReloadPolicy` decorators to the relevant Pydantic field hints. No new framework code — purely instantiation + per-file schema decoration.

## Reading

* `docs/design/command-center.md` § Config editor — the row table mapping value types to controls.
* `docs/design/configuration-management.md` — full config-file inventory; this story covers profiles + regimes.
* `docs/design/06-risk-guardrails/rules-and-limits.md` § Transitioning between profiles — discipline the editor exposes on save.
* `src/alphamind/command_center/views/configuration.py` (from [ALP-679](https://linear.app/alphamind-jatassi/issue/ALP-679/05i-view-d-1-generic-config-editor-framework)) — the form-schema + save endpoints.
* `src/alphamind/command_center/frontend/src/views/configuration/framework/` (from [ALP-679](https://linear.app/alphamind-jatassi/issue/ALP-679/05i-view-d-1-generic-config-editor-framework)) — control library + FormComposer.
* The Pydantic models for `config/profiles/*.yaml` and `config/regimes/*.yaml` — find them in `src/alphamind/config/` and decorate fields with `ReloadPolicy`.

## Depends on

* [ALP-679](<https://linear.app/alphamind-jatassi/issue/ALP-679>) (05i) — config editor framework + ReloadPolicy decorator.

## Scope

Backend: `ReloadPolicy` decorator application on the profiles + regimes Pydantic models (changes under `src/alphamind/config/`). Frontend at `frontend/src/routes/_authed/config/profiles/$profileName.tsx` + `config/regimes/$regimeName.tsx` + `frontend/src/views/configuration/profiles/` + `configuration/regimes/`. Tests at `tests/command_center/views/test_profiles_editor.py` + `test_regimes_editor.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* Apply `ReloadPolicy.INVOCATION_RELOAD` to most profile / regime field hints (per the design doc — most config is invocation-time-reload).
* Discover and apply `ReloadPolicy.DEPLOY_ONLY` to fields like database paths if any sneak into profiles/regimes (they shouldn't — verify).
* No new endpoints; reuse `/api/views/config/schema/{config_file}` and `/api/views/config/{config_file}` from [ALP-679](https://linear.app/alphamind-jatassi/issue/ALP-679/05i-view-d-1-generic-config-editor-framework).
* Add a thin `GET /api/views/config/files?family=profiles` and `?family=regimes` endpoint returning the list of files in that directory (for the file-picker UI).

### 2\. Frontend

`/config/profiles/$profileName` route — uses `FormComposer` to render the profile's form. Table editor for `rule_values` with `ObjectArrayTableEditor` (one row per rule with per-cell type-matched controls). Transition-discipline reminder banner per `rules-and-limits.md § Transitioning between profiles`: when the operator saves a profile change, render a banner explaining the next-invocation reload semantics.

`/config/regimes/$regimeName` route — similar shape. Table editor for the per-rule multiplier table.

A profile-picker sidebar lists `config/profiles/*.yaml` files via `GET /api/views/config/files?family=profiles`; similarly for regimes.

### 3\. Vitest tests

Renders correctly for a representative profile + regime fixture; FormComposer dispatches to ObjectArrayTableEditor for `rule_values`; ReloadPolicyBadge renders for each field.

## Acceptance criteria

- [ ] `ReloadPolicy` applied to all profile + regime field hints.
- [ ] `GET /api/views/config/files?family=profiles|regimes` returns the file list.
- [ ] Frontend `/config/profiles/$profileName` and `/config/regimes/$regimeName` render dynamically against the framework.
- [ ] `rule_values` and per-rule multiplier tables render via `ObjectArrayTableEditor`.
- [ ] Transition-discipline reminder banner renders on save of a profile change.
- [ ] File-picker sidebar lists available profiles + regimes.
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ tests/config/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.