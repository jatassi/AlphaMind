# Borrow-cost resolver unwired — borrow_cost_budget guardrail is a silent no-op and short-equity validation always UNAVAILABLE

## Symptom

`borrow_cost_resolver` is hardcoded to `None` at `src/alphamind/scheduler/orchestrator.py:354` (`"borrow_cost_resolver": None` in `_build_decision_kwargs`). It is threaded as `None` through `run_decision_pipeline` into every decision-layer agent's `ValidationToolState` and into `to_library_snapshot`.

## Consequences

1. The `borrow_cost_budget_pct_per_day` guardrail rule (`risk_guardrails/guardrail_evaluation/rules/shorts.py`) reads `proposal.daily_borrow_cost_usd`, which is always `None` → `0.0` when the resolver is absent. The rule has therefore never contributed a non-zero projection — borrow-cost budgeting is unenforced.
2. Post-[ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped), `validate_guardrail` returns `overall: UNAVAILABLE` with `unavailable_reason: missing_borrow_cost_resolver` for every short-equity OPEN/ADD regardless of ticker — so no short-equity thesis can be validated in an equity expression at all.

## Root cause

The `borrow_cost_resolver` is never constructed — there is no resolver builder, and `_build_decision_kwargs` passes the literal `None`.

A design issue blocks a naive one-line wiring. The resolver is typed `Callable[[str], float]` and documented (`library_snapshot.py` docstring) as "maps a ticker to its daily borrow cost in **USD**", and it must be pure per ticker (the `ValidationToolState.borrow_cost_resolver` purity contract — equal ticker inputs produce equal outputs across the whole invocation). But daily borrow cost in USD is `notional × fee_rate ÷ trading-days` — it depends on proposal/position size, which a ticker-only pure function cannot know. The `borrow_cost_daily` table stores `fee_pct` (an annualized borrow-fee rate), not USD.

## Scope

The data exists. `data_sources/iborrowdesk/borrow_cost.py` runs a daily universe sweep into `borrow_cost_daily` (and `borrow_cost_intraday`), keyed by `(observation_date, ticker)`, with a `fee_pct` column. The `sec_lending` analysis tool already reads this store.

### Resolver redefinition

Redefine the resolver to return the **annualized borrow fee rate** rather than a USD figure. The type stays `Callable[[str], float]`; only the value's meaning changes. Update the docstrings on `ValidationToolState.borrow_cost_resolver` and `to_library_snapshot`.

### Call sites that compute USD

The two call sites that know the notional perform the `notional × rate ÷ trading-days` arithmetic:

* `_resolve_borrow_cost` in `risk_guardrails/state_delivery/validation_tool.py` — has `request.size`, so it computes `daily_borrow_cost_usd` for the proposal.
* The borrow-cost loop in `risk_guardrails/library_snapshot.py` (\~lines 286-297) — has each position's market value, so it computes the same for existing positions.

### Resolver construction and wiring

Build a snapshot-backed resolver: read the latest `borrow_cost_daily.fee_pct` per active-universe ticker once at invocation start into a dict; the resolver is the dict lookup — pure and total within the invocation, satisfying the purity contract. Wire it at `orchestrator.py:_build_decision_kwargs` in place of the `None`.

### Per-ticker sparsity

The iBorrowDesk store is sparse / partially populated (see [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped)'s CSCO note — `sec_lending` for CSCO returned all-null quantitative fields). A ticker with no `borrow_cost_daily` row must still produce a clean, distinguishable signal rather than a silent `0.0` or a crash. The [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped) `UNAVAILABLE` / `missing_borrow_cost_resolver` machinery currently fires only when the whole resolver is `None`; it should generalize to "resolver present but this ticker has no borrow data".

## Acceptance criteria

- [ ] `borrow_cost_resolver` is constructed (snapshot-backed over `borrow_cost_daily.fee_pct`) and wired at `orchestrator.py:_build_decision_kwargs`; no longer hardcoded `None`.
- [ ] The resolver contract is redefined to an annualized fee rate; `_resolve_borrow_cost` and `library_snapshot.py` compute `daily_borrow_cost_usd` from notional × rate; docstrings updated.
- [ ] The `borrow_cost_budget_pct_per_day` rule contributes a correct non-zero projection for a short-equity OPEN of a ticker with borrow data.
- [ ] A short-equity OPEN of a ticker with a `borrow_cost_daily` row validates (PASS/FAIL), not `UNAVAILABLE`.
- [ ] A ticker with no borrow row produces a distinguishable result via the [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped) coverage-gap path, not a crash or a silent `0.0`.

## Verification

* Unit-test the resolver builder against a seeded `borrow_cost_daily` (one ticker with a row, one without).
* Unit-test `validate_guardrail` for a short-equity OPEN: borrow data present → PASS/FAIL with a non-zero `borrow_cost_budget_pct_per_day` projection; borrow data absent → coverage-gap result.
* Confirm `library_snapshot.py`'s `daily_borrow_cost_pct` accumulation matches the library rule's read scale (the existing invariant comment at `library_snapshot.py:283-285` pins this).

## Notes

Follow-up to [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped), which made the missing resolver a distinguishable `UNAVAILABLE` outcome rather than an unhandled exception. [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped) surfaced the gap; this issue closes it for borrow cost. Peer to the validation-tool market-price coverage gap (separate issue).
