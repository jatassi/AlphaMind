---
status: done
completed_date: 2026-04-28
commit_id: 67cc3c7
---

# 01 — Scaffold and canonical types

## Goal

Lay the package skeleton for `src/alphamind/risk_guardrails/guardrail_evaluation/` and define the frozen dataclasses every later story builds on. The library is a pure-function math layer with three callers — the agent-side validation tool, the proposal pre-processor's combined-set check, and the engine T3 enforcement check — each composing the library's primitives with its own orchestration. This story defines the input/output shapes those primitives consume and produce; downstream stories implement the math against these types.

The canonical per-rule output object (`RuleProjection`) is the doc-anchored shape every caller pattern-matches on. `LibraryConfig`, `PortfolioStateSnapshot`, `ProposedDelta`, `MarketInputs`, and `LibraryOutput` are the boundary contract this library accepts and produces — agnostic to which caller is invoking it.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` — full primitive list, input table, canonical per-rule output object, caller orchestration table
- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — output contract showing `per_rule[]`, `delta_adjusted_exposure`, `greeks` shape on the validation-tool wrapper; library output is the tool's `per_rule[]` plus computed greeks
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — canonical rule IDs and their T1/T2/T3 enforcement status
- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — four zones (Normal/Warning/Critical/Hard block) the library collapses to three statuses (PASS/WARNING/FAIL)
- `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A `combined_set_impact` — `per_rule` shape reused verbatim; `breaches[]` and `contributors` are caller-side composition
- `src/alphamind/config/resolver.py` — the upstream `ResolvedConfig` shape this library's `LibraryConfig` is adapted from

## Depends on

(none — this is the first story in the work tree)

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` re-exports the public API surface defined below; `from alphamind.risk_guardrails.guardrail_evaluation import RuleProjection, Status, ...` is the canonical import path. The package's `__init__.py` is the only place external callers import from.
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` defining the boundary dataclasses below. Every dataclass uses `dataclass(frozen=True, slots=True)` so the library's inputs and outputs are immutable and hashable. `Mapping`/`tuple` substitute for `dict`/`list` on dataclass fields. Sub-dataclasses (e.g., `Greeks`) follow the same convention.
- `Status` enum (`enum.Enum` with string values): `PASS`, `WARNING`, `FAIL`. Mapping from breach-behavior zones is documented in 04 (projection engine); this story only defines the enum.
- `RuleProjection` (frozen dataclass): the canonical per-rule output reused verbatim by every caller. Fields:
  - `rule: str` — canonical rule ID from `rules-and-limits.md` (e.g., `sector_concentration_tech`, `net_long_pct`)
  - `status: Status`
  - `current: float` — current value before proposed deltas
  - `limit: float` — effective limit after profile × regime × overlay cascade (caller-supplied via `LibraryConfig.effective_limits`)
  - `projected_after: float` — current + Σ contributions
  - `headroom_remaining: float` — `limit - projected_after` (signed; negative on FAIL)
  - `unit: str` — display unit (e.g., `"% of portfolio (delta-adjusted)"`, `"% of portfolio per 1-pt IV move"`, `"USD/day"`)
