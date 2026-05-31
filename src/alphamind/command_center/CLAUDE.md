# command_center/ — operator webapp (FastAPI + React SPA)

Operational tooling. Operator-facing monitoring, alerting, config editing, manual
control, and auth. FastAPI backend (`views/`, `control/`, `events/`, `auth/`, `alerts/`,
`persistence/`) serving the `frontend/` SPA. Design intent (historical):
`docs/design/command-center.md`.

The `frontend/` subpackage has its own `CLAUDE.md` (bun toolchain — read it before
touching TS/React).

## Key invariants

- The SPA is served by FastAPI StaticFiles in prod; in dev, set `COMMAND_CENTER_DEV_MODE=1` so the mount is skipped and Vite serves it.
- Backend API is the typed contract the frontend codegens against — keep `/openapi.json` honest.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
