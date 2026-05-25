## Symptom

After [ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider) lands, the continuous monitor's cascade dispatcher has a populated `BreachDispatchContext` (open positions, IV provider, ADV / R/R placeholders all wired). But the dispatcher still rejects every immediate breach at construction time because `per_rule_kwargs_providers={}` is passed empty in `__main__.py`.

`src/alphamind/execution/continuous_monitor/cascade_dispatch/dispatcher.py:234`:

```python
if rule.rule_id not in self._per_rule_kwargs_providers:
    msg = (
        f"no per-rule kwargs provider registered for rule_id "
        f"{rule.rule_id!r}; the dispatcher cannot construct the "
        f"selector's arguments"
    )
    raise ValueError(msg)
```

Every breach the dispatcher receives raises `ValueError` before any selector runs.

## Root cause

`src/alphamind/execution/continuous_monitor/__main__.py:436` constructs the dispatcher with the literal empty map:

```python
dispatcher = CascadeDispatcher(
    monitor_session_id=session.session_id,
    breach_config=breach_behavior_config,
    trigger_ids=trigger_ids,
    context_provider=dispatch_context_provider,
    submit_envelope=submit_envelope,
    deferral_sink=_log_deferral,
    per_rule_kwargs_providers={},  # ← empty
)
```

`PerRuleKwargsProvider = Callable[[RuleEvaluation, BreachDispatchContext], dict[str, Any]]` — one per rule_id whose `BreachResponse` is `immediate_engine`. Each provider builds the kwargs the rule's `selector_for(rule_id)` requires (positions, liquidity, R/R, current state, library config, etc.).

[ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers)'s substrate docstring explicitly defers this: "the per-rule kwargs providers are out of scope for [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers) — the dispatcher will not fire without that map populated."

## Scope

For each rule_id whose `BreachResponse` is `immediate_engine` (loaded from `config/guardrails.yaml`), build a `PerRuleKwargsProvider` that extracts the selector's argument set from `BreachDispatchContext` and `RuleEvaluation`. Wire the map at the dispatcher's construction site in `__main__.py`. Candidate rules per `guardrails.yaml` include `per_position_max_loss`, `daily_drawdown`, `cumulative_drawdown`, `total_short_exposure`, `single_short_max_size`, and `margin_call`.

Reading `tests/execution/continuous_monitor/cascade_dispatch/test_dispatcher.py:356-380` for the shape — `_per_position_max_loss_kwargs_provider` is a worked example.

## Acceptance criteria

* For every `immediate_engine` rule in `GuardrailsConfig.rules`, a `PerRuleKwargsProvider` is registered on `CascadeDispatcher`.
* Each provider satisfies its rule's selector signature (selectors live in `src/alphamind/risk_guardrails/breach_behavior/position_selection.py`).
* A new integration test constructs the dispatcher with a synthetic context containing positions, fires one immediate breach per registered rule, and asserts no `ValueError("no per-rule kwargs provider…")` is raised.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Surfaced during [ALP-507](https://linear.app/alphamind-jatassi/issue/ALP-507/continuous-monitor-cascade-dispatcher-ships-with-empty-iv-provider)'s code review (PR #69).
* Originally deferred by [ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers).
* Sibling to [ALP-508](https://linear.app/alphamind-jatassi/issue/ALP-508/wire-live-adv-risk-reward-signals-into-cascade-dispatchers) (live ADV / R/R signals) — both are gating the dispatcher's readiness for live trading.

## Reading

* `src/alphamind/execution/continuous_monitor/cascade_dispatch/dispatcher.py` — dispatcher and `PerRuleKwargsProvider` type.
* `src/alphamind/execution/continuous_monitor/__main__.py:436` — current empty wiring.
* `src/alphamind/risk_guardrails/breach_behavior/position_selection.py` — selector signatures.
* `tests/execution/continuous_monitor/cascade_dispatch/test_dispatcher.py:356-380` — worked example.