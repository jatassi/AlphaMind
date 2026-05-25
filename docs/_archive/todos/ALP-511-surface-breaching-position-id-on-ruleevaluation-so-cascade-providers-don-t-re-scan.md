## Background

[ALP-509](https://linear.app/alphamind-jatassi/issue/ALP-509/cascade-dispatcher-rejects-every-immediate-breach-per-rule-kwargs) wired the cascade dispatcher's `per_rule_kwargs_providers` map. For per-position rules — `position_max_loss_equity_pct`, `position_max_loss_options_pct`, `single_short_max_pct` — the selector signature needs a `breaching_position_id`, but `RuleEvaluation` (in `src/alphamind/execution/continuous_monitor/breach_loop/result.py`) doesn't carry one. The providers in `src/alphamind/execution/continuous_monitor/cascade_dispatch/per_rule_kwargs.py` work around this by re-scanning `context.open_positions` and selecting the worst in-class loser (or the largest short over the cap).

## Problem

When the breach loop's library projection identifies a specific position as the breaching subject (e.g., position A is at -3.5% past the -3% cap), the dispatcher may close a different position than the one whose breach triggered the rule — if there's a same-instrument-type peer with a deeper loss. Today this is dormant (the drawdown / per-position-max-loss rules aren't yet emitted by the library), but it ships as a lossy provider as soon as those rules are wired.

## Scope

* Add a `breaching_position_id: str | None` field to `RuleEvaluation`.
* When the library's rule registry adds per-position rules (`position_max_loss_*_pct`), have `_build_rule_evaluation` populate `breaching_position_id` from the projection. The library projection will need a new field for this too.
* Update `per_rule_kwargs.py` providers for `position_max_loss_equity_pct`, `position_max_loss_options_pct`, and `single_short_max_pct` to read `rule.breaching_position_id` directly and pass it through, replacing `_select_worst_loss_position` / `_select_largest_short_over_cap`.
* Update unit tests in `tests/execution/continuous_monitor/cascade_dispatch/test_per_rule_kwargs.py` to set `breaching_position_id` on the `RuleEvaluation` fixtures and remove the fall-back-scan tests.

## Acceptance criteria

* `RuleEvaluation.breaching_position_id` is `str | None`.
* Per-position providers in `per_rule_kwargs.py` read directly from the rule instead of scanning open positions.
* `_select_worst_loss_position` and `_select_largest_short_over_cap` are removed.
* Existing integration test `test_dispatcher_with_built_providers_handles_one_breach_per_registered_rule` still pins the submitted envelope's position_id per scenario.
* Full lint + pytest chain green.

## Reading

* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` — `RuleEvaluation`.
* `src/alphamind/execution/continuous_monitor/breach_loop/task.py:301-352` — `_build_rule_evaluation`.
* `src/alphamind/execution/continuous_monitor/cascade_dispatch/per_rule_kwargs.py` — current re-scanning providers.
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `RuleProjection` (the library's per-rule output).