- `Greeks` (frozen dataclass): `delta: float`, `gamma: float`, `theta: float`, `vega: float`. Used both as the per-leg result of the Black-Scholes core and as the strategy-level net result after aggregation.
- `Direction` enum: `LONG`, `SHORT`. String values match the design docs.
- `AssetType` enum: `EQUITY`, `OPTION`, `STRATEGY`. String values match the design docs.
- `ContractType` enum: `CALL`, `PUT`. Used inside `OptionLeg`.
- `OptionLeg` (frozen dataclass): `contract_type: ContractType`, `strike: float`, `expiration: date`, `quantity: int` (signed: positive = long the leg, negative = short the leg). Strategies carry a tuple of these.
- `ProposedDelta` (frozen dataclass): the caller's proposed exposure change. Fields:
  - `id: str` — caller-assigned ID (`REC-1`, `SA-3`, etc.) used for the pre-processor's `contributors` attribution; opaque to the library
  - `underlying: str` — root ticker
  - `sector: str` — sector key (matches a key in `LibraryConfig.active_sectors`)
  - `direction: Direction`
  - `asset_type: AssetType`
  - `notional_usd: float` — equity notional (unsigned magnitude; sign comes from `direction` at the delta-adjusted-exposure layer); for options/strategies, the premium-at-risk
  - `quantity: float` — shares for equity, contracts for options/strategies (positive integer; direction is on the dataclass)
  - `option_legs: tuple[OptionLeg, ...] | None` — required when `asset_type ∈ {OPTION, STRATEGY}`, `None` for equity. A single-leg option is a tuple of length 1. Strategies are tuples of length ≥ 2.
  - `action: Action` — `OPEN | ADD | CLOSE | ADJUST | CANCEL` (string enum). Only `OPEN | ADD | CLOSE` change exposure; `ADJUST | CANCEL` are passed through for the entry point's gate logic. Detailed handling lives in 05.
  - `existing_position_id: str | None` — required for `ADD | CLOSE | ADJUST`, must be `None` for `OPEN | CANCEL`.
  - `daily_borrow_cost_usd: float | None = None` — required (non-`None`) for short-equity OPEN/ADD proposals; consumed by the borrow-cost-budget rule in story 04. The library does not compute borrow cost; the caller supplies it from the data pipeline's iBorrowDesk feed (per `data_sources.yaml`'s `iborrowdesk` provider). `None` for non-shorts, options, and CLOSE/ADJUST/CANCEL.
  - `reserves_capital: bool = False` — `True` when the proposal is a non-marketable limit order whose capital is held by the broker until fill or cancel. `False` for marketable orders, options (premium is paid up front, not reserved), and CLOSE/ADJUST/CANCEL. Consumed by the pending-order-capital rule in story 04.
  - The dataclass is constructible without runtime validation beyond the type system; cross-field invariants (e.g., `option_legs is None ⇔ asset_type == EQUITY`, `direction=SHORT and asset_type=EQUITY and action ∈ {OPEN, ADD} ⇒ daily_borrow_cost_usd is not None`) are checked at the entry point in story 05.
