# 06i — command_center views shape-probe merge + build_app collapse

## Goal

Two command_center consolidations: (1) the per-endpoint response-shape probe clusters (the 48-test/752-line shape-probe spread across `views/test_risk.py`, `test_portfolio_dashboard.py`, `test_position_detail.py`) each re-assert one field of a seeded response — merge each endpoint's cluster into a single response-shape assertion; (2) \~26 `build_app()` spin-ups re-boot the whole app to assert one wiring fact — collapse to a few representative boots plus pure resolver units.

## Reading

* `tests/command_center/views/*.py` — the per-field shape-probe clusters.
* `tests/command_center/**` `build_app()` call sites — the app-boot spin-ups + the resolver functions they exercise.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

* **Views:** per endpoint, merge the shape-probe cluster into one test asserting the full response shape (all fields at once). Keep any test asserting a distinct computed value or error path.
* **build_app:** collapse the \~26 full-boot tests to a few representative boots; extract the pure wiring/resolver assertions into unit tests that call the resolver directly instead of booting the app.

**Safety guard:** deletions/merges must not drop coverage of `src/alphamind/command_center/views/` or the app-wiring source. Keep any view test that exercises a branch the merged shape-assertion would not (empty state, error response, auth gate).

## Acceptance criteria

- [ ] Each view endpoint has one consolidated response-shape test (plus retained branch/error tests); the per-field probes are gone.
- [ ] `build_app()` boots collapsed to a few representatives; pure wiring facts asserted via resolver units.
- [ ] `coverage report` for `src/alphamind/command_center/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/command_center/views tests/command_center -p no:xdist` green (scope to the touched dirs); `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; lint clean.