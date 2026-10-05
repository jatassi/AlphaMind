# 04c — Frontend lint + formatter setup (mirror SlipStream)

## Goal

Land the TypeScript / React lint + formatter configuration **before any TypeScript source file is written** in this work tree, so the frontend foundation story (04d) and every subsequent view story is constrained by the rules from day one. Mirror the canonical setup from the operator's `~/Developer/SlipStream-v1/web/` project verbatim — same **bun** runtime + toolchain, same ESLint 9 flat config, same Prettier 3 config, same dev-dep floor versions, same `lint` / `lint:fix` / `format` / `format:check` scripts. Update `CLAUDE.md` with a frontend-linting section mirroring the existing Python-linting block so subsequent agents know to run the linter after every batch of TS changes.

## Reading

* `~/Developer/SlipStream-v1/web/package.json` — canonical dev-dep list + scripts to copy.
* `~/Developer/SlipStream-v1/web/bun.lock` — canonical lockfile shape (bun, not npm).
* `~/Developer/SlipStream-v1/web/eslint.config.js` — full ESLint flat config (typescript-eslint strict + react + react-hooks + react-refresh + jsx-a11y + unicorn + simple-import-sort + import-x + prettier-config) with the SlipStream rule customizations (max-lines 350, max-lines-per-function 50, max-depth 3, max-params 3, complexity 10, banned TS enums, kebab-case filename enforcement, etc.).
* `~/Developer/SlipStream-v1/web/.prettierrc` — Prettier 3 config with `prettier-plugin-tailwindcss`.
* `~/Developer/SlipStream-v1/web/.prettierignore` — `dist`.
* `~/Developer/SlipStream-v1/web/tsconfig.app.json` — referenced by `eslint.config.js`'s `import-x/resolver` settings; copy structure but adapt paths for this project.
* `CLAUDE.md` § Linting (project root) — existing Python linting block this story mirrors for the frontend.

## Depends on

* (none — this is a leaf substrate story; parallel with everything in Waves 1-3; must complete before 04d Frontend foundation)

## Scope

