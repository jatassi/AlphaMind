---
status: done
completed_date: 2026-04-29
commit_id: c751a5a
---

# 04 — Projection engine and rule-contribution registry

## Goal

The library's deterministic projection layer. Two pieces, one cohesive story:

1. **Projection engine** — uniform `project_rule(...)` that takes `(rule_id, current, contributions, effective_limit, escalation_zones)` and returns the canonical `RuleProjection` with the right `Status`. The engine has zero rule-specific knowledge; status classification, headroom math, and unit propagation are uniform.
2. **Rule-contribution registry** — per-rule `RuleSpec` describing how each in-scope rule reads its `current` from `PortfolioStateSnapshot` and how each `ProposedDelta` (with its precomputed `DeltaAdjustedExposure`) contributes to the rule. The registry covers all 13 rules in scope at proposal-validation time.

Together they implement `guardrail-evaluation.md § Per-rule projection and evaluation` — the math primitive every caller composes against.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Per-rule projection and evaluation, § Output shape — the canonical per-rule projection
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — the in-scope rule list with units
- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — the four-zone (Normal/Warning/Critical/Hard block) → three-status (PASS/WARNING/FAIL) collapse; per-rule overrides for daily and cumulative drawdown
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Position size limits, § Sector concentration limits, § Directional exposure limits, § Gross exposure limit, § Options-specific limits, § Short-specific limits, § Capital sufficiency — the per-rule semantics for each contribution function
- `docs/design/04-decision-layer/proposal-pre-processor.md` § §1.A `combined_set_impact` — confirms the projection engine's per-rule output shape is reused verbatim; `breaches[]` and `contributors` are caller-side composition
- `docs/design/configuration-management.md` § `guardrails.yaml` — `escalation_zones` block per rule (the daily/cumulative drawdown overrides land in config, not code)
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py`, `delta_adjusted.py`, `effective_limits.py` (from earlier stories)

## Depends on

- 01 (canonical types)
- 02c (effective limits + feature gate — `LibraryConfig` carries `escalation_zones` per rule)
- 03 (delta-adjusted exposure — rule contributions consume `DeltaAdjustedExposure.signed_notional_usd` and `net_greeks`)

## Scope

In scope:

### Projection engine (`projection.py`)

- `project_rule(...)` — pure function:
  ```python
  def project_rule(
      *,
      rule_id: str,
      current: float,
      contributions: Iterable[float],
      effective_limit: float,
      zones: EscalationZones,
      unit: str,
      magnitude: bool = False,
      inverse: bool = False,
  ) -> RuleProjection:
  ```
  - `projected_after = current + sum(contributions)` — the contribution layer's outputs flow through unchanged (signed for theta/vega/net-long, absolute for gross/sector, etc., per each rule's contribution function).
  - `headroom_remaining = effective_limit - projected_after`.
  - `consumption_pct` derivation:
    - If `magnitude=True`: `consumption_pct = |projected_after| / effective_limit × 100`. Theta and vega use this — signed contributions sum to a signed projected value; the rule cap is on absolute magnitude.
    - Else: `consumption_pct = projected_after / effective_limit × 100`. Negative consumption (e.g., net-long rule against a net-short book) is a valid result and classifies as PASS.
  - `status` classification:
    - **Standard rules** (`inverse=False`):
      - `consumption_pct < zones.warning` → `Status.PASS`
      - `zones.warning ≤ consumption_pct < zones.hard_block` → `Status.WARNING` (covers both Warning and Critical zones from breach-behavior)
      - `consumption_pct ≥ zones.hard_block` → `Status.FAIL`
    - **Inverse rules** (`inverse=True`, e.g., `min_cash_reserve_pct`): the limit is a *minimum*, so "below the limit" is the failure direction. Classification flips:
      - `projected_after ≥ effective_limit` → `Status.PASS`
      - `projected_after` in the inverse warning band (within `(100 − zones.warning)%` of the limit on the deficit side) → `Status.WARNING`
      - `projected_after < effective_limit × (1 − (100 − zones.hard_block) / 100)` → `Status.FAIL`. Concretely: with `zones.hard_block=95`, FAIL fires when `projected_after < effective_limit × 0.95` — i.e., the cash reserve has fallen 5%+ below the minimum.
  - `effective_limit == 0`: defensive — raises `ProjectionError` (zero limits are blocked by the configuration semantic-self-test; this is a structural-error guard).
  - Returns `RuleProjection(rule=rule_id, status, current, limit=effective_limit, projected_after, headroom_remaining, unit)`.
  - The engine does not know about rule names beyond pass-through. Adding a rule = adding a `RuleSpec` to the registry; no projection-engine change. The two flags (`magnitude`, `inverse`) are the only general-purpose dispatch axes; rules with novel classification needs require either a new flag (rare, design-doc-driven) or a custom contribution function that pre-shapes the inputs to one of the existing flag combinations.
- `ProjectionError(Exception)` — defensive structural-error class.

### Rule-contribution registry (`rules/__init__.py`, `rules/exposure.py`, `rules/options_greeks.py`, `rules/shorts.py`, `rules/capital.py`)

- `RuleSpec` (frozen dataclass):
  ```python
  @dataclass(frozen=True, slots=True)
  class RuleSpec:
      rule_id: str
      unit: str
      read_current: Callable[[PortfolioStateSnapshot, LibraryConfig], float]
      contribute: Callable[[ProposedDelta, DeltaAdjustedExposure, PortfolioStateSnapshot, LibraryConfig], float]
      effective_limit_key: str  # the key into LibraryConfig.effective_limits
      requires_options: bool
      requires_shorts: bool
      magnitude: bool = False    # True for theta and vega; engine classifies on |projected_after|
      inverse: bool = False      # True for min_cash_reserve_pct; engine classifies "below limit = FAIL"
  ```
  - `requires_options=True` means the rule is only in scope when `feature_flags.options_enabled=True` (the rule's `effective_limit_key` is dropped from `effective_limits` upstream, so this flag is structural — but the registry filter is the cleanest expression).
  - `requires_shorts=True` is the parallel for short-specific rules.
  - `magnitude=True` means the rule constrains absolute magnitude (signed contributions sum to a signed `projected_after`; the engine takes `|projected_after|` before computing `consumption_pct`). Theta and vega use this.
  - `inverse=True` means the rule is a *minimum* (e.g., `min_cash_reserve_pct`); the engine classifies `projected_after < effective_limit` as FAIL rather than `>`. Min-cash-reserve uses this.
  - For sector concentration (per active sector), one `RuleSpec` per sector is generated dynamically by the registry's `build_active_specs(config)` function.
- `build_active_specs(config: LibraryConfig) -> tuple[RuleSpec, ...]`:
  - Returns the registry filtered to rules in scope under `config`. Rules absent from `config.effective_limits` are dropped. Rules requiring options/shorts are dropped if the corresponding feature flag is False.
  - For sector concentration: one `RuleSpec` per `sector ∈ config.active_sectors` with `rule_id=f"sector_concentration_{sector}"` and a sector-specific `read_current` / `contribute` closure capturing the sector key.
  - Order is stable: by `effective_limit_key` lexicographic, with sector-concentration entries sorted by sector key. Stable order matters because the entry point's output `per_rule[]` is ordered by registry order.
- **Per-rule implementations:**

#### Exposure rules (`rules/exposure.py`)

- **`position_max_size_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.position_max_size_pct` (the largest currently-held position size as % of portfolio).
  - `contribute(proposal, dae, state, config)` — for `OPEN`: returns the proposal's own position size as % of portfolio = `|dae.signed_notional_usd| / state.portfolio_value_usd × 100` only if it exceeds `state.position_max_size_pct` (i.e., contribution = `max(0, proposal_size_pct - state.position_max_size_pct)`). For `ADD`: returns the *increase* in the existing position's size (since current already reflects the existing largest, an ADD only contributes if it pushes its position above the existing largest). For `CLOSE`: 0 (closes never increase). For `ADJUST`/`CANCEL`: 0.
  - **Note.** This rule's `current + contribute` semantic is "what is the max position size after this proposal?" The contribution function returns the *delta* from `current` to the new max. Simpler alternative: contribution returns the proposal's full size; `current` is 0 (start at 0, project to the proposal's size). The latter loses information about existing positions when the proposal is smaller than the current max. The former is the choice — `current` reads the current max; contribution is "would this proposal push the max higher?"
  - `unit = "% of portfolio"`
- **`sector_concentration_{sector}`** (T1+T2+T3, per active sector):
  - `read_current(state, config)` — returns `state.sector_exposure_pct[sector]`.
  - `contribute(proposal, dae, state, config)` — if `proposal.sector == sector`: returns `dae.signed_notional_usd / state.portfolio_value_usd × 100`. The contribution is signed (a CLOSE on a long position contributes negatively); sector concentration projections see net effect across mixed proposals. Otherwise: 0.
  - `unit = "% of portfolio (delta-adjusted)"`
- **`net_long_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.net_long_pct`.
  - `contribute(proposal, dae, state, config)` — returns `dae.signed_notional_usd / state.portfolio_value_usd × 100` (signed). Net long is `total_long − total_short`: a positive `signed_notional_usd` (long add or short close) increases net long; a negative `signed_notional_usd` (short add or long close) decreases it. The contribution is signed in the same direction as the underlying net-long change.
  - **Negative `current` semantics.** When the book is net short, `state.net_long_pct` is negative. The rule's `effective_limit` is positive (`max net long`); `consumption_pct = projected_after / effective_limit × 100` is negative; the projection engine classifies as `PASS`. This is correct — a net-short book is trivially not breaching a max-net-long cap.
  - `unit = "% of portfolio (delta-adjusted)"`
- **`net_short_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.net_short_pct` (positive number representing net short magnitude when net direction is short, else 0 — the convention is documented in `PortfolioStateSnapshot`).
  - `contribute(proposal, dae, state, config)` — `-dae.signed_notional_usd / state.portfolio_value_usd × 100` (short contributions are negative `signed_notional`, so flipped sign makes them positive contributions to net short).
  - `unit = "% of portfolio (delta-adjusted)"`
- **`gross_exposure_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.gross_pct`.
  - `contribute(proposal, dae, state, config)` — `|dae.signed_notional_usd| / state.portfolio_value_usd × 100`. Gross is sum of absolutes; every contribution is positive.
  - **Edge.** A CLOSE on an existing long position has `dae.signed_notional_usd < 0` (per story 03's convention) → `|·|` is positive → contribution is positive. But the close *reduces* gross by removing the position. Convention: for gross, the contribution is the *signed change* — `dae.signed_notional_usd / state.portfolio_value_usd × 100` if the proposal opens or adds (CLOSE produces a *negative* contribution because gross decreases). Contribution function: `(|proposal_after| - |proposal_before|) / state.portfolio_value_usd × 100`. For OPEN: `proposal_before = 0`, `proposal_after = |dae.signed_notional_usd|`. For CLOSE-LONG: `proposal_before = existing_position.notional_usd`, `proposal_after = 0`, so contribution is `-existing_position.notional_usd / state.portfolio_value_usd × 100`. For ADD: contribution is the additional `|dae.signed_notional_usd|`. For CLOSE-SHORT: contribution is `-existing_position.notional_usd / state.portfolio_value_usd × 100` (gross decreases because the short is being closed).
  - The contribution function reads `state.existing_positions[proposal.existing_position_id]` for ADD/CLOSE proposals to compute the before/after.
  - `unit = "% of portfolio (delta-adjusted)"`

#### Options-greeks rules (`rules/options_greeks.py`, gated on `requires_options=True`)

- **`options_delta_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.options_delta_pct`.
  - `contribute(proposal, dae, state, config)` — for options/strategies (`asset_type != EQUITY`), returns `dae.signed_notional_usd / state.portfolio_value_usd × 100` (options-only delta-adjusted exposure). For equity: 0.
  - `unit = "% of portfolio (delta-adjusted)"`
- **`portfolio_theta_pct_per_day`** (T1+T2+T3, `magnitude=True`):
  - `read_current(state, config)` — returns `state.portfolio_theta_pct_per_day`.
  - `contribute(proposal, dae, state, config)` — for options/strategies, theta in dollar terms is `dae.net_greeks.theta × proposal.quantity × 100` (BS theta is per-calendar-day per leg; strategy net theta is sum-of-leg-thetas; multiplied by `quantity` (number of strategies) and contract multiplier 100). Convert to % portfolio: `theta_dollars / state.portfolio_value_usd × 100`. **Signed contribution.** For equity: 0. For CLOSE/ADD on options: same before/after pattern as gross — contribution is the signed change in theta footprint, computed via `state.existing_positions[proposal.existing_position_id].current_greeks.theta`.
  - **Magnitude classification.** Theta is signed (long options decay → negative theta; short options earn theta → positive theta). The rule constrains absolute magnitude (`max daily theta`). The contribution function returns the *signed* theta change; `current + Σ contributions` produces a signed `projected_theta`; the projection engine takes `|projected_after|` before computing `consumption_pct`. The dispatch is keyed on the rule's `magnitude` flag in `RuleSpec` (see § Rule-contribution registry — `RuleSpec` definition below). Theta sets `magnitude=True`; vega sets `magnitude=True`; all other rules default to `magnitude=False`.
  - `unit = "% of portfolio per day"`
- **`portfolio_vega_pct_per_iv_point`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.portfolio_vega_pct_per_iv_point`.
  - `contribute(proposal, dae, state, config)` — for options/strategies, vega in dollar terms is `dae.net_greeks.vega × proposal.quantity × 100 / 100` (the BS vega is per-1.00-IV-move per leg per story 02a; divide by 100 to get per-1-IV-point; multiply by quantity and contract multiplier). Convert to % portfolio. Magnitude rule: `magnitude=True`.
  - For equity: 0. For CLOSE on options: same before/after pattern.
  - `unit = "% of portfolio per 1-pt IV move"`

