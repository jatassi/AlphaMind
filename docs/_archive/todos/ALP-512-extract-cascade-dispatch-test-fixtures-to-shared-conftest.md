## Background

[ALP-509](https://linear.app/alphamind-jatassi/issue/ALP-509/cascade-dispatcher-rejects-every-immediate-breach-per-rule-kwargs) added `tests/execution/continuous_monitor/cascade_dispatch/test_per_rule_kwargs.py`, which duplicates \~130 LOC of test scaffolding already present in `tests/execution/continuous_monitor/cascade_dispatch/test_dispatcher.py`.

## Duplicated symbols

Both files independently define:

* `_StubLibraryOutput`, `_StubLibraryConfig`, `_StubPortfolioState`, `_StubMarketInputs`, `_ScriptedLibrary`
* `_RecordingSubmit`, `_RecordingDeferralSink`
* `_equity_view` / `_equity_position_view` (near-duplicates; the new file adds short-equity handling — `LocateStatus`, `borrow_rate_pct`, `margin_held_usd`)
* `_make_breach_config`
* `_make_phase1_result`
* `_make_breach_loop_result`

## Scope

* Create `tests/execution/continuous_monitor/cascade_dispatch/conftest.py`.
* Move the duplicated symbols there.
* Reconcile the two `_equity_view` builders into one (the richer [ALP-509](https://linear.app/alphamind-jatassi/issue/ALP-509/cascade-dispatcher-rejects-every-immediate-breach-per-rule-kwargs) version is a superset — short-equity defaults compose cleanly).
* Update both test files to import from conftest.
* Keep `_StubRuleProjection` in `test_dispatcher.py` (only used there).

## Acceptance criteria

* `test_dispatcher.py` and `test_per_rule_kwargs.py` no longer duplicate the scaffolding.
* Both test files still pass with `uv run pytest -n auto`.
* No behavior change — `_ScriptedLibrary` keeps the broader `(state, proposals, config, market, delta_buffer_factor)` recording shape from `test_dispatcher.py`.
* Full lint + pytest chain green.

## Reading

* `tests/execution/continuous_monitor/cascade_dispatch/test_dispatcher.py:86-247` — current location of stubs/builders.
* `tests/execution/continuous_monitor/cascade_dispatch/test_per_rule_kwargs.py:90-368` — duplicate copies.