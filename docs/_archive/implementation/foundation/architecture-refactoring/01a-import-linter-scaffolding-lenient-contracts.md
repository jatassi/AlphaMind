# 01a — Import-linter scaffolding (lenient contracts)

## Goal

Install `import-linter` as a dev dependency, write the initial set of layer contracts that already pass against the current codebase, and wire `lint-imports` into the CI / lint pipeline alongside `ruff` and `mypy`. The contracts established here are intentionally lenient — they encode the layering that is already true and leave the cycle-breaking contracts (which require the cycle fixes in stories 02a/02b/03 to land first) for story 03. Without this scaffolding, story 03's tightening has nothing to extend.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L1 — describes the absent linter as the gating finding
* `pyproject.toml` § `[tool.ruff]`, `[tool.mypy]`, `[dependency-groups.dev]` — existing static-analysis posture this story extends
* `.claude/skills/python-architecture/references/foundations.md` § A2 — "the import graph IS the architecture" thesis; rationale for enforcing
* `.claude/skills/python-architecture/scripts/analyze_imports.py` — script that computes the current import graph; story 03 uses it to verify cycle elimination, and this story should ensure it still runs after the linter is added

## Depends on

* (none — this is a foundation gate that parallels 01b)

## Scope

In scope, all under `pyproject.toml` and a new `.importlinter` configuration file (or `[tool.importlinter]` section in `pyproject.toml` — pick whichever convention is cleaner for this project). Tests at `tests/test_import_linter.py` or via CI workflow only.

### 1\. Add `import-linter` to dev dependencies

Add `import-linter>=2.0` to `[dependency-groups.dev]` in `pyproject.toml`. Run `uv sync` to update `uv.lock`. Verify the installed CLI works via `uv run lint-imports --help`.

### 2\. Write initial layer contracts that pass today

Author the contracts as a TOML section (either `.importlinter` or in `pyproject.toml`). The contracts express the layering that the current codebase already respects:

* **Layered contract** declaring `alphamind` package layers in order (closest to boundary first → core domain): `[alphamind.config, alphamind.persistence, alphamind.data_sources]` (boundaries) → `[alphamind.distillation]` (numerical core) → `[alphamind.analysis, alphamind.decision]` (LLM layers) → `[alphamind.execution]` (egress). Layers may import from layers below; not from layers above.
* **Forbidden contract** declaring `alphamind.distillation.* → sqlalchemy` is forbidden (distillation should be pure compute — this fails today per audit finding L6 but the linter can be configured with `ignore_imports` for known violations and tightened later).

Use `ignore_imports` to record current violations as deliberate exceptions; the tightening in story 03 (after cycles break) removes them. Document each `ignore_imports` entry with a comment naming the punch-list item that will eliminate it.

### 3\. Wire into CI / pre-commit

Add a CI step (in the existing workflow file or pre-commit config) that runs `uv run lint-imports` after `ruff` and `mypy`. The step must fail the build when any contract fails.

### 4\. Document in `CLAUDE.md`

Add a short section to `CLAUDE.md` under "Linting" naming `lint-imports` as a required check, and noting that contracts will tighten as cycle fixes land (story 03 specifically).

### Out of scope

Tightening the contracts to forbid the two known cycles (`decision.* ↛ execution.*`, `portfolio_state.* ↛ risk_guardrails.*`) — story 03 owns that once stories 02a and 02b have eliminated the cycles. Importing the `lint-imports` invocation into pre-commit hooks if pre-commit isn't already in use (just CI is enough for this story).

## Acceptance criteria

- [ ] `import-linter` appears in `[dependency-groups.dev]` of `pyproject.toml` and `uv.lock` reflects the install.
- [ ] An import-linter configuration exists (either `.importlinter` file or `[tool.importlinter]` in `pyproject.toml`) with at least one `Layered` contract covering the `alphamind` package layers.
- [ ] `uv run lint-imports` exits 0 against the current `main` branch.
- [ ] CI runs `lint-imports` and fails the build if any contract fails.
- [ ] `CLAUDE.md` documents `lint-imports` as part of the lint suite alongside `ruff` and `mypy`.
- [ ] Every `ignore_imports` entry carries a comment naming the punch-list item that will remove it.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

Running `uv run lint-imports` locally returns "Contracts: N kept, 0 broken". CI build passes on a PR that touches `pyproject.toml`. Manually injecting a synthetic violation (e.g., temporarily adding `import alphamind.execution` to `alphamind/config/__init__.py`) and re-running `lint-imports` returns a non-zero exit code with a clear failure message.