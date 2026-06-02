# 05i — View D-1 — Generic config editor framework

## Goal

Ship the generic, type-matched form framework that subsequent editor stories (06a profiles+regimes, 06b alerts+security+other) instantiate per config file. Includes the value-type control library (number with unit suffix, boolean toggle, enum dropdown, string with regex, string-array tag editor, object-array table editor, cron expression picker, path input), three-layer inline validation surfacing (parse-time / cross-reference / semantic), reload-policy badges (invocation-time-reload vs deploy-time-only), and atomic-file-replace save discipline using the existing `_kernel/atomic_io` primitive. No per-file editor in this story.

## Reading

* `docs/design/command-center.md` § Config editor — value-type → control mapping, validation layers, reload-policy badges.
* `docs/design/configuration-management.md` § Validation — three validation layers + their error surfacing.
* `src/alphamind/_kernel/atomic_io.py` — atomic file write primitive the save action wraps per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (I).
* `src/alphamind/command_center/auth/dependencies.py` (from [ALP-667](https://linear.app/alphamind-jatassi/issue/ALP-667/03-webauthn-sessions-csrf)) — `current_session`, `csrf_required` dependencies.
* `src/alphamind/command_center/frontend/src/components/ui/` (from [ALP-670](https://linear.app/alphamind-jatassi/issue/ALP-670/04d-frontend-foundation)) — shadcn/ui primitives the controls extend.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02) — foreign_reader_session, FastAPI app.
* [ALP-670](<https://linear.app/alphamind-jatassi/issue/ALP-670>) (04d) — frontend foundation.

## Scope

Backend at `src/alphamind/command_center/views/configuration.py` (the form-schema endpoint + the atomic-save endpoint). Frontend at `frontend/src/views/configuration/framework/` (per-control-type React components + the dynamic form composer + validation surfacing). Tests at `tests/command_center/views/test_configuration_framework.py` + Vitest. Frontend toolchain is bun per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (J).

### 1\. Backend

* `GET /api/views/config/schema/{config_file}` — returns the form-schema metadata for one config file: per-leaf type + control type + constraints + reload-policy badge. The schema is derived from the file's existing Pydantic v2 model via `.model_json_schema()` plus an in-tree `ReloadPolicy` decorator (introduced in this story) on each field.
* `PUT /api/views/config/{config_file}` — atomic file write: writes YAML to `{path}.tmp`, fsync, rename. Validates the proposed YAML against all three validation layers (parse, cross-reference, semantic — reuse the existing `config/` validator chain); rejects on failure with the layered error envelope.

### 2\. Frontend framework components

Under `frontend/src/views/configuration/framework/`:

* `NumberInput` (with unit suffix + min/max enforcement)
* `BooleanToggle`
* `EnumDropdown`
* `StringInput` (with regex validation)
* `StringArrayTagEditor` (add / remove chip)
* `ObjectArrayTableEditor` (row add / remove, per-cell type-matched control)
* `CronExpressionPicker` (structured every-N-hours-at-HH:MM with read-only cron preview)
* `PathInput` (with file-existence indicator from a thin `GET /api/views/config/path-exists?path=...` backend probe)
* `FormComposer` (renders a full form from a schema; dispatches to per-control components)
* `ValidationBanner` (page-level for cross-ref + semantic errors)
* `ReloadPolicyBadge` (per-field; reads from schema metadata)

`SaveAction` button — gated on all three validation layers passing client-side; PUTs to the backend; on success, surfaces a toast + reload-policy reminder if any deploy-time-only fields changed.

### 3\. Vitest tests

Each control component renders + validates; the FormComposer dispatches correctly; ValidationBanner renders layered errors; SaveAction PUT body matches the form state.

### Out of scope

* Per-file editor instantiations — stories 06a (profiles+regimes), 06b (alerts+security+other), 06c (resolved viewer + diff).

## Acceptance criteria

- [ ] `GET /api/views/config/schema/{config_file}` returns form-schema metadata derived from the file's Pydantic model + reload-policy decorators.
- [ ] `PUT /api/views/config/{config_file}` writes atomically via `_kernel/atomic_io` (write `.tmp`, fsync, rename); validates all three layers before writing; rejects with layered envelope on failure.
- [ ] `ReloadPolicy` decorator exists, applied to relevant Pydantic field hints in each config model (this story decorates `CommandCenterConfig`, `SecurityConfig`, `AlertsConfig` — extends to other configs in 06a/06b).
- [ ] All eight control component types exist with documented behavior; FormComposer assembles them dynamically.
- [ ] ValidationBanner renders parse-time field-level errors + cross-ref / semantic page-level errors.
- [ ] SaveAction is gated on client-side validation; success path POSTs to backend; success and failure render appropriate toasts.
- [ ] Vitest tests pass for each control + the FormComposer + the SaveAction.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports`, frontend `bun run typecheck && bun run lint && bun run test` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/views/ -n auto`. Frontend: `cd src/alphamind/command_center/frontend && bun run test`.