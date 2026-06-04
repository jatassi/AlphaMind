# 06e — Parametrize the activity-log exhaustiveness classes (preserve all cases)

## Goal

`tests/portfolio_state/records/test_activity_log.py` has two \~35-test classes whose per-test assertion is weak (`e.event_type == X`), but they are **not** tautologies: each constructs one `(event_type, event_group, detail)` triple and so exercises the `ActivityLogEntry.__post_init__` consistency-validator's **pass-branch** for that triple. The verifier confirmed the "covered elsewhere" claim is **false** (the mismatch tests prove only that one wrong combo raises; the mapping-exhaustiveness tests never instantiate the detail dataclasses). Convert both classes to parametrized tests that drive **all** cases through the constructor — preserving every case, not collapsing to representatives.

## Reading

* `tests/portfolio_state/records/test_activity_log.py` — `TestActivityLogEntryHappyPath` (L899–1307, 35 tests), `TestDetailClassHappyPaths` (L339–673, \~35 tests), and the two mismatch tests (L1318–1370).
* `src/alphamind/portfolio_state/records/` — `ActivityLogEntry.__post_init__` validator + the detail dataclasses.
* Parent `ALP-783`.

## Depends on

* none.

## Scope — `test_activity_log.py` only

* `TestActivityLogEntryHappyPath`: replace the 35 near-identical methods with ONE parametrized test over all 35 `(event_type, event_group, detail-factory)` triples, constructed through `ActivityLogEntry(...)` (so the validator pass-branch is exercised per triple). All 35 triples remain as parametrize rows.
* `TestDetailClassHappyPaths`: parametrize via a detail-factory table driving **every** detail dataclass through construction (most have no other constructor-exercise — they are sole-covered here). **KEEP** `test_command_abandoned_detail_requires_command_type` (L619) and the `command_type` encode/decode roundtrip parametrize (L630–646) as distinct tests.
* Leave the two mismatch tests and `TestMappingExhaustiveness` as-is.

## Acceptance criteria

- [ ] Both happy-path classes are parametrized; **all 35 + \~35 cases remain** as rows, each constructing its real detail instance through `ActivityLogEntry`.
- [ ] `test_command_abandoned_detail_requires_command_type` and the `command_type` roundtrip are retained.
- [ ] `coverage report` for `src/alphamind/portfolio_state/records/` shows no newly-missing lines vs. before (proves no per-type construction coverage was dropped).
- [ ] `uv run pytest tests/portfolio_state/records/test_activity_log.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; collected test count for the two classes still reflects all cases (parametrize rows, not a handful of representatives); coverage diff no regression.