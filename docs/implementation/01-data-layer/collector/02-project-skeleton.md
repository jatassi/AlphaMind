---
status: done
completed_date: 2026-04-26
commit_id: 95a90fc
---

# 02 — Project skeleton & package structure

## Goal

Stand up the Python package layout for the data-layer collector under `src/alphamind/`, matching the module structure described in the design docs. Empty packages with import-only smoke test.

## Reading

- `docs/design/01-data-layer/collector/data-sources.md` — module layout
- `docs/design/01-data-layer/collector/runner.md` — collector module location
- `pyproject.toml` — declares dependencies and `testpaths = ["tests"]`

## Depends on

None.

## Scope

In scope:
- The directory tree under `src/alphamind/`:
  - `config/`
  - `data_sources/` with one subdirectory per vendor: `polygon/`, `fred/`, `eia/`, `bls/`, `treasury/`, `finnhub/`, `marketaux/`, `sec_edgar/`, `polymarket/`, `kalshi/`, `alpaca/`
  - `persistence/` with a `migrations/` subdirectory
  - `collector/`
- An empty `__init__.py` in every directory above (the top-level package and every subpackage).
- `tests/` directory at the repo root with `__init__.py`.
- A smoke test at `tests/test_imports.py` that imports `alphamind`, `alphamind.config`, `alphamind.data_sources`, `alphamind.persistence`, and `alphamind.collector`, asserting each import succeeds.
- `pyproject.toml` updates only if needed to make the `src/` layout importable (e.g., `[tool.setuptools.packages.find].where = ["src"]` or `[tool.hatch.build.targets.wheel].packages = ["src/alphamind"]` depending on the build backend).

Out of scope:
- Any module-level code beyond `__init__.py`.
- Configuration files (story 03a).
- SQLAlchemy models or Alembic setup (story 03b).
- `_common.py` content (story 04).
- Vendor-specific code (stories 05*).

## Notes

The existing `pyproject.toml` already declares Python 3.13 and the third-party deps (`sqlalchemy`, `alembic`, `apscheduler`, `httpx`, `polygon-api-client`, etc.). Verify with `uv sync` after edits.

Use the `src/` layout (Python's preferred convention for distributable packages) — the import root is `src/alphamind/`. If the build backend isn't already configured for it, add the appropriate config block.

## Acceptance criteria

- [ ] `src/alphamind/__init__.py` exists.
- [ ] `src/alphamind/config/__init__.py` exists.
- [ ] `src/alphamind/data_sources/__init__.py` exists, plus `__init__.py` in each vendor subdirectory listed in scope.
- [ ] `src/alphamind/persistence/__init__.py` exists; `src/alphamind/persistence/migrations/` exists as a directory.
- [ ] `src/alphamind/collector/__init__.py` exists.
- [ ] `tests/__init__.py` exists.
- [ ] `tests/test_imports.py` imports each top-level package and passes.
- [ ] `uv sync` completes without errors.
- [ ] `uv run pytest` collects and runs the smoke test successfully.
