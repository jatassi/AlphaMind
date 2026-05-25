`_resolve_path` in `src/alphamind/persistence/session.py:50` calls `os.path.expandvars(db_path)` to expand `%USERPROFILE%` in `main.yaml`'s `paths.database`. Works on Windows (`%VAR%` syntax) but not on POSIX (only expands `$VAR`/`${VAR}`).

**Side-effect:** `tests/test_persistence.py::TestResolvePath::test_yaml_path_expands_environment_variables` fails on macOS because `%ALPHAMIND_TEST_ROOT%` survives the call unchanged.

**Fix:** Replace `os.path.expandvars` with a manual `re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: os.environ.get(m.group(1), m.group(0)), db_path)` so `%VAR%` syntax works on both OSes.

Production-side fix at `41e0ffa` solved the Windows boot-time crash but did not survive cross-platform testing.