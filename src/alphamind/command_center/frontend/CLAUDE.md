# frontend/ — command center SPA (React + Vite, bun toolchain)

Operator-facing single-page app served by the FastAPI command center. Toolchain is
**bun** (not npm); lockfile is `bun.lock`. Design intent: `docs/design/command-center.md`.

Run lint + format + typecheck + tests after every batch of TS/React changes here:

```bash
bun install                 # idempotent; reflects bun.lock
bun run lint                # ESLint 9 flat config — must exit zero
bun run format:check        # Prettier 3 — must exit zero (`bun run format` to autofix)
bun run typecheck           # tsc -b --noEmit — must exit zero
bun run test                # Vitest run mode — must exit zero
```

Build / dev:

```bash
bun run dev                 # Vite dev server :5173, proxies API/auth/events/healthz to FastAPI :8080
bun run build               # tsc -b && vite build → dist/ (consumed by FastAPI StaticFiles)
bun run generate-types      # openapi-typescript: /openapi.json → src/api/openapi.d.ts
```

Config: `eslint.config.js`, `.prettierrc`. Rules mirror `~/Git/SlipStream-v1/web/`: max-lines
350, max-lines-per-function 50, max-depth 3, max-params 3, complexity 10, no TS enums (use
`as const`), kebab-case filenames. Never disable a rule without alerting the operator.

Dev workflow runs Vite + FastAPI side-by-side; set `COMMAND_CENTER_DEV_MODE=1` on the
FastAPI daemon so its StaticFiles mount is skipped (Vite serves the SPA, proxies to :8080).

## Key invariants

- Types are generated from the live OpenAPI schema — regenerate with `bun run generate-types` after backend contract changes; don't hand-edit `src/api/openapi.d.ts`.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
