## Symptom

Surfaced by the [ALP-503](https://linear.app/alphamind-jatassi/issue/ALP-503/risk-budget-never-populated-end-to-end-analyst-crashes-on-empty) verify-debug-e2e run (`scripts/verify_debug_e2e.py`, archive `inv-20260517T152650Z-fafa3088` and again `inv-20260517T152508Z-032774ea` on 2026-05-17). With `risk_budget` populated ([ALP-503](https://linear.app/alphamind-jatassi/issue/ALP-503/risk-budget-never-populated-end-to-end-analyst-crashes-on-empty)'s fix), the decision pipeline progresses past the analyst and strategist cleanly. The pre-processor then crashes with:

```
alphamind.risk_guardrails.guardrail_evaluation.evaluate.LibraryInputError: proposal 'SA-2': STRATEGY requires option_legs
  at src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py:145
  via compute_combined_set_impact → evaluate_proposals → _validate_inputs
```

`SA-2` is the strategist's assessment of `debug-pos-07` (a STRATEGY position on MSFT). The strategist proposes a clean `close all` — no leg breakdown supplied — and the translator at `src/alphamind/decision/proposal_pre_processor/translator.py:123-136` builds a `ProposedDelta` with `asset_type=existing.asset_type` (STRATEGY, inherited from the existing position) and `option_legs=None`. The library validator at `evaluate.py:_check_asset_type_legs_consistency` rejects.

## Root cause

The library's CLOSE-on-options math is inconsistent across rules:

* `_gross_contribute` (`rules/exposure.py:147-154`) and the greek rules (`rules/options_greeks.py:_greek_contribution`) special-case CLOSE: they read `existing.notional_usd` / `existing.current_greeks * existing.quantity` directly. These work without `option_legs`.
* `_sector_concentration_contribute` (`rules/exposure.py:76`), `_net_long_contribute` (`rules/exposure.py:98`), and `_options_delta_contribute` (`rules/options_greeks.py:51`) read `dae.signed_notional_usd` uniformly across all actions.

For CLOSE on options/strategy, `compute_delta_adjusted_exposure` at `delta_adjusted.py:80-91` iterates `proposal.option_legs` to re-run Black-Scholes against current spot/IV. With `option_legs=None`, the loop is empty → `buffered_abs_delta=0` → `signed_notional_usd=0`. The three rules above then report zero impact from the close — wrong (closing a position decreases sector/net-long/options-delta exposure).

The validator at `_check_asset_type_legs_consistency` enforces `STRATEGY/OPTION ⇒ option_legs is not None` unconditionally — to compensate for the rule math's reliance on a non-empty DAE. The validator's blanket requirement is too strict for CLOSE actions.

The strategist is not the bug: a CLOSE assessment on an existing position legitimately doesn't carry the legs (the existing position already knows them).

## Scope

Make CLOSE-on-options math symmetric with `_gross_contribute`'s approach — read from the existing position, not the proposal:

**Library-side changes (**`src/alphamind/risk_guardrails/guardrail_evaluation/`**):**

* `rules/exposure.py:_sector_concentration_contribute`, `_net_long_contribute`, `_net_short_contribute` — branch on `proposal.action`. CLOSE reads `existing.delta_adjusted_exposure_usd` (signed by `existing.direction`) divided by `state.portfolio_value_usd * 100`; OPEN/ADD keep the current `dae.signed_notional_usd` path.
* `rules/options_greeks.py:_options_delta_contribute` — same branch. CLOSE reads `existing.delta_adjusted_exposure_usd` for options/strategy positions (skip for equity). The theta/vega rules in the same file already handle CLOSE via `_existing_greek_dollars`; mirror that pattern.
* `evaluate.py:_check_asset_type_legs_consistency` — relax the `STRATEGY/OPTION requires option_legs` check to apply only to `OPEN`/`ADD` actions. CLOSE/ADJUST/CANCEL on existing options/strategy positions are valid without legs.

**Caller-side**: no translator change needed once the library is symmetric; the translator's current `option_legs=None` for assessments becomes correct.

## Acceptance criteria

* `scripts/verify_debug_e2e.py --archive-root .archive/verify-debug-e2e` progresses past the pre-processor on the synthetic-portfolio invocation (closing `debug-pos-07` no longer raises `LibraryInputError`). 7/7 verification checks should pass unless an unrelated downstream issue blocks them.
* New tests in `tests/risk_guardrails/guardrail_evaluation/test_rules_exposure.py` and `test_rules_options_greeks.py` cover CLOSE on STRATEGY/OPTION positions: assert the sector / net_long / options_delta contributions equal `±existing.delta_adjusted_exposure_usd / portfolio_value * 100` (signed for direction), not zero.
* A test in `test_evaluate_proposals.py` constructs a CLOSE-on-STRATEGY `ProposedDelta` with `option_legs=None` and asserts `evaluate_proposals` accepts it (currently raises).
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Related: [ALP-503](https://linear.app/alphamind-jatassi/issue/ALP-503/risk-budget-never-populated-end-to-end-analyst-crashes-on-empty) (the analyst-side empty-budget bug that masked this one — fixed in PR #65).
* Surfaced by: `scripts/RUNBOOK_end_to_end_verification.md`.

## Reading

* `src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py:176-189` — the validator's `_check_asset_type_legs_consistency`.
* `src/alphamind/risk_guardrails/guardrail_evaluation/delta_adjusted.py:47-100` — DAE math.
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/exposure.py:62-78,93-125` — the three rule contributions that read `dae.signed_notional_usd` uniformly.
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/options_greeks.py:42-105` — `_options_delta_contribute` (uniform) vs `_greek_contribution` (action-branched).
* `src/alphamind/decision/proposal_pre_processor/translator.py:95-136` — strategist assessment → ProposedDelta translation that produces `option_legs=None`.