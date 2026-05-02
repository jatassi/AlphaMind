---
status: done
completed_date: 2026-04-27
commit_id: 1314dfb
---

# 02 — Convert `config/models.py` to a package

## Goal

Convert `src/alphamind/config/models.py` (currently a single file holding the three collector models) into a package `src/alphamind/config/models/`, with one module per YAML config file. Re-export from `models/__init__.py` so existing imports (`from alphamind.config.models import DataSourcesConfig`) keep working unchanged.

This is a refactor with zero behavioral change. It exists to give every subsequent configuration-management story a clean target file to land its Pydantic model in, eliminating merge-conflict friction when many parallel stories add models to the same file.

## Reading

- `src/alphamind/config/models.py` — current state (three collector models, internal helpers, `.env.example` cache)
- `src/alphamind/config/__init__.py` — current re-exports (verify whether `models` is referenced)
- `src/alphamind/data_sources/_common.py` — primary consumer of the models (`load_config()` imports them)
- `tests/test_imports.py` and any tests under `tests/` that import from `alphamind.config.models`
- `docs/design/configuration-management.md` § File layout — names every YAML file the package will eventually carry a module for

## Depends on

- 01 (link from design README — the orchestrator dispatch sequencing dependency, not a code dependency)

## Scope

In scope:
- New directory `src/alphamind/config/models/` with `__init__.py`.
- One module per existing model group:
  - `models/news_outlets.py` — `CredibilityTier`, `OutletEntry`, `NewsOutletsConfig`
  - `models/collector_schedule.py` — `CollectorEntry`, `CollectorScheduleConfig`, `_validate_cron`, `_CRON_FIELD_RE`
  - `models/data_sources.py` — `CriticalityTier`, `BackoffStrategy`, `RetryShapeConfig`, `ProviderConfig`, `CategoryConfig`, `DataSourcesConfig`, plus the `.env.example` helpers (`_ENV_EXAMPLE_PATH`, `_load_env_example_keys`, `_ENV_EXAMPLE_KEYS`) since they are only consumed by `DataSourcesConfig`'s validator.
- `models/__init__.py` re-exports every public symbol that was importable from the old `models.py`. The set is exactly: `CredibilityTier`, `OutletEntry`, `NewsOutletsConfig`, `CollectorEntry`, `CollectorScheduleConfig`, `CriticalityTier`, `BackoffStrategy`, `RetryShapeConfig`, `ProviderConfig`, `CategoryConfig`, `DataSourcesConfig`.
- Delete the old `src/alphamind/config/models.py` (the package replaces it).
- Update `pyproject.toml` build target if it pinned the file (it shouldn't — the existing `[tool.setuptools.packages.find]` or equivalent picks up the package automatically, but verify).

Out of scope:
- Adding any new model — that is the work of stories 03a–03i.
- Changing the public API of any model (field names, validators, exception messages).
- Adding new tests — existing tests must continue to pass without modification.
- Touching `_common.py` — the import path does not change.

## Notes

Pydantic v2 does not require any package-vs-module-specific configuration; `BaseModel` subclasses move freely between modules.

The `_ENV_EXAMPLE_*` helpers are private and are cached at import time. Move them to `models/data_sources.py` (their only consumer) rather than to `models/__init__.py`. The cache is per-module-import, not global; a single import site keeps the behavior identical to today.

Internal helpers (`_validate_cron`, `_CRON_FIELD_RE`, `_load_env_example_keys`, `_ENV_EXAMPLE_PATH`, `_ENV_EXAMPLE_KEYS`) are leading-underscore — do not re-export them. They are used internally by their containing module's validators.

Verify the conversion by running `uv run pytest` against the existing test suite without modifying any test file. Every existing import must resolve through the package re-export.

If `pyproject.toml` uses a build backend that requires explicit listing of packages (it should not — `find` semantics auto-discover), update the configuration to include the new `models` subpackage. Confirm by running `uv sync` after edits.

The `__init__.py` re-export list is the *contract* this story preserves. Future stories add new public symbols by editing that file's re-export list, not by changing existing ones.

## Acceptance criteria

- [ ] `src/alphamind/config/models/` exists as a package with `__init__.py`.
- [ ] `src/alphamind/config/models.py` no longer exists.
- [ ] `models/news_outlets.py`, `models/collector_schedule.py`, `models/data_sources.py` each exist and contain exactly the model groups described in Scope.
- [ ] `models/__init__.py` re-exports the eleven public names listed in Scope.
- [ ] `from alphamind.config.models import DataSourcesConfig` (and the other ten public names) resolve at runtime — verified by a smoke import test.
- [ ] No test file is modified by this story.
- [ ] `uv run pytest` passes against the existing test suite.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` all pass.
- [ ] `uv sync` completes without errors.