In scope, under `src/alphamind/command_center/frontend/`. Also `CLAUDE.md` (project root). Tests: `bun run lint` exits zero against the empty `src/` (the foundation in 04d hasn't written code yet); CI job runs `bun install --frozen-lockfile && bun run lint && bun run format:check`.

### 1\. Toolchain: bun

The frontend toolchain is **bun** (not npm or yarn or pnpm), mirroring SlipStream. Bun handles install, script running, and resolution. Commands:

* `bun install` — install + generate / update `bun.lock`.
* `bun install --frozen-lockfile` — CI-mode install; fails if lockfile drift.
* `bun run <script>` — run a `package.json` script (e.g., `bun run lint`).
* `bunx <bin>` — one-shot run of an installed binary (e.g., `bunx shadcn add ...`).

`bun.lock` is the canonical lockfile committed in-tree. No `package-lock.json`, no `yarn.lock`, no `pnpm-lock.yaml`. The operator's dev machine and the trading machine both need bun installed (operator handover concern — note in RUNBOOK in story 07).

### 2\. Minimal `package.json`

Create `src/alphamind/command_center/frontend/package.json` containing:

* `"name": "alphamind-command-center-web"`, `"type": "module"`, `"private": true`.
* `scripts`: `dev`, `build`, `preview`, `lint`, `lint:fix`, `format`, `format:check`. The `dev` / `build` / `preview` scripts call `vite` placeholders that will work once 04d adds the source tree. (Bun runs scripts via `bun run <name>`; no Node-vs-Bun script syntax differences.)
* `dependencies`: empty for now (04d adds them); declare it as an empty object.
* `devDependencies`: copy verbatim from SlipStream's `package.json`:

  ```
  @eslint/js, @types/node, @types/react, @types/react-dom, @vitejs/plugin-react,
  eslint, eslint-config-prettier, eslint-import-resolver-typescript, eslint-plugin-import-x,
  eslint-plugin-jsx-a11y, eslint-plugin-react, eslint-plugin-react-hooks,
  eslint-plugin-react-refresh, eslint-plugin-simple-import-sort, eslint-plugin-unicorn,
  globals, prettier, prettier-plugin-tailwindcss, typescript, typescript-eslint, vite
  ```

  Pin floor versions to whatever SlipStream's current `package.json` records at copy time (no upper bounds per [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) § Pre-resolved (N) bundling discipline). Generate `bun.lock` via `bun install`.

### 3\. ESLint flat config

Copy `~/Developer/SlipStream-v1/web/eslint.config.js` to `src/alphamind/command_center/frontend/eslint.config.js` **verbatim** — same imports, same `defineConfig([globalIgnores(['dist']), ...])`, same `extends`, same `rules` block (the SlipStream rule customizations are intentional and the operator wants identical enforcement across both projects).

Only allowed deviation: the `import-x/resolver` `project` path — if 04d's `tsconfig.app.json` lives at a different location relative to `eslint.config.js` than SlipStream's does, adjust the path string to point at the correct location.

### 4\. Prettier config

Copy `~/Developer/SlipStream-v1/web/.prettierrc` and `~/Developer/SlipStream-v1/web/.prettierignore` verbatim to `src/alphamind/command_center/frontend/.prettierrc` and `.prettierignore`. No deviations.

### 5\. Minimal `tsconfig.app.json`

Create `src/alphamind/command_center/frontend/tsconfig.app.json` with the minimum needed for the ESLint config's `typescript-eslint` `projectService` to load: `compilerOptions` with `target`, `module`, `moduleResolution`, `strict`, `jsx`; `include: ["src"]`. Story 04d will extend this with the real compiler settings. A stub `tsconfig.json` referencing `tsconfig.app.json` (no source files yet) keeps `tsc -b` clean.

### 6\. CLAUDE.md update

Append a new subsection to the `## Linting` section of `CLAUDE.md`:

```markdown
### Frontend (command center)

Frontend toolchain is **bun** (not npm). Lockfile is `bun.lock`. Run the frontend linter
+ formatter after every batch of TypeScript / React changes in
`src/alphamind/command_center/frontend/`:

`​`​`bash
cd src/alphamind/command_center/frontend
bun install                 # idempotent; ensures node_modules reflects bun.lock
bun run lint                # ESLint 9 flat config — must exit zero
bun run format:check        # Prettier 3 — must exit zero (use `bun run format` to autofix)
`​`​`

Config files are `eslint.config.js` and `.prettierrc`, mirrored verbatim from `~/Developer/SlipStream-v1/web/`.
Same rule customizations apply (max-lines 350, max-lines-per-function 50, max-depth 3, max-params 3,
complexity 10, banned TS enums in favor of `as const` objects, kebab-case filenames). The alert
discipline from the Python side carries over: never disable a rule (via `// eslint-disable-*` or
`eslintrc` overrides) without alerting the operator first.
```

Place the new subsection directly after the existing `lint-imports` paragraph in `CLAUDE.md`.

### 7\. CI job

Add a new GitHub Actions job under `.github/workflows/ci.yml` (or a new file under the same dir if the existing workflow's `paths-ignore` doesn't fit): runs `bun install --frozen-lockfile && bun run lint && bun run format:check` against the frontend on every PR + push to main. The job uses `oven-sh/setup-bun@v2` (or equivalent current action) to install bun on the runner. Triggers only when files under `src/alphamind/command_center/frontend/**` change (use `paths` filter). Mirror the existing CI conventions in `.github/workflows/ci.yml`.

### Out of scope

* Any TypeScript source code — 04d (Frontend foundation) writes the first `.ts` / `.tsx` files.
* Vite / Tailwind / shadcn/ui config — 04d.
* `vitest` and `@testing-library/*` dev-deps — 04d brings these in alongside its first test.
* Switching the Python side from `uv` to anything else — out of scope; this story only addresses the new frontend subtree.

## Acceptance criteria

- [ ] `src/alphamind/command_center/frontend/package.json` exists with the exact devDependencies list from `~/Developer/SlipStream-v1/web/package.json` and the four lint / format scripts (`lint`, `lint:fix`, `format`, `format:check`).
- [ ] `src/alphamind/command_center/frontend/bun.lock` exists (generated by `bun install`); no `package-lock.json` is committed.
- [ ] `src/alphamind/command_center/frontend/eslint.config.js` is byte-identical to `~/Developer/SlipStream-v1/web/eslint.config.js` modulo the `import-x/resolver` `project` path field.
- [ ] `src/alphamind/command_center/frontend/.prettierrc` and `.prettierignore` are byte-identical to SlipStream's copies.
- [ ] `src/alphamind/command_center/frontend/tsconfig.app.json` + `tsconfig.json` exist as stubs sufficient for `bun run lint` to load without error.
- [ ] `cd src/alphamind/command_center/frontend && bun install --frozen-lockfile && bun run lint && bun run format:check` exits zero against an empty `src/` (or a `src/` containing only a `.gitkeep`).
- [ ] `CLAUDE.md` has a new `### Frontend (command center)` subsection under `## Linting` mirroring the structure documented in scope item 6 — and explicitly calls out bun as the runtime + toolchain.
- [ ] CI workflow uses `oven-sh/setup-bun@v2` to install bun and runs the frontend lint + format-check on PRs that touch `src/alphamind/command_center/frontend/**`.
- [ ] No TypeScript source files are added in this story (`find src/alphamind/command_center/frontend/src -name '*.ts' -o -name '*.tsx'` returns empty or only a `.gitkeep`).
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass (no Python changes expected; verify clean).

## Verification

`cd src/alphamind/command_center/frontend && bun install --frozen-lockfile && bun run lint && bun run format:check` exits zero on a fresh checkout. CI's new frontend job runs green on the PR landing this story. CLAUDE.md diff confirms the new subsection landed correctly and names bun as the toolchain.