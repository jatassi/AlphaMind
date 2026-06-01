# 02 — Command center backend foundation

## Goal

Replace the placeholder `command_center/` namespace with the real package skeleton — FastAPI app composition + Uvicorn lifespan, configuration models for `command-center.yaml` + `security.yaml` + `alerts.yaml`, the four command-center-owned SQL tables (`alerts`, `webauthn_credentials`, `operator_sessions`, plus an `operator_actions` audit-buffer table if the activity_log binding shape demands it), the DB-access split (read-only `aiosqlite` for foreign tables + writer scoped to command-center-owned tables), and the `OperatorInvocationHandle` primitive that every operator action wraps. This story is the foundation every subsequent story imports from — name the `_kernel/` types, the `app.py` composition root, and the persistence layer concretely enough that no follow-on story needs to re-derive them.

Wants Opus despite the mechanical-feeling table-creation surface — naming the `_kernel/` types and getting the DB-access split right is judgment-heavy.

## Reading

* `docs/design/command-center.md` § Architecture context, § Tech stack — Backend, § Persistence boundary, § Authentication (for the credential-storage tables); the backend stack is FastAPI + Uvicorn + SQLAlchemy 2.0 + aiosqlite.
* `src/alphamind/command_center/__init__.py` and `command_center/backend/__init__.py` — current placeholder; replace cleanly.
* `src/alphamind/scheduler/__main__.py`, `scheduler/supervisor.py`, `scheduler/session.py`, `scheduler/logging_setup.py` — sibling daemon's lifecycle shape this package mirrors.
* `src/alphamind/state/invocation_context/context.py` and `invocation_context/records.py` — `InvocationHandle` + invocation row shape the new `OperatorInvocationHandle` extends with `run_type: operator_console`.
* `src/alphamind/_kernel/atomic_io.py` — atomic file-write primitive the future config editor stories rely on (story 05i bakes it into the editor framework).
* `src/alphamind/_kernel/ids.py` (or equivalent) — existing NewType conventions to mirror.
* `src/alphamind/state/tables/__init__.py` and existing table modules (e.g. `state/tables/activity_log.py`) — SQLAlchemy declarative-table convention + `_money_column.py` style.
* `src/alphamind/persistence/session.py` — existing engine/session factory; new code must coexist with it.
* `src/alphamind/persistence/migrations/versions/` — Alembic head for chained migration ordering after [ALP-663](<https://linear.app/alphamind-jatassi/issue/ALP-663>) lands.
* [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (D), (H) — InvocationHandle binding shape + DB-access split discipline.

## Depends on

* [ALP-663](<https://linear.app/alphamind-jatassi/issue/ALP-663>) (01a, this work tree) — Alembic migration ordering (this story's new tables migration lands after the PROFILE_SWITCHED migration to keep a linear head).

## Scope

In scope, all under `src/alphamind/command_center/`. Migration under `src/alphamind/persistence/migrations/versions/`. Config files at `config/`. Tests at `tests/command_center/{_kernel,persistence}/` and `tests/persistence/migrations/`.

### 1\. Package skeleton

```
src/alphamind/command_center/
├── __init__.py             # public surface (re-exports app + run() + version constant)
├── __main__.py             # python -m alphamind.command_center entrypoint
├── app.py                  # FastAPI app composition: app = FastAPI(lifespan=lifespan); included routers TBD by later stories
├── config.py               # CommandCenterConfig (Pydantic), loaders for command-center.yaml + security.yaml + alerts.yaml
├── session.py              # ProcessSession context manager (signal handling, NSSM-friendly logging redirect)
├── supervisor.py           # asyncio.TaskGroup-rooted supervisor: lifespan task + future SSE-consumer tasks (added by 04b) + future alert-engine task (added by 05a)
├── logging_setup.py        # mirrors scheduler/logging_setup.py
│
├── _kernel/
│   ├── __init__.py
│   ├── ids.py              # OperatorSessionId, WebauthnCredentialId (base64url str), AlertId, AlertRuleName, DiscordWebhookUrl
│   ├── control.py          # ControlVerb, ControlErrorCode StrEnums; ControlResult frozen dataclass
│   ├── events.py           # PipelineEvent / MonitorEvent frozen dataclasses + per-event-type payloads (populated more by 04b)
│   └── operator_invocation.py  # OperatorInvocationHandle async context manager + RUN_TYPE_OPERATOR_CONSOLE constant
│
└── persistence/
    ├── __init__.py
    ├── tables.py           # SQLAlchemy declarative tables for alerts, webauthn_credentials, operator_sessions
    ├── codecs.py           # row ↔ frozen-dataclass converters
    └── session.py          # AsyncEngineFactory: rw scoped to cc tables, ro for everything else
```

### 2\. Configuration models

`config/command-center.yaml` (new file), `config/security.yaml` (new file), `config/alerts.yaml` (new file — empty rule list, populated by story 05a).

```yaml
# command-center.yaml — load via CommandCenterConfig
bind:
  host: "127.0.0.1"           # or LAN IP for off-machine access; v1 stays local
  port: 8080
db:
  alphamind_db_path: "%USERPROFILE%/AlphaMind/data/alphamind.db"
frontend:
  dist_path: "src/alphamind/command_center/frontend/dist"

# security.yaml — load via SecurityConfig
session:
  duration_hours: 12
  cookie_name: "cc_session"
csrf:
  cookie_name: "cc_csrf"
webauthn:
  relying_party_id: "localhost"
  relying_party_name: "AlphaMind Command Center"

# alerts.yaml — load via AlertsConfig
rules: []   # populated by 05a
channels:
  discord:
    webhook_url_env: "ALPHAMIND_DISCORD_WEBHOOK"
```

Use existing `config/` loader convention (see `configuration-management.md` § Composition). Each YAML maps 1:1 to a Pydantic v2 model with `extra="forbid"`.

### 3\. SQL tables

Three command-center-owned tables in `command_center/persistence/tables.py`:

* `alerts(alert_id PK, rule_name, severity, status, fired_at, acknowledged_at, snoozed_until, context_json)` — populated by story 05a.
* `webauthn_credentials(credential_id PK, public_key, sign_count, transports, created_at)` — populated by story 03.
* `operator_sessions(session_id PK, credential_id FK, expires_at, csrf_token_hash, created_at)` — populated by story 03.

Single new Alembic migration creating all three; chained after the PROFILE_SWITCHED migration (story 01a) for linear head ordering.

### 4\. DB access split

`command_center/persistence/session.py` exports two `AsyncSession` factories:

* `cc_writer_session()` — connects to `alphamind.db` with full read/write; the SQLAlchemy `MetaData` registered with this engine contains only the three command-center-owned tables; any attempt to write a foreign table raises at the ORM mapper layer.
* `foreign_reader_session()` — connects to the same DB with `?mode=ro` in the URI; reads all tables (foreign metadata reflected on demand) but writes raise at the SQLite layer.

The split is enforced structurally; tests assert that a writer session can't `session.add(Position(...))` (no mapper exists for it) and a reader session can't `session.add(Alert(...))` (DB-level read-only).

### 5\. `OperatorInvocationHandle` primitive

`command_center/_kernel/operator_invocation.py`:

```python
@asynccontextmanager
async def operator_invocation(
    *,
    invocation_context_writer: InvocationContextWriter,
    operator_session_id: OperatorSessionId,
    verb: ControlVerb,
) -> AsyncIterator[InvocationHandle]:
    """Open a short-lived InvocationHandle with run_type=operator_console for one
    operator action. The yielded handle is the activity_log binding scope; the
    caller writes activity_log entries through it. On exit, the handle is closed
    and the invocations row is finalized.
    """
```

The yielded handle is the standard `InvocationHandle` from `state/invocation_context/context.py`; the only difference from a pipeline invocation is `run_type=operator_console` and the synthetic single-verb scope. The verb name + operator session ID are persisted on the invocation row for traceability.

### 6\. FastAPI app composition (`app.py`)

`app = FastAPI(lifespan=lifespan)`. Lifespan acquires the cc_writer session factory + foreign_reader session factory + supervisor TaskGroup; tears them down on shutdown. Routers from 03, 04a, 04b, 05a, and the view stories are included in this `app` via `app.include_router(...)` calls added by each story — this story ships an empty router list but the include-points are documented in `app.py`.

### Out of scope

* WebAuthn implementation — story 03.
* `/api/control/*` and `/api/events` routes — story 04a and 04b.
* Frontend bundling / static mount — story 04c.
* Alert engine — story 05a.
* `agent_calls`, `validations`, `validation_outcomes`, `retrospective_reports`, `retrospective_decisions`, `weekly_digest_snapshots`, `saved_queries` tables — F-group; deferred to feedback-loop follow-on per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Scope.

## Acceptance criteria

- [ ] `src/alphamind/command_center/{__init__,__main__,app,config,session,supervisor,logging_setup}.py` exist with the documented surfaces.
- [ ] `command_center/_kernel/{ids,control,events,operator_invocation}.py` exist with the named NewTypes, StrEnums, dataclasses, and async context manager.
- [ ] `command_center/persistence/{tables,codecs,session}.py` exist with the three SQLAlchemy tables, row↔dataclass codecs, and dual session factories.
- [ ] `config/command-center.yaml`, `config/security.yaml`, `config/alerts.yaml` exist with the documented fields; `CommandCenterConfig`, `SecurityConfig`, `AlertsConfig` Pydantic models load them with `extra="forbid"`.
- [ ] New Alembic migration creates `alerts`, `webauthn_credentials`, `operator_sessions`; migration test asserts upgrade and downgrade.
- [ ] `cc_writer_session()` cannot write a foreign table (the mapper for the foreign table doesn't exist on this engine).
- [ ] `foreign_reader_session()` cannot write any table (SQLite read-only mode rejects DDL/DML).
- [ ] `operator_invocation()` async context manager opens a row in `invocations` with `run_type=operator_console` and closes it cleanly on exit; the row carries the verb name + operator session ID.
- [ ] `python -m alphamind.command_center` boots the FastAPI app, binds Uvicorn to `127.0.0.1:8080`, responds to a probe (a trivial `/healthz` route is acceptable), and shuts down cleanly on SIGTERM.
- [ ] No story-04+ surface exists in this story (no auth routes, no control proxy, no SSE, no alerts, no views).
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/ tests/persistence/migrations/ -n auto`. Manual smoke: `uv run python -m alphamind.command_center` boots in < 2 s; `curl http://127.0.0.1:8080/healthz` returns `200`; SIGTERM tears down cleanly.