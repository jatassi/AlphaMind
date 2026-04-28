---
status: not_started
completed_date:
commit_id:
---

# 05d — Capital, order, and parameter delivery-time computations

## Goal

Implement the small, focused delivery-time field computations the snapshot assembler applies on top of repository-supplied OMS records. Specifically: `cash_pct_of_portfolio` and `true_deployable_capital_usd` on `CashLedger`; `age_hours` on each `OrderRecord`; `parameter_change_flag` on `ActiveRiskParameterSet`. None of these compute the *risk budget consumption* itself — that is owned by the risk-guardrails enforcement layer (`state-delivery.md`) and arrives via the repository fully populated. This module fills the four delivery-time fields portfolio state owns by virtue of being the consumer-facing assembler.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 4a (`cash_pct_of_portfolio`, `true_deployable_capital`), § 4b ("Order age: hours since placement"), § 4d (`parameter_change_flag`: "whether anything changed since the last invocation — overnight regime shifts can invalidate yesterday's sizing assumptions")
- `../../../design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload — the boundary line: "State is computed by the enforcement layer at the start of Phase 1." Risk budget consumption (4c) and active risk parameter values (4d) are computed elsewhere; this story only handles the delivery-time *delta-flag* on parameters and the *consumer-facing computed fields* on cash and orders.
- `02-package-skeleton-and-config.md` — package layout (`computations/risk_budget.py` is this story's target despite the narrower-than-original scope; the filename is preserved per story 02's layout commitment)
- `03c-order-and-bracket-records.md` — `OrderRecord.submission_timestamp`, `OrderRecord.age_hours`
- `03d-capital-state-records.md` — `CashLedger`, `ActiveRiskParameterSet`, `ActiveRiskParameterEntry`
- `04b-repository-protocol.md` — `PriorInvocationContext.prior_active_risk_parameters`
- `05a-position-computations.md`, `05b-portfolio-pnl-and-drawdown-rollups.md` — sibling pure-function pattern

## Depends on

- 02
- 03c (consumes `OrderRecord`)
- 03d (consumes `CashLedger`, `ActiveRiskParameterSet`)
- 04b (consumes `PriorInvocationContext` for parameter diff)

## Scope

In scope, all under `src/alphamind/portfolio_state/computations/risk_budget.py` — pure functions with no I/O. Tests at `tests/portfolio_state/computations/test_risk_budget.py`.

Filename is preserved from story 02's layout. The narrower scope reflects the design boundary: the *risk budget consumption* itself is computed by the enforcement layer per `state-delivery.md`. This module owns the small portfolio-state-side delivery-time computations adjacent to that boundary.

### 1. Cash ledger computed fields

`compute_cash_pct_of_portfolio(current_cash_usd: float, total_portfolio_value_usd: float) -> float`

- Returns `(current_cash_usd / total_portfolio_value_usd) * 100.0` when `total_portfolio_value_usd > 0`.
- Returns `0.0` when `total_portfolio_value_usd == 0` (degenerate empty portfolio); never raises on zero division.
- Validation: `current_cash_usd >= 0` per the cash-ledger semantics; negative raises `ValueError`. `total_portfolio_value_usd >= 0`; negative raises `ValueError`.

`compute_true_deployable_capital_usd(cash_ledger: CashLedger) -> float`

- Returns `cash_ledger.settled_cash_usd - cash_ledger.reserved_capital_usd - cash_ledger.margin_held_usd`.
- May be negative when reserved + margin exceed settled cash (a transient state during settlement-cycle compression). The function does not clamp; the caller observes the signed value and decides whether to treat negative as an alarm condition.
- Validation: every input field is finite (already enforced by `CashLedger`'s validators per 03d); this function does no additional validation.

### 2. Order age

`compute_order_age_hours(submission_timestamp: datetime, now: datetime) -> float`

- Returns `(now - submission_timestamp).total_seconds() / 3600.0`.
- Both arguments must be tz-aware; mixing tz-naive and tz-aware raises `ValueError`.
- Mirrors story 05a's `compute_position_age_hours` shape; declared here because the assembler enriches `OrderRecord.age_hours` separately from `PositionRecord.position_age_hours` and the function lives with the order-related field it computes.
- Returns `0.0` when `submission_timestamp == now`; non-negative for `now >= submission_timestamp`. The caller is responsible for asserting the assembler's `now` is monotonic; the function does not clamp negative values.

### 3. Parameter change flag

`compute_parameter_change_flag(current: ActiveRiskParameterSet, prior: ActiveRiskParameterSet | None) -> bool`

- Returns `False` when `prior is None` (first invocation in process lifetime; nothing to compare against).
- Returns `True` when:
  - `current.regime_label != prior.regime_label`, OR
  - the set of `(rule_id, value)` pairs differs between `current.entries` and `prior.entries` (any rule's value changed, any rule was added or removed), OR
  - `current.active_overlays` differs from `prior.active_overlays` (overlay activated or deactivated).
- Returns `False` otherwise.
- Implementation note: compare the values element-wise rather than via `current == prior`, because the timestamps and metadata fields on the parent record are not relevant to the change determination — only the parameter set is.

The function returns a plain `bool`; the caller (the assembler) writes it to `ActiveRiskParameterSet.parameter_change_flag` when constructing the snapshot's parameter set instance.

### 4. Tests

- `compute_cash_pct_of_portfolio` happy path: cash=$1,500, total=$10,000 → `15.0`. Cash=0 → `0.0`. Cash=$1,000, total=$0 → `0.0` (no exception). Negative cash → `ValueError`. Negative total → `ValueError`.
- `compute_true_deployable_capital_usd` happy path: settled=$5,000, reserved=$1,000, margin=$500 → `3,500`. Negative result allowed: settled=$1,000, reserved=$1,200, margin=$0 → `-200`.
- `compute_order_age_hours` happy path on a 6-hour-old order; mixed tz-aware/tz-naive raises `ValueError`; `now < submission` returns negative without raising.
- `compute_parameter_change_flag(current, None)` returns `False`.
- `compute_parameter_change_flag(current, prior)` returns `True` when regime label differs.
- `compute_parameter_change_flag(current, prior)` returns `True` when one rule's value differs.
- `compute_parameter_change_flag(current, prior)` returns `True` when `active_overlays` differ.
- `compute_parameter_change_flag(current, prior)` returns `True` when a rule is added or removed.
- `compute_parameter_change_flag(current, prior)` returns `False` when both records are structurally identical (same regime, same rule values, same overlays) even with different `as_of_timestamp` or other metadata fields (which the function ignores).
- All functions are deterministic across repeated calls.

Out of scope:
- Risk budget consumption itself (computed by the risk-guardrails enforcement layer per `state-delivery.md`; the repository delivers a fully-populated `RiskBudgetConsumption` per story 04b).
- Zone classification (`NORMAL`/`WARNING`/`CRITICAL`/`BLOCKED`) for any rule — owned by the guardrail-evaluation library; this story does not derive zones.
- Reg T excess computations on `CashLedger` (per `regt-margin-attribution.md`, computed at delivery from fill-record metadata; the repository populates the trailing aggregates directly).
- Settlement-date arithmetic on unsettled proceeds (the cash ledger entries already carry settlement dates per 03d; this story does not transform them).
- The active risk parameter *values* themselves (owned by the risk-guardrails layer).

## Notes

This story's module name (`computations/risk_budget.py`) is preserved from story 02's package layout, even though the narrower scope is "delivery-time field computations adjacent to risk budget" rather than "risk budget computation." The naming preserves the layout commitment without misrepresenting the contents in the docstring or `__init__.py`. A future rename to `delivery_fields.py` is acceptable if profiling or readability warrants it.

Per `feedback_no_inventing_component_names.md`, every function name describes the field it produces, sourced from the design's field vocabulary on `CashLedger`, `OrderRecord`, and `ActiveRiskParameterSet`.

Per `feedback_avoid_numeric_anchors.md`, no threshold is hardcoded. The functions return raw numbers and a boolean; downstream consumers interpret them.

Per `feedback_simplify_before_building.md`, four pure functions in one module — no class hierarchy, no parameter-bag value objects, no helper class.

The `compute_parameter_change_flag` function uses element-wise comparison rather than `current == prior` because the records carry metadata (timestamps, etc.) that should not influence the change determination. The function compares only the structurally-significant fields per the design's intent: "whether anything *changed*" refers to operative parameters, not to metadata bookkeeping.

The risk budget consumption boundary deserves special note. Per `state-delivery.md` § Portfolio state ingestion payload, the enforcement layer computes per-rule current values, limits, headrooms, and zones. Portfolio state ingests them as a fully-populated `RiskBudgetConsumption` (story 03d). This story does not implement that pipeline. When the risk-guardrails feature lands, its enforcement layer wires into the assembler (story 06) and supplies the populated record; until then, the stub repository (story 04b) supplies a fixture-built `RiskBudgetConsumption` for tests.

## Acceptance criteria

- [ ] `compute_cash_pct_of_portfolio` returns the documented value; `0.0` on zero total; raises `ValueError` for negative cash or negative total.
- [ ] `compute_true_deployable_capital_usd` returns `settled - reserved - margin`; permits negative results without exception.
- [ ] `compute_order_age_hours` returns the correct hour-difference; mixed tz-aware/tz-naive raises `ValueError`; permits negative when `now < submission`.
- [ ] `compute_parameter_change_flag(current, None)` returns `False`.
- [ ] `compute_parameter_change_flag` returns `True` when regime label differs.
- [ ] `compute_parameter_change_flag` returns `True` when any rule's value differs.
- [ ] `compute_parameter_change_flag` returns `True` when `active_overlays` differ.
- [ ] `compute_parameter_change_flag` returns `True` when a rule is added or removed.
- [ ] `compute_parameter_change_flag` returns `False` when records are structurally identical (ignoring non-operative metadata).
- [ ] All functions are deterministic across repeated calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
