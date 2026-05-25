## Symptom

`python -m alphamind.scheduler run --debug-e2e` (canonical e2e gate per `scripts/RUNBOOK_end_to_end_verification.md`) crashes at the analyst with:

```
ValueError: risk_budget missing required entry for rule_id 'sector_concentration_energy'
  at src/alphamind/risk_guardrails/state_delivery/primitives.py:357
  via render_analyst_header → assemble_input_bundle_normal (analyst input bundle)
```

Reproduced 2026-05-17 via `scripts/verify_debug_e2e.py --archive-root .archive/verify-debug-e2e` — invocation `inv-20260517T072650Z-fafa3088`. Analysis pipeline (distillation + 3 sector researchers + qualitative + adaptive + synthesizer) completes cleanly; the decision pipeline dies on the very first sector lookup at the analyst.

## Root cause

The production `risk_budget` is never populated. Three load-bearing facts:

1. `src/alphamind/state/repository/sql_repository.py:540-543` — `SqlRepository.get_risk_budget_consumption()` returns `RiskBudgetConsumption(entries=())`, with the inline comment *"Zero-valued passthrough; the assembler computes actual consumption by combining position + cash + active_risk_parameters in story 07."*
2. `src/alphamind/portfolio_state/assembler.py:458` — the assembler reads the empty stub and passes it straight through to the snapshot at line 672. There is **no** computation step. `portfolio_state/computations/risk_budget.py` only carries helpers for `cash_pct`, `order_age_hours`, `parameter_change_flag`, and `true_deployable_capital_usd` — nothing that builds entries.
3. `src/alphamind/pipeline/decision.py:328` hands `pydantic_snapshot.risk_budget` (empty) to `run_analyst`. `resolve_sector_entries` (`src/alphamind/risk_guardrails/state_delivery/primitives.py:346-359`) iterates `active_sectors` and raises `ValueError` on the first sector for which `risk_budget.entry_by_rule_id("sector_concentration_<sector>")` is `None` — which is *every* sector when the budget is empty.

The only code path in `src/` that actually constructs `RiskBudgetEntry` records is `execution/continuous_monitor/emergency_trigger/evaluator.py:163` — the breach-loop emergency-trigger projector. The main scheduler pipeline has no equivalent. The per-feature verify scripts retired by [ALP-502](https://linear.app/alphamind-jatassi/issue/ALP-502/05-end-to-end-verification-runbook) all stubbed `risk_budget` via fixtures, masking this gap; the consolidated debug-e2e gate is the first end-to-end exercise of the analyst path.

Per the comment, "story 07" was supposed to land this in the assembler. The four `consumers/` modules (`synthesizer.py`, `analyst.py`, `strategist.py`, `portfolio_manager.py`) all carry `(story 07)` headers, and the parent strategist Issue ([ALP-308](https://linear.app/alphamind-jatassi/issue/ALP-308/07-strategist-runner) "07 — Strategist runner") is Done — but the *assembler*-side computation of `risk_budget.entries` was never built.

## Scope

Implement the missing pipeline step. Conceptually: a function `build_risk_budget_consumption(snapshot, active_risk_parameters, effective_limits, library_config)` that emits one `RiskBudgetEntry` per active rule:

* One `sector_concentration_<sector>` entry per `config.active_sectors` (mirroring `build_active_specs` at `src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py:40`).
* `net_long_pct`, `net_short_pct` (gated by `short_selling_enabled`).
* `position_max_size_pct` (or whichever ID `project_analyst_view` projects via `per_position_size_rule_id`).
* Per-position options-greeks rules (gated by `options_enabled`) per `rules/options_greeks.py`.
* The capital + shorts rules from `rules/capital.py` / `rules/shorts.py`.

Each entry needs `current_value`, `limit_value`, `headroom`, `headroom_pct_of_limit`, `zone`, `unit`, `cumulative_invocation_impact_value`. The `current_value` is read from the snapshot per the matching `RuleSpec.current_provider`; the `limit_value` from `effective_limits[spec.effective_limit_key]`; the `zone` from the headroom + escalation-zone thresholds (the breach-detector module already classifies zones — share the logic).

Wire the new helper into `src/alphamind/portfolio_state/assembler.py` between the existing `get_risk_budget_consumption()` call (line 458) and the snapshot construction (line 657). Drop the empty-stub comment from `sql_repository.py:540`.

## Acceptance criteria

* `scripts/verify_debug_e2e.py --archive-root .archive/verify-debug-e2e` reaches `=== DEBUG-E2E VERIFICATION === 7/7 checks passed`. The synthetic-portfolio invocation runs through analyst + strategist + PM without the `resolve_sector_entries` `ValueError` or any sibling `require_budget_entry` failure.
* A new unit test in `tests/portfolio_state/` constructs a snapshot + limits and asserts the builder emits one entry per expected `rule_id` with correct `current_value` / `headroom` / `zone`.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.
* The empty-stub `RiskBudgetConsumption(entries=())` is gone from `sql_repository.py` (the new builder is the single source of truth).

## Related

* Blocked by: nothing — the missing helper is self-contained.
* Related: `src/alphamind/risk_guardrails/regime_adaptation/breach_detector.py` (zone classification logic to share).
* Related: `src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py` (`build_active_specs` — the canonical rule registry to mirror).
* Surfaced by: `scripts/RUNBOOK_end_to_end_verification.md` ([ALP-493](https://linear.app/alphamind-jatassi/issue/ALP-493/debug-e2e-mode) / [ALP-502](https://linear.app/alphamind-jatassi/issue/ALP-502/05-end-to-end-verification-runbook)).