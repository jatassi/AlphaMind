# 07d — state table/migration column-set de-duplication

## Goal

For each of the \~12 table/migration pairs in `tests/state/`, both the table test and the migration test assert the identical column set. Drop the duplicated column-set assertion on the **table** side, keeping it on the **migration** side — production is alembic-managed, so the migration is the authoritative column-set contract. \~280 LOC. Keep all behavior/codec/upgrade-downgrade tests.

## Reading

* `tests/state/` — the per-table tests (`test_*_table.py`) and the migration tests.
* `src/alphamind/state/tables/` and `src/alphamind/persistence/migrations/versions/` — the two declaration sites.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

* For each table/migration pair where BOTH assert the same exact column set, delete the column-set assertion on the **table-test** side; keep the migration-test's column-set assertion (prod runs alembic).
* Keep everything else: the table tests' codec/round-trip/behavior assertions, the migration tests' upgrade/downgrade/CHECK-constraint assertions.

**Safety guard:** drop a table-side column-set assertion only when the migration-side asserts the identical set. Coverage of `src/alphamind/state/` and the migrations must not be reduced.

## Acceptance criteria

- [ ] No table/migration pair asserts the column set on both sides; the migration side is the one retained.
- [ ] All codec/behavior/upgrade-downgrade tests still present and green.
- [ ] `coverage report` for `src/alphamind/state/` and `src/alphamind/persistence/migrations/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/state tests/persistence -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; each migration test still asserts its column set.