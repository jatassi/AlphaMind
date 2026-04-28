---
status: in_progress
completed_date:
commit_id:
---

# 03d — Capital state records

## Goal

Define the consumer-facing typed records for capital and capacity state (raw state categories 2c, 4a, 4c, 4d — drawdown tracking, cash and buying power, risk budget consumption, active risk parameter set) at `src/alphamind/portfolio_state/records/capital.py`, with field constraints validated via Pydantic v2 and a corresponding test suite at `tests/portfolio_state/records/test_capital.py`.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — sections 2c (drawdown tracking), 4a (cash and buying power), 4c (risk budget consumption), 4d (active risk parameter set) — consumer-facing delivery contracts and computed fields
- `../../../design/05-execution-layer/state-persistence.md` § Cash ledger — the OMS fields this story's `CashLedger` surfaces: current cash, settled cash, reserved capital, available buying power, margin held, unsettled proceeds with settlement dates
- `../../../design/05-execution-layer/regt-margin-attribution.md` — `regt_excess_over_pm` per-fill attribution; defines the three trailing aggregate fields (`regt_excess_trailing_30d`, `regt_excess_trailing_90d`, `regt_excess_lifetime`) added to the 4a delivery contract
- `../../../design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload / Integration with portfolio state categories — defines what 4c and 4d deliver into the guardrail state header; zone vocabulary (normal/warning/critical/blocked), transition state (stable/tightening/loosening with invocations remaining), cumulative tracking seam
- `../../../design/06-risk-guardrails/rules-and-limits.md` — headers only, for the per-rule taxonomy `RiskBudgetConsumption` iterates over (rule IDs stored as opaque strings in this story)
- `../../../design/06-risk-guardrails/regime-adaptation.md` — four regime labels (low-vol compression, normal, elevated, crisis) and loosening transition mechanics (invocations remaining); regime-multiplier shape per rule
- `../../../design/06-risk-guardrails/breach-behavior.md` — cumulative drawdown progressive response tiers and proximity zone threshold model (zone names sourced here; thresholds are owned by the guardrail-evaluation library, not this story)
- `02-package-skeleton-and-config.md` — package layout; `src/alphamind/portfolio_state/records/capital.py` is listed as story 03d's target
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: frozen Pydantic v2 records, StrEnum discriminators, validation tests, acceptance-criteria style

## Depends on

- 02

## Scope

In scope:

1. **Enums** — all `StrEnum`, members in ALL CAPS:

   - `RegimeLabel`: `LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS` — four labels from `regime-adaptation.md` mapping to the volatility classification in quant 11f
   - `RegimeTransitionState`: `STABLE`, `TIGHTENING`, `LOOSENING` — per `state-delivery.md` "transition state (stable / tightening / loosening with invocations remaining)"
   - `RiskZone`: `NORMAL`, `WARNING`, `CRITICAL`, `BLOCKED` — per `state-delivery.md` per-rule "zone (normal/warning/critical/blocked)" and `breach-behavior.md` escalation model
   - `DrawdownTier`: `CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT` — per `breach-behavior.md` cumulative drawdown progressive response: 8% depth (CONSTRAINED), 10% depth (HEAVILY_CONSTRAINED), 12% depth (FULL_HALT). The "no response active" state is represented by `DrawdownState.cumulative_tier == None`, not by an enum sentinel — see Notes.

2. **Value objects** — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`:

   - `UnsettledProceedsEntry`:
     - `settlement_date: datetime` — tz-aware UTC; the date the proceeds clear the settlement cycle per venue configuration
     - `amount_usd: float` — proceeds amount; finite float
     - `source_transaction_id: str` — opaque identifier linking back to the originating fill or transaction

   - `CashLedger` (raw state 4a):
     - `current_cash_usd: float` — total cash including unsettled proceeds; per the cash ledger entity in `state-persistence.md`
     - `settled_cash_usd: float` — cash available for new purchases, accounting for settlement cycles per venue configuration
     - `reserved_capital_usd: float` — dollar value of pending entry orders (committed but not yet deployed)
     - `available_buying_power_usd: float` — settled cash minus reserved capital, margin-adjusted
     - `margin_held_usd: float` — total margin collateral across all margin-requiring positions (short equity, short options)
     - `unsettled_proceeds: tuple[UnsettledProceedsEntry, ...]` — cash from recent sells that has not cleared the settlement cycle; empty tuple when no in-flight settlements
     - Consumer-facing computed fields (shape declared here; population is the assembler's job — story 06):
       - `cash_pct_of_portfolio: float` — cash as a percentage of total portfolio value (cash + position market value); per `portfolio-state.md` § 4a
       - `true_deployable_capital_usd: float` — settled cash minus reserved capital minus margin held; the capital actually available for new positions, accounting for all active commitments
       - `regt_excess_trailing_30d_usd: float` — trailing 30-day sum of `regt_excess_over_pm` across processed fills; per `regt-margin-attribution.md`
       - `regt_excess_trailing_90d_usd: float` — trailing 90-day sum of `regt_excess_over_pm`
       - `regt_excess_lifetime_usd: float` — lifetime sum of `regt_excess_over_pm` since the attribution module was activated; operator's broker-switch signal

   - `DrawdownState` (raw state 2c):
     - `current_drawdown_pct: float` — percentage decline from the most recent equity high-water mark; non-negative
     - `equity_high_water_mark_usd: float` — portfolio equity value at the most recent peak
     - `drawdown_duration_hours: float` — hours since portfolio equity last reached the high-water mark; non-negative
     - `lifetime_max_drawdown_pct: float` — deepest peak-to-trough decline since system inception; non-negative; calibration input for risk guardrail tuning
     - `intraday_drawdown_pct: float` — worst drawdown point today vs. opening equity; non-negative; resets each day at market open
     - `daily_zone: RiskZone` — proximity zone for the daily drawdown limit (the daily limit uses the 60/80/90 override thresholds per `breach-behavior.md`)
     - `cumulative_zone: RiskZone` — proximity zone for the cumulative drawdown limit (50/70/85 override thresholds per `breach-behavior.md`)
     - `cumulative_tier: DrawdownTier | None` — active cumulative drawdown response tier; `None` when cumulative drawdown has not triggered any response; otherwise the tier corresponding to the current drawdown depth
     - `drawdown_by_source_pct: dict[str, float]` — per-position or per-sector contribution to current drawdown expressed as percentage; keys are position IDs or sector labels; sourced from portfolio-state.md "Drawdown by source: which positions/sectors contributed"

   - `RiskBudgetEntry` — one rule's slot in the risk budget consumption snapshot:
     - `rule_id: str` — stable opaque identifier matching the rule taxonomy in `rules-and-limits.md`; stored as string; this story does not own the taxonomy
     - `rule_label: str` — human-readable rule name for display and logging
     - `current_value: float` — current measured value of the metric this rule governs
     - `limit_value: float` — active limit value (regime-adjusted; resolved by the guardrail-evaluation library before delivery)
     - `headroom: float` — `limit_value - current_value`; enforced at construction (see validation)
     - `headroom_pct_of_limit: float` — headroom as a percentage of `limit_value`, clipped to [0, 100]; 0 when `current_value ≥ limit_value`, 100 when `current_value ≤ 0`
     - `zone: RiskZone` — the proximity classification for this rule; provided by the caller (the guardrail-evaluation library, which owns the threshold logic); this record validates only that `zone` is a valid `RiskZone` member — zone classification is the caller's responsibility, not the record's
     - `unit: str` — unit label for display (e.g., `"% of portfolio (delta-adjusted)"`, `"USD"`, `"ratio"`)
     - `cumulative_invocation_impact_value: float` — net cumulative impact of in-invocation guardrail-validation calls already accumulated at snapshot time; per `state-delivery.md` "Cumulative tracking: State persists across calls within a single agent invocation." The portfolio state record carries the initial cumulative state at snapshot time (typically zero at snapshot assembly); the validation tool accumulates its own per-call delta as the agent reasons

   - `RiskBudgetConsumption` (raw state 4c):
     - `entries: tuple[RiskBudgetEntry, ...]` — one entry per rule the system tracks; ordering deterministic by `rule_id` ascending; rule IDs are unique across entries (see validation)
     - Convenience helpers (implemented as `@property` methods — Pydantic v2 frozen models support properties):
       - `entries_by_zone(zone: RiskZone) -> tuple[RiskBudgetEntry, ...]` — all entries whose `zone` matches the argument; empty tuple when none match
       - `breaching_entries() -> tuple[RiskBudgetEntry, ...]` — entries with `zone` in `{CRITICAL, BLOCKED}`; empty tuple when none
       - `entry_by_rule_id(rule_id: str) -> RiskBudgetEntry | None` — entry whose `rule_id` matches; `None` when no match

   - `ActiveRiskParameterEntry` — one rule's slot in the active parameter set:
     - `rule_id: str` — stable opaque identifier matching the rule taxonomy in `rules-and-limits.md`
     - `rule_label: str` — human-readable rule name
     - `value: float` — current active limit value for this rule (regime-adjusted)
     - `unit: str` — unit label for display
     - `regime_multiplier_applied: float` — the multiplier applied vs. the base parameter (from the regime-adaptation multiplier table); 1.0 when regime is NORMAL or when the rule has no regime multiplier
     - `base_value: float` — pre-regime-multiplier base value from `rules-and-limits.md`

   - `ActiveRiskParameterSet` (raw state 4d):
     - `regime_label: RegimeLabel` — current volatility regime driving the active parameter set
     - `transition_state: RegimeTransitionState` — whether the system is stable in the current regime, tightening toward a higher-vol regime, or loosening toward a lower-vol regime
     - `transition_invocations_remaining: int` — invocations remaining in a loosening transition (0 when `transition_state == STABLE`; counts down from 2 toward 0 as loosening interpolation advances per `regime-adaptation.md`); see validation
     - `parameter_change_flag: bool` — `True` if any parameter value changed since the prior invocation; computed by the snapshot assembler (story 06) by diffing the current and prior parameter sets; the record stores the boolean result
     - `entries: tuple[ActiveRiskParameterEntry, ...]` — one entry per rule, ordered by `rule_id` ascending; rule IDs are unique (see validation)
     - `active_overlays: tuple[str, ...]` — names of any active regime overrides (e.g., pre-event tightening overlay, stress overlay); empty tuple when none active; per `state-delivery.md` "Active regime overrides"

3. **Validation rules** (enforced via Pydantic `model_validator` or `field_validator`):

   - All `*_usd` float fields in `CashLedger`, `DrawdownState`, and `RiskBudgetEntry` must be finite (no NaN, no ±Inf) — use `pydantic.Field` with `allow_inf_nan=False` or an equivalent validator.
   - `CashLedger.cash_pct_of_portfolio` in `[0, 100]`.
   - `DrawdownState.current_drawdown_pct >= 0`.
   - `DrawdownState.lifetime_max_drawdown_pct >= 0`.
   - `DrawdownState.intraday_drawdown_pct >= 0`.
   - `DrawdownState.drawdown_duration_hours >= 0`.
   - `RiskBudgetEntry.headroom == limit_value - current_value` within 1e-9 tolerance; validate at construction and raise `ValidationError` on mismatch.
   - `RiskBudgetEntry.headroom_pct_of_limit` in `[0, 100]`; the assembler must supply this clipped; the record validates the range.
   - `RiskBudgetConsumption.entries` rule IDs are unique; duplicate `rule_id` values raise `ValidationError`.
   - `ActiveRiskParameterSet.transition_invocations_remaining >= 0`.
   - `ActiveRiskParameterSet.transition_state == STABLE` ⇒ `transition_invocations_remaining == 0`; violation raises `ValidationError`.
   - `ActiveRiskParameterSet.entries` rule IDs are unique; duplicate `rule_id` values raise `ValidationError`.

4. **Tests** at `tests/portfolio_state/records/test_capital.py`:
   - Each enum: correct member set, correct string values.
   - `RegimeLabel` has exactly `LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`.
   - `RegimeTransitionState` has exactly `STABLE`, `TIGHTENING`, `LOOSENING`.
   - `RiskZone` has exactly `NORMAL`, `WARNING`, `CRITICAL`, `BLOCKED`.
   - `DrawdownTier` has exactly `CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT`.
   - `UnsettledProceedsEntry`: valid construction passes; `settlement_date` without timezone raises `ValidationError`.
   - `CashLedger`: valid construction with populated and empty `unsettled_proceeds`; `cash_pct_of_portfolio` outside `[0, 100]` raises `ValidationError`; non-finite `*_usd` field raises `ValidationError`.
   - `DrawdownState`: valid construction; `current_drawdown_pct < 0` raises `ValidationError`; `lifetime_max_drawdown_pct < 0` raises `ValidationError`; `intraday_drawdown_pct < 0` raises `ValidationError`; `drawdown_duration_hours < 0` raises `ValidationError`; `cumulative_tier` accepts `None` and a `DrawdownTier` value.
   - `RiskBudgetEntry`: valid construction; `headroom` inconsistent with `limit_value - current_value` raises `ValidationError`; `headroom_pct_of_limit` outside `[0, 100]` raises `ValidationError`; invalid `zone` value raises `ValidationError`.
   - `RiskBudgetConsumption`: valid construction with multiple entries; duplicate `rule_id` values raise `ValidationError`; `entries_by_zone` returns the correct subset; `breaching_entries` returns entries with `zone` in `{CRITICAL, BLOCKED}` only; `entry_by_rule_id` returns the matching entry and `None` on a miss.
   - `ActiveRiskParameterSet`: valid construction; `transition_state == STABLE` with `transition_invocations_remaining != 0` raises `ValidationError`; `transition_state == LOOSENING` with `transition_invocations_remaining == 2` passes; `transition_invocations_remaining < 0` raises `ValidationError`; duplicate `rule_id` in `entries` raises `ValidationError`; empty `active_overlays` passes.

Out of scope:

- The rule-ID taxonomy itself — which rules exist, their string identifiers, their base limit values. Owned by `rules-and-limits.md` and the risk-guardrails work tree. This story stores `rule_id` as an opaque string.
- Zone classification logic — the thresholds that map `headroom_pct_of_limit` to a `RiskZone`. Owned by the guardrail-evaluation library; this story validates only that `zone` is a valid `RiskZone` enum member.
- Drawdown limit values and the numeric thresholds that gate `DrawdownTier` escalation. Owned by `rules-and-limits.md` and `breach-behavior.md`.
- Reg T excess computation logic. Owned by `regt-margin-attribution.md`; this story stores the trailing sums as pre-computed fields.
- Active overlay state machine — the logic that activates and deactivates pre-event tightening or stress overlays. Owned by `regime-adaptation.md`; this story stores active overlay names as a tuple of strings.
- Computed-field population for `CashLedger` (`cash_pct_of_portfolio`, `true_deployable_capital_usd`, `regt_excess_*_usd`) — shape declared here; population is the assembler's job (story 06).
- Repository / OMS reads (story 04b).

## Notes

These four records are the data-layer's contract for what the guardrail state header (`state-delivery.md`) renders. The header rendering is the risk-guardrails feature; this story owns only the read-side data shape.

Per `feedback_avoid_numeric_anchors.md`, validation is structural (non-negative, headroom identity invariant, zone-is-valid-enum, range checks on percentages) — no thresholds for "high drawdown" or "concentrated cash position" are baked into field docstrings. The enum values are the structural classification; their numeric thresholds are operator-tunable parameters owned by the guardrail-evaluation library and `rules-and-limits.md`, not this file.

Per `feedback_no_inventing_component_names.md`, every type name, enum member, and field name is sourced from the named design docs. `RegimeLabel` members (`LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`) match `regime-adaptation.md`'s four-regime table literally. `DrawdownTier` members (`NONE`, `CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT`) correspond to the cumulative drawdown progressive response tiers in `breach-behavior.md` (no-response, 8%, 10%, 12% depth).

`parameter_change_flag` in `ActiveRiskParameterSet` requires comparing the current parameter set to the prior invocation's set. The record stores the boolean result; computing it is the snapshot assembler's job (story 06) — the assembler reads the prior-invocation parameter set from the repository (story 04b) and diffs the `value` fields per `rule_id`.

`cumulative_invocation_impact_value` on `RiskBudgetEntry` provides the seam for the guardrail validation tool's cumulative tracking per `state-delivery.md` "Cumulative tracking." The portfolio state record carries the *initial* cumulative state at snapshot time (typically zero); the validation tool accumulates its own per-call delta as the agent reasons within an invocation. Storage of the seam in this record clarifies the contract between the portfolio state snapshot and the guardrail validation tool.

The convenience helpers on `RiskBudgetConsumption` (`entries_by_zone`, `breaching_entries`, `entry_by_rule_id`) are implemented as `@property` methods or plain methods; Pydantic v2 frozen models support both. They do not modify model state and add no new fields to the serialization contract. `entries_by_zone` takes a `RiskZone` argument and is a method, not a property, because it is parameterized.

`DrawdownState.cumulative_tier` is `None` when the cumulative drawdown response has not been triggered. The enum carries only the three tiers (`CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT`); the absence of an active response is the `None` sentinel on the field, avoiding two ways to represent "no response active."

## Acceptance criteria

- [ ] `RegimeLabel` defines exactly `LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`.
- [ ] `RegimeTransitionState` defines exactly `STABLE`, `TIGHTENING`, `LOOSENING`.
- [ ] `RiskZone` defines exactly `NORMAL`, `WARNING`, `CRITICAL`, `BLOCKED`.
- [ ] `DrawdownTier` defines exactly `CONSTRAINED`, `HEAVILY_CONSTRAINED`, `FULL_HALT`.
- [ ] `UnsettledProceedsEntry`, `CashLedger`, `DrawdownState`, `RiskBudgetEntry`, `RiskBudgetConsumption`, `ActiveRiskParameterEntry`, and `ActiveRiskParameterSet` are frozen Pydantic v2 models with the documented field sets.
- [ ] All `*_usd` fields reject non-finite float values.
- [ ] `CashLedger.cash_pct_of_portfolio` outside `[0, 100]` raises `ValidationError`.
- [ ] `DrawdownState.current_drawdown_pct`, `lifetime_max_drawdown_pct`, `intraday_drawdown_pct`, and `drawdown_duration_hours` each reject negative values.
- [ ] `RiskBudgetEntry.headroom == limit_value - current_value` invariant is enforced; violation raises `ValidationError`.
- [ ] `RiskBudgetEntry.headroom_pct_of_limit` outside `[0, 100]` raises `ValidationError`.
- [ ] `RiskBudgetConsumption.entries` with duplicate `rule_id` values raises `ValidationError`.
- [ ] `entries_by_zone(zone)` returns exactly the entries with a matching zone.
- [ ] `breaching_entries()` returns exactly the entries with `zone` in `{CRITICAL, BLOCKED}`.
- [ ] `entry_by_rule_id(rule_id)` returns the matching entry or `None` on a miss.
- [ ] `ActiveRiskParameterSet.transition_state == STABLE` with `transition_invocations_remaining != 0` raises `ValidationError`.
- [ ] `ActiveRiskParameterSet.transition_invocations_remaining < 0` raises `ValidationError`.
- [ ] `ActiveRiskParameterSet.entries` with duplicate `rule_id` values raises `ValidationError`.
- [ ] Tests pass for all validation rules with at least one passing fixture and one failing fixture each.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
