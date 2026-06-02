# 06b — View D-3 — Alerts + security + other config editor pages

## Goal

Instantiate the config-editor framework from story 05i for the remaining top-level config files: `config/alerts.yaml` (alert rule registry — coordinates with story 05a's engine), `config/security.yaml` (session + WebAuthn parameters), `config/command-center.yaml` (bind + DB path + frontend dist), and any remaining smaller config files the operator should manage via GUI (`config/digest.yaml`, `config/universe.yaml`, etc.). Apply `ReloadPolicy` decorators per file. Each editor page is small relative to profiles/regimes — string + number + boolean + enum + small object-array editors suffice.

## Reading

* `docs/design/command-center.md` § Config editor and § Alerting (the alert rule registry editor coordinates with the engine).
* `docs/design/configuration-management.md` — full config-file inventory.
* `src/alphamind/command_center/alerts/rules.py` (from [ALP-671](https://linear.app/alphamind-jatassi/issue/ALP-671/05a-alert-engine-discord-channel-acksnooze-apis)) — `AlertRule` Pydantic shape the alerts editor renders.
* `src/alphamind/command_center/views/configuration.py` (from [ALP-679](https://linear.app/alphamind-jatassi/issue/ALP-679/05i-view-d-1-generic-config-editor-framework)) — endpoints to reuse.

## Depends on

* [ALP-679](<https://linear.app/alphamind-jatassi/issue/ALP-679>) (05i) — config editor framework.
* [ALP-671](<https://linear.app/alphamind-jatassi/issue/ALP-671>) (05a) — `AlertRule` shape + AlertsConfig the alerts editor edits + the alert engine that reloads on file change.

## Scope

Backend: `ReloadPolicy` decorator application on `AlertsConfig`, `SecurityConfig`, `CommandCenterConfig`, and the other top-level Pydantic models. Hot-reload integration for `alerts.yaml` (the engine watches the file mtime + reloads rules on change). Frontend at `frontend/src/routes/_authed/config/{alerts,security,command-center,digest,universe}.tsx` + `frontend/src/views/configuration/` per-file components. Tests at `tests/command_center/views/test_other_config_editors.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* Apply `ReloadPolicy` to `AlertsConfig`, `SecurityConfig`, `CommandCenterConfig` fields. Most are `INVOCATION_RELOAD`; bind host + DB path + WebAuthn relying-party are `DEPLOY_ONLY`.
* Alert engine hot-reload: extend story 05a's `AlertEngine` to watch `config/alerts.yaml` mtime; on change, re-parse + diff against current rule set; apply additions / removals / updates without restart. Restart-required only for changes to non-rules sections.
* No new endpoints beyond what 05i shipped.

### 2\. Frontend

One route per config file under `/config/<file>`:

* `/config/alerts` — alerts rule registry editor. Uses `ObjectArrayTableEditor` for the rules list with per-cell controls (name string, severity enum, debounce timedelta number, channels string-array). Save success surfaces "Alert engine reloaded" toast.
* `/config/security` — session + WebAuthn parameters. Cookie names, session duration, relying-party fields. Most fields render as simple `NumberInput` / `StringInput`.
* `/config/command-center` — bind host + port, DB path, frontend dist path. `DEPLOY_ONLY` reload-policy badges visible.
* `/config/digest` — notable-shift thresholds from existing `config/digest.yaml` (multiple sub-blocks).
* `/config/universe` — universe inclusion / exclusion lists (if `config/universe.yaml` exists; if not, omit).

A nav-bar entry under "Configuration" lists each editor.

### 3\. Vitest tests

Each editor renders correctly against fixture data; ObjectArrayTableEditor add/remove rows for the alerts case; ReloadPolicyBadge renders correctly for DEPLOY_ONLY fields.

## Acceptance criteria

- [ ] `ReloadPolicy` applied to `AlertsConfig`, `SecurityConfig`, `CommandCenterConfig` fields with documented INVOCATION_RELOAD vs DEPLOY_ONLY classification.
- [ ] Alert engine hot-reloads `config/alerts.yaml` on mtime change; rule additions / removals / updates apply without process restart; non-rules-section changes surface a "restart required" banner.
- [ ] Frontend `/config/alerts`, `/config/security`, `/config/command-center`, `/config/digest` (and `/config/universe` if applicable) routes render correctly.
- [ ] Alerts editor's `ObjectArrayTableEditor` supports add / remove / edit rows; save success surfaces engine-reload toast.
- [ ] DEPLOY_ONLY badges visible on relevant fields with explanatory tooltip.
- [ ] Nav-bar lists each editor under "Configuration".
- [ ] Vitest tests pass.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ tests/command_center/alerts/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.