#### Short-specific rules (`rules/shorts.py`, gated on `requires_shorts=True`)

- **`total_short_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.total_short_pct`.
  - `contribute(proposal, dae, state, config)` — for short proposals (`direction=SHORT`): `|dae.signed_notional_usd| / state.portfolio_value_usd × 100` for OPEN/ADD; `-existing_position.notional_usd / state.portfolio_value_usd × 100` for CLOSE on a short. Long proposals: 0.
  - `unit = "% of portfolio"`
- **`single_short_max_pct`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.single_short_max_pct`.
  - `contribute(proposal, dae, state, config)` — same `current + delta` pattern as `position_max_size_pct`, scoped to shorts.
  - `unit = "% of portfolio"`
- **`borrow_cost_budget_pct_per_day`** (T1+T2+T3):
  - `read_current(state, config)` — returns `state.daily_borrow_cost_pct`.
  - `contribute(proposal, dae, state, config)` — for short-equity OPEN/ADD: `proposal.daily_borrow_cost_usd / state.portfolio_value_usd × 100` (the library reads the precomputed borrow cost from the proposal — set in 01's `ProposedDelta`). For short-equity CLOSE: `-existing_position.daily_borrow_cost_usd / state.portfolio_value_usd × 100` (closing a short releases its borrow accrual). Long proposals or options: 0.
  - `unit = "% of portfolio per day"`

#### Capital rules (`rules/capital.py`)

- **`min_cash_reserve_pct`** (T3, surfaced as PM context at T2):
  - `read_current(state, config)` — returns `state.cash_pct`.
  - `contribute(proposal, dae, state, config)` — capital impact: long OPEN/ADD on equity reduces cash by `proposal.notional_usd`; option OPEN/ADD reduces cash by the premium-at-risk (`proposal.notional_usd` for options); CLOSE-LONG releases cash equal to the position's notional; CLOSE-SHORT releases cash by the position's notional. SHORT OPEN does not reduce cash directly but the rule is enforced against directly available cash post-proposal (Reg T margin is handled separately).
  - **Convention.** This rule's "current" is cash %; the limit is the *minimum* (e.g., `≥ 10%`). The projection engine's "consumption" model assumes max-not-min limits. Special handling: invert. The rule's `effective_limit_key` is `min_cash_reserve_pct`; the projection logic for min-rules is — `consumption_pct = (1 - projected_after / current_above_limit) × 100`? No — simpler: classify min rules as inverse:
    - `Status.FAIL` if `projected_after < effective_limit` (below the floor)
    - `Status.WARNING` if `projected_after < effective_limit × (1 + warning_band_pct / 100)` (within the warning band above the floor)
    - `Status.PASS` otherwise
  - Add `inverse: bool = False` to `RuleSpec`. Min-cash sets it True; the engine reads the flag and uses inverse classification. Same flag pattern as `magnitude`.
  - `unit = "% of portfolio"`
- **`pending_order_capital_pct`** (T3):
  - `read_current(state, config)` — returns `state.reserved_for_pending_orders_usd / state.portfolio_value_usd × 100`.
  - `contribute(proposal, dae, state, config)` — non-marketable limit OPENs reserve capital equal to the proposal's notional; marketable orders (market orders, marketable limits) do not. The library does not classify orders as marketable; the proposal carries `proposal.reserves_capital: bool` (added field — same pattern as `daily_borrow_cost_usd`, `None`/`False`/`True` based on order type). Story 01's `ProposedDelta` adds this field. CANCEL on a pending order releases the reserved capital.
  - `unit = "% of portfolio"`

### Engine integration

- `project_rule` is called by the registry's batch projector:
  ```python
  def project_all(
      *,
      proposals_with_dae: Iterable[tuple[ProposedDelta, DeltaAdjustedExposure]],
      state: PortfolioStateSnapshot,
      config: LibraryConfig,
  ) -> tuple[RuleProjection, ...]:
  ```
  - Build active specs via `build_active_specs(config)`.
  - For each spec: compute `current = spec.read_current(state, config)`; compute `contributions = [spec.contribute(p, dae, state, config) for p, dae in proposals_with_dae]`; call `project_rule(...)` with the spec's `unit` and `effective_limit = config.effective_limits[spec.effective_limit_key]`, `zones = config.escalation_zones[spec.effective_limit_key]`, taking magnitude/inverse flags into account.
- The projection engine and the registry are exported separately so callers can compose either:
  - Most callers use `project_all` (the validation tool, pre-processor, engine).
  - Tests can call `project_rule` directly with synthetic inputs.
- Re-export `project_rule`, `project_all`, `build_active_specs`, `ProjectionError`, `RuleSpec` from `guardrail_evaluation/__init__.py`.

### Story-01 type extensions

This story extends `ProposedDelta` (defined in 01) to add:
- `daily_borrow_cost_usd: float | None = None` — required for short-equity OPENs; otherwise `None`.
- `reserves_capital: bool = False` — `True` when the proposal is a non-marketable limit order whose capital is held until fill/cancel; `False` for marketable orders, options (premium is paid up front), and CLOSE/ADJUST/CANCEL.

These fields are noted as forward-looking placeholders in 01's notes — the test added in 01 verifies the fields exist with the documented defaults. (Story 01's spec is authoritative; this story implements consumers.)

### Tests

- **Projection engine.**
  - `project_rule` with `current=10, contributions=[5], effective_limit=20, zones=(70, 85, 95)` → `consumption=75% → WARNING`.
  - `project_rule` with `current=18, contributions=[5], effective_limit=20` → `consumption=115% → FAIL`.
  - `project_rule` with `current=5, contributions=[2], effective_limit=20` → `consumption=35% → PASS`.
  - `project_rule` with `effective_limit=0` raises `ProjectionError`.
  - `project_rule` with magnitude rule and signed contributions: `current=-0.10, contributions=[-0.05], effective_limit=0.15, magnitude=True` → `|projected_after|=0.15 → consumption=100% → FAIL`.
  - `project_rule` with inverse rule (min-cash): `current=12, contributions=[-3], effective_limit=10, zones=(70, 85, 95), inverse=True` → `projected_after=9 < 10 → FAIL`.
  - `project_rule` with inverse rule, projected within warning band: `effective_limit=10`, `projected_after=10.5`, warning band logic — test pins the band convention.
  - Pure: equal inputs produce equal `RuleProjection`s.
- **Registry — exposure.** Each exposure rule's `read_current` and `contribute` is unit-tested with synthetic state and proposals, asserting the documented arithmetic.
- **Registry — options-greeks.** With `requires_options=True`, the registry includes options rules under medium profile; under micro profile (options disabled), the rules are absent from `build_active_specs`.
- **Registry — shorts.** Symmetric coverage for the three short rules.
- **Registry — capital.** Min-cash-reserve uses inverse classification; pending-order-capital uses normal classification.
- **Sector-concentration registry expansion.** With `active_sectors=("tech", "semis", "financials", "energy")`, `build_active_specs` produces four sector rules with `rule_id ∈ {"sector_concentration_tech", ..., "sector_concentration_energy"}`.
- **Stable order.** `build_active_specs` returns the same rule order across calls with equal config.
- **`project_all` end-to-end.** Construct a small portfolio state, two proposals (one long equity in tech, one short equity in semis), run `project_all` → assert the resulting `per_rule` includes net long, net short, gross, position max size, sector concentration tech/semis, and total short with the expected `current`/`projected_after`/`status` per rule.
- **Closure under feature flags.** Under micro profile (options/shorts disabled), `project_all` returns no options-greeks or short-rule projections.

Out of scope:

- The entry point's orchestration loop (story 05) — it composes `compute_delta_adjusted_exposure` (per proposal) with `project_all`.
- The validation tool's cumulative-state wrapper, the pre-processor's `breaches[]`/`contributors`, the engine's transactional integration.
- A literal lookup for `magnitude_warning_band_pct` and `inverse_warning_band_pct` constants — these land in this story's source as documented constants; values from breach-behavior § Escalation model (`70/85/95`) apply uniformly except where overridden in `guardrails.yaml`. The min-cash inverse band is a per-rule override.

## Notes

**Why the magnitude/inverse flags.** Three rules don't fit the standard "max limit, signed contribution" pattern: theta and vega are magnitude rules (constrain absolute exposure), and min-cash is an inverse rule (constrain minimum). The cleanest expression is per-rule flags on `RuleSpec`; the projection engine reads the flags and dispatches to one of three classification paths (standard, magnitude-absolute, inverse-floor). The alternative — three separate engine functions — duplicates the math and the unit propagation.

**Why CLOSE has signed contributions, not separate "remove" handling.** The story-03 convention (CLOSE produces `signed_notional_usd < 0` for the *direction of change*) means rule contributions just sum, regardless of `Action`. Sector concentration: `current + Σ signed` — a long CLOSE in the sector reduces the projection. Net long: same. Gross: requires the before/after pattern because gross is `Σ |·|` not `|Σ ·|` — a long CLOSE *reduces* gross by removing the position. Story-03's CLOSE convention does not directly help gross; the gross contribution function explicitly reads `state.existing_positions` for the before-state.

**The `position_max_size_pct` rule is unusual.** It's not a sum across the portfolio — it's a max. The rule's contribution function returns the *delta from current max to new max if new max > current max*, which makes the projection engine's standard `current + Σ contributions` work. Tests pin this (an OPEN smaller than `state.position_max_size_pct` contributes 0; an OPEN larger contributes `proposal_size - state.position_max_size_pct`).

**`single_short_max_pct` follows the same `max` pattern as `position_max_size_pct` but scoped to shorts.**

**Why the registry is a tuple of `RuleSpec`s, not a class hierarchy.** A `RuleSpec` is data + two function references. A class-per-rule pattern adds boilerplate without conveying anything. The closure pattern (per-sector spec generation) is cleaner with `RuleSpec` than with class subclassing.

**Why per-rule rules-and-limits ID strings, not enum.** Per the 01 note: rule IDs are open-ended (sector concentration spreads across active sectors). Strings keyed against `LibraryConfig.effective_limits` are the contract; the registry generates `sector_concentration_{sector}` IDs at runtime.

**`current_above_limit` warning band for inverse rules.** For min-cash with limit=10%, we want `WARNING` when projected lands between 10% and ~12% (close to floor). The cleanest expression: a fixed `MIN_RULE_WARNING_BAND_PCT = 20.0` (meaning within 20% above the floor, e.g., 10% × 1.20 = 12%, projection in [10%, 12%) is WARNING). Story-internal constant. Operator-tunable later if the band proves wrong.

**`magnitude` does not affect headroom sign.** A theta rule with `current=-0.10, projected_after=-0.13, limit=0.15`: `|projected|=0.13`, `consumption=87% → WARNING`. `headroom_remaining = limit - |projected_after| = 0.02`. The `RuleProjection.headroom_remaining` is reported against the absolute value for magnitude rules; documented in the unit string (`% of portfolio per day` is the absolute-magnitude basis).

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/projection.py` exists and defines `project_rule(...)` and `ProjectionError`.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py` exists and defines `RuleSpec`, `build_active_specs(...)`, `project_all(...)`.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/rules/exposure.py` defines specs for `position_max_size_pct`, `sector_concentration_*` (per-sector factory), `net_long_pct`, `net_short_pct`, `gross_exposure_pct`.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/rules/options_greeks.py` defines specs for `options_delta_pct`, `portfolio_theta_pct_per_day`, `portfolio_vega_pct_per_iv_point` with `requires_options=True` and `magnitude=True` on theta/vega.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/rules/shorts.py` defines specs for `total_short_pct`, `single_short_max_pct`, `borrow_cost_budget_pct_per_day` with `requires_shorts=True`.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/rules/capital.py` defines specs for `min_cash_reserve_pct` (with `inverse=True`) and `pending_order_capital_pct`.
- [ ] `RuleSpec`, `project_rule`, `project_all`, `build_active_specs`, `ProjectionError` are re-exported from `guardrail_evaluation/__init__.py`.
- [ ] A unit test asserts `project_rule(current=10, contributions=[5], limit=20, zones=(70,85,95))` returns `Status.WARNING`.
- [ ] A unit test asserts `project_rule(current=18, contributions=[5], limit=20)` returns `Status.FAIL`.
- [ ] A unit test asserts `project_rule(current=5, contributions=[2], limit=20)` returns `Status.PASS`.
- [ ] A unit test asserts `project_rule(effective_limit=0)` raises `ProjectionError`.
- [ ] A unit test asserts magnitude classification — signed contributions whose absolute sum exceeds the limit produce `FAIL`.
- [ ] A unit test asserts inverse classification — `projected_after < effective_limit` produces `FAIL`; `effective_limit ≤ projected_after < effective_limit × 1.2` produces `WARNING`; otherwise `PASS`.
- [ ] A unit test asserts each exposure-rule `contribute` function returns the documented value for OPEN, ADD, CLOSE proposals.
- [ ] A unit test asserts `position_max_size_pct.contribute` returns 0 when the proposal is smaller than `state.position_max_size_pct` and returns the delta when larger.
- [ ] A unit test asserts `gross_exposure_pct.contribute` correctly accounts for CLOSE-LONG and CLOSE-SHORT (gross decreases) using `state.existing_positions`.
- [ ] A unit test asserts options-greeks specs are dropped from `build_active_specs` under `feature_flags.options_enabled=False`.
- [ ] A unit test asserts short-rule specs are dropped under `feature_flags.short_selling_enabled=False`.
- [ ] A unit test asserts sector-concentration spec generation produces one spec per `config.active_sectors` entry, named `sector_concentration_{sector}`.
- [ ] A unit test asserts `build_active_specs` returns rules in stable lexicographic order.
- [ ] A unit test asserts `project_all` against a small synthetic state with two proposals produces the documented `per_rule` set with correct `current`/`projected_after`/`status` values.
- [ ] A unit test asserts magnitude rules' `headroom_remaining` is reported against the absolute value of `projected_after`.
- [ ] A unit test asserts `ProposedDelta.daily_borrow_cost_usd` and `ProposedDelta.reserves_capital` exist with documented defaults (verifies the 01-spec extensions land).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
