`_resolve_path` in `src/alphamind/persistence/session.py:34` falls through silently to `_default_db_path()` when no explicit path, no `DATABASE_PATH` env var, and `config/main.yaml` is absent/unresolved. SQLAlchemy then creates an empty SQLite file at the default path; downstream callers see "all tables MISSING" instead of "no DB configured."

**Reproduced:** 2026-05-03 `verify_bootstrap.py` invocation caused fall-through to `~/AlphaMind/data/alphamind.db` (empty, 9.4 MB), producing a misleading 17-table-missing FAIL.

**Fix options:**

**(A) Opt-in env var.** Add `ALPHAMIND_REQUIRE_EXPLICIT_DB_PATH` (default off for backwards compat) that raises `RuntimeError("no database path configured")` when the resolution chain falls through.

**(B) Simpler.** Have `make_engine` log a WARNING when the default path is used so the operator sees it.

**Sister item:** "Cross-platform %VAR% expansion in YAML-supplied DB path" addresses the proximate cause on macOS within the same resolution chain.