- `PortfolioStateSnapshot` (frozen dataclass): the Phase-1 portfolio snapshot the library projects deltas against. Pre-aggregated read interface — the library does not iterate raw positions to compute exposures. Fields:
  - `portfolio_value_usd: float` — total equity (cash + position market value)
  - `cash_usd: float` — settled cash
  - `reserved_for_pending_orders_usd: float` — capital held against pending limit orders
  - `sector_exposure_pct: Mapping[str, float]` — delta-adjusted exposure per active sector, as % of portfolio
  - `net_long_pct: float`, `net_short_pct: float`, `gross_pct: float` — delta-adjusted directional exposures, as % of portfolio
  - `options_delta_pct: float` — sum of |delta| × spot × multiplier × signed direction over open option positions, as % of portfolio
  - `portfolio_theta_pct_per_day: float`, `portfolio_vega_pct_per_iv_point: float` — current Greeks footprint as % of portfolio
  - `total_short_pct: float` — gross short notional as % of portfolio
  - `single_short_max_pct: float` — largest single short position size as % of portfolio
  - `daily_borrow_cost_pct: float` — current daily borrow accrual as % of portfolio
  - `position_max_size_pct: float` — largest currently-held position size as % of portfolio (used for the per-position-size rule's `current` when no `OPEN` is in flight)
  - `existing_positions: Mapping[str, ExistingPosition]` — keyed on position ID; consumed by `ADD`/`CLOSE`/`ADJUST` rule contributions
- `ExistingPosition` (frozen dataclass): minimal per-position fields the library consults for `ADD`/`CLOSE` projection — `position_id: str`, `underlying: str`, `sector: str`, `direction: Direction`, `asset_type: AssetType`, `notional_usd: float`, `delta_adjusted_exposure_usd: float`, `current_greeks: Greeks | None`, `daily_borrow_cost_usd: float | None`, `reserves_capital_usd: float`. Greeks are `None` for equity positions; `daily_borrow_cost_usd` is `None` for long and options positions, populated for shorts; `reserves_capital_usd` is the capital the existing position holds against an unfilled non-marketable limit (zero for filled positions).
- `MarketInputs` (frozen dataclass): the market data the Black-Scholes math reads. Fields:
  - `underlying_prices: Mapping[str, float]` — spot per ticker
  - `risk_free_rate: float` — annualized, decimal (e.g., `0.045` for 4.5%)
  - `iv_provider: IvProvider` — protocol defined in 02b; the library calls it for IV lookups. The protocol lives in `iv_sourcing.py` (story 02b); this story imports a forward declaration as `Protocol` so `MarketInputs` can be defined without circular import.
  - `as_of: datetime` — snapshot timestamp; option time-to-expiration is computed as `expiration - as_of` in days/365.
- `LibraryConfig` (frozen dataclass): the carved subset of `ResolvedConfig` the library reads. The caller is responsible for adapting `ResolvedConfig` → `LibraryConfig` (story 02c provides the helper). Fields:
  - `effective_limits: Mapping[str, float]` — per-rule effective limit after profile × regime × overlay cascade (rule ID → limit value). Only rules in scope under the active profile appear.
  - `escalation_zones: Mapping[str, EscalationZones]` — per-rule warning/critical/hard-block percentages.
  - `feature_flags: FeatureFlagsView` — `options_enabled: bool`, `short_selling_enabled: bool`. Carved from `ResolvedConfig.feature_flags` (which has more fields the library does not consult).
  - `active_sectors: tuple[str, ...]` — sector keys in scope under the active profile (from `ResolvedConfig.profile.active_sectors`).
  - `active_regime: str` — regime label (`"normal"`, `"elevated"`, etc.); used by the conservative-buffer selector in 03.
  - `active_profile: str` — profile label (`"micro"`, `"medium"`, etc.); included for audit.
  - `conservative_buffer_pct: float` — base buffer applied to absolute delta (default `10`, meaning +10%); per-regime override is applied at the call site (story 03).
- `EscalationZones` (frozen dataclass): `warning: float`, `critical: float`, `hard_block: float` — percentages of the limit (e.g., `70.0`, `85.0`, `95.0`). Order invariant `warning < critical < hard_block` is asserted by `__post_init__`.
- `FeatureFlagsView` (frozen dataclass): `options_enabled: bool`, `short_selling_enabled: bool`. Subset of `ResolvedConfig.feature_flags` the library cares about.
- `Action` enum: `OPEN`, `ADD`, `CLOSE`, `ADJUST`, `CANCEL`. String values match the OMS command names.
- `IvSource` enum: `SURFACE`, `REALIZED_VOL_FALLBACK`. The library reports which source supplied the IV used in any options computation, so callers can feed it into their audit trails.
- `DeltaAdjustedExposure` (frozen dataclass) — the result of the per-proposal Black-Scholes / aggregation work, consumed by the projection layer:
  - `proposal_id: str` — pass-through of `ProposedDelta.id`
  - `signed_notional_usd: float` — equity: full notional signed by direction; options/strategies: `buffered_net_delta × spot × contract_multiplier × signed direction`
  - `net_greeks: Greeks | None` — `None` for equity, present for options/strategies
  - `iv_used: float | None` — `None` for equity, the IV used for greeks otherwise
  - `iv_source: IvSource | None` — `None` for equity, the source of the IV otherwise
  - `unbuffered_delta: float | None` — for options/strategies, the BS-computed |net delta| before the conservative buffer; useful for audit. `None` for equity.
- `LibraryOutput` (frozen dataclass) — the entry point's return shape. Fields:
  - `per_rule: tuple[RuleProjection, ...]` — one entry per rule in scope (rules disabled by feature flags do not appear)
  - `delta_adjusted: Mapping[str, DeltaAdjustedExposure]` — keyed on `ProposedDelta.id`; one entry per proposal, including equity (with `net_greeks=None`)
  - `feature_disabled: tuple[FeatureDisabledRejection, ...]` — entries for proposals filtered by the feature-flag gate; the projection layer never sees them. `()` when no proposals were filtered.
- `FeatureDisabledRejection` (frozen dataclass): `proposal_id: str`, `reason: str` (e.g., `"options_disabled_on_micro"`, `"shorts_disabled_on_small"`), `disabled_feature: str` (`"options"` or `"shorts"`).
- `__init__.py` re-exports every dataclass and enum named above. Acceptance test asserts the exact public-API surface.

Out of scope:

- Implementations of any function — this story is types only. `bs_greeks(...)`, `lookup_iv(...)`, `evaluate_proposals(...)`, etc. all land in later stories.
- Validation tool's cumulative-tracking wrapper, pre-processor `breaches[]`/`contributors`, and engine T3 transactional integration. Those caller-side compositions live with their respective implementations (state-delivery, proposal-pre-processor, execution-layer architecture).
- Adapter from `ResolvedConfig` to `LibraryConfig` — that's story 02c's responsibility. This story defines the target shape; 02c writes the adapter.
- IV provider Protocol body — defined in `iv_sourcing.py` (story 02b). This story uses a forward declaration so `MarketInputs.iv_provider` types check.

## Notes

**Why frozen dataclasses, not Pydantic.** Pydantic is the YAML loader's contract (story 03* of configuration management); inside the library, the boundary is Python-to-Python and validation already happened upstream. Frozen `slots=True` dataclasses give immutability, hashability, and zero runtime overhead. The library's purity discipline (story 05's tests assert hash stability of outputs) requires hashable inputs.

**Parallel `EscalationZones` types — same data, two representations.** The Python codebase carries `EscalationZones` in two places: `alphamind.config.models.guardrails.EscalationZones` (Pydantic v2; the YAML-loader's contract; consumed by `breach_behavior/04a`'s `classify_zone` for the four-zone rendering classifier) and `alphamind.risk_guardrails.guardrail_evaluation.types.EscalationZones` (frozen dataclass; this story; consumed by the projection engine's three-status classifier in story 04). Both encode `warning`, `critical`, `hard_block` percentages over the same source data in `config/guardrails.yaml`. The duplication exists because the library's `LibraryOutput` must be hashable for determinism tests, which the Pydantic version cannot reliably satisfy. The `from_resolved_config` adapter (story 02c) bridges the two — reading the Pydantic version from `ResolvedConfig.guardrails.rules` and producing the dataclass version on the library side. Implementers composing `breach_behavior/04a`'s `classify_zone` with the projection engine's `project_rule` should know they pass different `EscalationZones` instances (same numbers, different Python types).

**Per-position-size rule's `current`.** For an `OPEN`, `current` is the largest currently-held position size; the proposal's contribution is the proposal's own size (not added to current — the rule asks "is the new position itself too large"). For `ADD`, `current` is the existing position's size; the contribution is the addition. The rule contribution implementation in story 04 handles this branching; the type system carries enough information (`Action` + `existing_position_id`) for the contribution function to dispatch.

**Why pre-aggregated `PortfolioStateSnapshot`.** Per [`portfolio-state.md` § 4c](../../../design/01-data-layer/internal/portfolio-state.md): risk-budget consumption is a pre-computed read interface. The library does not own portfolio-iteration logic — that lives in the portfolio-state ingestion layer. The library projects deltas against pre-aggregated values, which is also what the validation tool's per-rule output shape expects.

**`existing_positions` mapping is small.** Most invocations have ≤ 20 open positions; passing the full mapping lets the library look up the target of an `ADD`/`CLOSE`/`ADJUST` without a second round-trip. The mapping is a frozen `MappingProxyType` to preserve `slots=True` immutability discipline.

**Rule IDs are strings, not enums.** The rule ID space is open-ended (sector concentration spreads across active sectors — `sector_concentration_tech`, `sector_concentration_semis`, etc.); enumerating them at the type level is awkward. Strings keyed against `LibraryConfig.effective_limits` are the contract.

**`Action.ADJUST | CANCEL` carry through but don't change exposure.** `ADJUST` modifies bracket parameters and `CANCEL` withdraws an unfilled order — neither changes delta-adjusted exposure or greeks. The library's per-proposal output reports them with `signed_notional_usd: 0.0` and `net_greeks: None` (equity) or `Greeks(0,0,0,0)` (options) so the projection sees no contribution. Detailed gating lives at the entry point in 05.

**`IvSource` is informational.** The library uses the IV regardless of source; the source label flows through to `DeltaAdjustedExposure.iv_source` so callers and the feedback loop can segment outcomes by IV provenance.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/__init__.py` exists and re-exports every name listed in the Scope section, and only those names.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` defines `Status`, `Direction`, `AssetType`, `ContractType`, `Action`, and `IvSource` as `enum.Enum` subclasses with the listed string values.
- [ ] `Greeks`, `OptionLeg`, `ProposedDelta`, `ExistingPosition`, `PortfolioStateSnapshot`, `MarketInputs`, `EscalationZones`, `FeatureFlagsView`, `LibraryConfig`, `DeltaAdjustedExposure`, `FeatureDisabledRejection`, `RuleProjection`, and `LibraryOutput` are `dataclass(frozen=True, slots=True)`.
- [ ] A unit test asserts every type-module dataclass is `frozen=True` and `slots=True` by reading the dataclass `__dataclass_params__` field.
- [ ] A unit test asserts every dataclass is hashable: `hash(instance)` succeeds for an instance of each.
- [ ] A unit test asserts `EscalationZones.__post_init__` raises `ValueError` when `warning >= critical` or `critical >= hard_block`.
- [ ] A unit test asserts the `Status`, `Direction`, `AssetType`, `ContractType`, `Action`, `IvSource` enum members and string values match the spec.
- [ ] A unit test asserts the public-API surface — `set(dir(...))` of the package excluding private dunders matches the documented re-export list.
- [ ] A unit test asserts `ProposedDelta.daily_borrow_cost_usd` defaults to `None` and `ProposedDelta.reserves_capital` defaults to `False`.
- [ ] `MarketInputs.iv_provider` types check against the forward `IvProvider` Protocol declaration; story 02b later replaces the forward declaration with the implemented protocol.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
