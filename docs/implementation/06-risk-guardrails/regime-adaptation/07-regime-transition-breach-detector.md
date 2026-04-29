---
status: not_started
completed_date:
commit_id:
---

# 07 — Regime-transition breach detector

## Goal

Land the pure function that, after the new effective limits are resolved for the current invocation, scans the held positions and emits one `RegimeTransitionBreach` per (position, breaching rule) pair. Surfaces the deferred-to-strategist breaches the design specifies — the strategist's guardrail state header renders a `Regime-transition breaches (if any):` block from this output, and the strategist proposes thesis-informed remedies (trim, close, hold-with-rationale) the PM then reviews.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Position handling when tightening creates breaches — the deferred classification: regime-transition breaches go to strategist + PM rather than triggering immediate engine action; this story's detector emits the records the strategist consumes
- `docs/design/06-risk-guardrails/state-delivery.md` § Strategist guardrail state header — the `Regime-transition breaches (if any):` block format; this story produces the records the renderer formats
- `docs/design/06-risk-guardrails/state-delivery.md` § PM guardrail state header — the same block also surfaces in the PM header
- `docs/design/04-decision-layer/strategist.md` § Regime-transition remedy proposals — the strategist's downstream contract; the breach records are the trigger
- `docs/design/06-risk-guardrails/breach-behavior.md` § Per-rule breach response classification — the deferred-vs-immediate classification per rule; this story's scope is the *deferred* rules only (sector concentration, directional exposure, gross exposure, options delta, single short max size when ≤ 110%)
- `02-package-skeleton-and-types.md` — `RegimeTransitionBreach` typed record and its invariants
- `src/alphamind/portfolio_state/records/positions.py` — `Position` typed record (the held-positions input shape)
- `src/alphamind/portfolio_state/records/capital.py` — `RiskBudgetEntry`, `RiskBudgetConsumption` (the per-rule current-value snapshot the detector reads)

## Depends on

- 02 (package skeleton + types)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/breach_detector.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_breach_detector.py`.

### 1. Public function

```python
def detect_regime_transition_breaches(
    *,
    held_positions: tuple[Position, ...],
    risk_budget: RiskBudgetConsumption,
    new_effective_limits: Mapping[str, float],
    transition_state: RegimeTransitionState,
    rule_metadata: Mapping[str, RuleMetadata],
) -> tuple[RegimeTransitionBreach, ...]
```

Returns a tuple of `RegimeTransitionBreach` records, sorted ascending by `(rule_id, position_id)`. Empty tuple if no breaches.

### 2. Detection rules

#### 2a. Short-circuit on non-tightening

If `transition_state != RegimeTransitionState.TIGHTENING`, return `()` immediately. Regime-transition breaches arise only from tightening transitions; loosening or stable transitions cannot create new breaches on existing positions (loosening is gradual via interpolation and only relaxes limits; stable means no change).

This is the design's core assertion: regime-transition breaches are a tightening-only phenomenon.

#### 2b. Per-position-rule scan

For each position in `held_positions`, for each rule in the deferred-classification set (see §3 below):

1. Look up the rule's *current* contribution attributable to this position (e.g., the position's `delta_adjusted_exposure_pct_of_portfolio` for `sector_concentration_*`, the position's `size_pct_of_portfolio` for `position_max_size_pct`).
2. Sum across all positions to get the rule's total `current_value` (mirror what `risk_budget.entry_by_rule_id(rule_id)` reports — sanity check via assertion).
3. Look up the *new effective limit* for this rule from `new_effective_limits[rule_id]`.
4. If `current_value > new_effective_limit`, this position contributes to a breach.
5. The per-position breach overage is computed as `position's contribution * (current_value - new_effective_limit) / current_value` — a *proportional attribution* of the total overage to each contributing position. (See §3 for the per-rule attribution semantics.)
6. Emit a `RegimeTransitionBreach(position_id=position.position_id, rule_id=rule_id, rule_label=rule_metadata[rule_id].label, current_value=position_contribution, new_limit_value=position_contribution - position_overage, overage=position_overage, unit=rule_metadata[rule_id].unit)`.

Wait — re-reading: the design's `Regime-transition breaches` block in state-delivery.md shows individual position-level entries:

```
POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%
```

So the breach is attributed to the *position itself*, not split proportionally across all contributors. This makes sense for `position_max_size_pct` (one position, one breach) but is ambiguous for `sector_concentration_pct` (multiple positions contribute to one sector total).

#### 2c. Two breach modes — per-position vs. per-rule

The detector emits two structurally different kinds of breach record, both as `RegimeTransitionBreach`:

**Per-position breaches (rule applies to each position individually):**
- `position_max_size_pct` — each position's `size_pct_of_portfolio` is checked against `new_effective_limits["position_max_size_pct"]` directly.
- `single_short_max_pct` — each short position's `size_pct_of_portfolio` checked against `new_effective_limits["single_short_max_pct"]`.

For these, `current_value` is the position's individual contribution; `new_limit_value` is the rule's effective limit; `overage` is the difference.

**Per-rule aggregate breaches (rule applies to the rule's total current value):**
- `sector_concentration_pct` — the *sector total* (sum of all positions in the sector, delta-adjusted) is checked against `new_effective_limits["sector_concentration_pct"]`. If breaching, every position contributing to that sector is emitted as a separate `RegimeTransitionBreach` with `position_id=position.position_id` and `current_value=position.delta_adjusted_exposure_pct_of_portfolio` (the position's contribution); `new_limit_value` is set to the limit; `overage` is the position's contribution `*` (sector_total - limit) / sector_total — proportional attribution.
- `net_long_pct`, `net_short_pct`, `gross_exposure_pct`, `options_delta_pct` — same shape as sector concentration; each contributing position gets its own breach record with proportional overage attribution.

This dual semantic mirrors the existing design in `state-delivery.md`'s strategist header: per-position lines like `POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5%` show the position's individual size against the rule's effective limit, regardless of whether the rule is per-position or per-rule. The proportional attribution for aggregate rules makes the overage figures consumer-readable (a strategist can see which position contributed how much to the breach).

#### 2d. Deferred-classification rule set

The detector scans only the rules whose breach response is `deferred_to_pm` (per `breach-behavior.md` § Per-rule breach response classification). The full set:

- `position_max_size_pct` — per-position
- `sector_concentration_pct` — per-rule aggregate
- `net_long_pct`, `net_short_pct`, `gross_exposure_pct` — per-rule aggregate
- `options_delta_pct` — per-rule aggregate
- `single_short_max_pct` — per-position (when total short overage ≤ 110% of limit; otherwise the engine acts immediately per breach-behavior.md, but this detector's job is the deferred surface, so it emits the per-position record regardless)

Rules excluded from the detector (immediate-action or non-position-level):
- `daily_drawdown_pct`, `cumulative_drawdown_pct` — drawdown breaches handled by breach-behavior; not regime-transition surface
- `position_max_loss_equity_pct`, `position_max_loss_options_pct` — engine acts immediately on these
- `total_short_pct` — handled per breach-behavior's > 110% rule; not deferred
- `correlation_max`, `thesis_dependency_flag_pct` — T1/T2-only advisory rules, not engine-enforced; the strategist already sees them in headroom
- `borrow_cost_budget_pct_per_day`, `min_cash_reserve_pct`, `pending_order_capital_pct`, `portfolio_theta_*`, `portfolio_vega_*` — not position-level breach surfaces; the strategist sees them via the headroom block

The deferred set is a closed list maintained as a module-level constant `_DEFERRED_RULE_IDS`.

### 3. `RuleMetadata` shape

```python
@dataclass(frozen=True, slots=True)
class RuleMetadata:
    rule_id: str
    label: str          # human-readable, mirrors the registry's rule_label
    unit: str           # mirrors RiskBudgetEntry.unit (e.g., "% of portfolio (delta-adjusted)")
```

The orchestrator (09) constructs the `Mapping[str, RuleMetadata]` from `RuleRegistry` (the rules-and-limits work tree's accessor). This story does not introduce a new metadata source; the typed wrapper exists so the breach detector does not depend on `RuleRegistry` directly (looser coupling).

### 4. Sector-concentration rule-id fanning

The configured `sector_concentration_pct` rule id is per-sector at runtime — the resolver fans `sector_concentration_pct` out to `sector_concentration_tech`, `sector_concentration_semis`, etc., one per active sector. This story's detector reads the per-sector ids from `risk_budget.entries` (which already carry the per-sector ids; the resolver did the fan-out).

For each sector-prefixed rule id in `risk_budget.entries`:
- Extract the sector key from the suffix (`sector_concentration_tech` → `tech`).
- Look up `new_effective_limits[rule_id]` for the same per-sector id.
- Compare per the per-rule-aggregate logic above.
- Filter held positions to those whose `sector` matches the rule's suffix when emitting per-position breach records.

### 5. Short-circuit on missing data

- If `held_positions` is empty, return `()` immediately (no positions = no breaches).
- If `new_effective_limits` is missing a rule_id present in `risk_budget.entries`, raise `ValueError` with the missing rule_id — surfaces resolver-output drift.
- If `rule_metadata` is missing a rule_id from `_DEFERRED_RULE_IDS` that the detector needs, raise `ValueError`.
- If `risk_budget.entries` is missing a rule_id from `_DEFERRED_RULE_IDS`, the detector skips the rule (the rule is not in scope for this profile / feature flags closed it).

### 6. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_breach_detector.py`:

- **No tightening transition returns empty.** `transition_state=STABLE` with positions over their limits returns `()`. `transition_state=LOOSENING` with positions over their limits returns `()`.
- **No positions returns empty.** `held_positions=()` with `transition_state=TIGHTENING` returns `()`.
- **No breaches returns empty.** Positions all under their new effective limits, `transition_state=TIGHTENING` returns `()`.
- **Per-position breach (`position_max_size_pct`):** one position at 4.5% with new effective limit 3.5%. Returns one breach: `position_id=POS-...`, `rule_id="position_max_size_pct"`, `current_value=4.5`, `new_limit_value=3.5`, `overage=1.0`.
- **Per-position breach across multiple positions:** three positions at 4.0%, 5.0%, 3.0% with new effective limit 3.5%. Returns two breaches (the 4.0 and 5.0 positions); the 3.0 position is not breaching.
- **Per-rule aggregate breach (`sector_concentration_tech`):** sector tech total at 28% with new effective limit 20%; three tech positions contributing 12%, 10%, 6%. Returns three breaches (one per contributing position) with proportional overage: position@12% gets `overage = 12 * (28-20)/28 = 3.43`; position@10% gets `overage = 10 * 8/28 = 2.86`; position@6% gets `overage = 6 * 8/28 = 1.71`. Sum of overages ≈ 8.0 (matches the total sector overage).
- **Multiple sectors with breaches:** sector tech at 28%/20% and sector semis at 18%/15%; both breaching. Returns breaches for tech positions and semis positions.
- **Per-rule aggregate breach for `net_long_pct`:** net_long total at 65% with new effective limit 45%; positions contributing to net_long get proportional breach records.
- **`gross_exposure_pct` aggregate breach:** same shape.
- **`options_delta_pct` aggregate breach:** options positions contributing to delta-adjusted exposure get proportional breach records.
- **`single_short_max_pct` per-position breach:** short positions individually checked against the rule.
- **`single_short_max_pct` does not flag long positions:** positions with `direction != short` are skipped by the per-position scan for this rule.
- **Sorted output:** result tuple is sorted ascending by `(rule_id, position_id)`. Verified by inspecting the tuple ordering for a fixture with multiple rules and positions.
- **Drawdown rules excluded:** position with high P/L loss does not produce a `daily_drawdown_pct` or `cumulative_drawdown_pct` breach record.
- **Position-loss rules excluded:** position with `unrealized_loss_pct=35%` (above 30% equity backstop) does not produce a `position_max_loss_equity_pct` breach record (this rule is the engine's immediate domain, not the deferred surface).
- **Correlation and thesis-dependency rules excluded:** these T1/T2-only rules are not in the deferred set.
- **Missing rule in `new_effective_limits` raises `ValueError`** with the rule_id in the message.
- **Missing rule in `rule_metadata` raises `ValueError`.**
- **Missing rule in `risk_budget.entries` is silently skipped** (e.g., `options_delta_pct` is closed under `options_enabled: false` — the detector does not flag this as an error; the rule is simply not in scope for this profile).
- **`RegimeTransitionBreach` invariants hold for every emitted record:** `overage > 0`, `overage` close to `current_value - new_limit_value` (verified via the typed-record validator).
- **Determinism / purity:** identical inputs produce identical outputs; the input collections are not mutated.

Out of scope:

- Drawdown breach detection — handled by `breach-behavior` and the continuous monitor, not regime-adaptation.
- Position-level max-loss detection — same; engine immediate-action surface.
- Total-short cascade detection (the > 110% rule) — `breach-behavior`'s domain.
- Suggesting *remedies* (trim quantity, close position, hold rationale) — that is the strategist's job per `strategist.md` § Regime-transition remedy proposals; the breach records are the input to that decision, not the decision itself.
- Persisting the breach records — they are returned as typed records and consumed by the orchestrator (09), which surfaces them in `RegimeAdaptationOutput`. The state-delivery work tree's renderer will read them from the orchestrator output.

## Notes

The dual breach mode (per-position vs. per-rule aggregate) reflects how the strategist's state header consumes the records: every breach line shows a position id, a rule, and an overage. For per-position rules (`position_max_size_pct`), the mapping is direct. For per-rule aggregate rules (`sector_concentration_*`), the breach total is split proportionally across contributing positions so the strategist can see which positions to consider trimming first.

The proportional-attribution arithmetic preserves the invariant `sum(per-position overages) == total rule overage` for aggregate rules. A test asserts this within `1e-9` tolerance for at least one fixture.

Per `feedback_no_inventing_component_names.md`, the function name `detect_regime_transition_breaches` mirrors the design's "regime-transition breaches" terminology. The `RuleMetadata` typed record exists because the breach detector should not depend on `RuleRegistry` directly (looser coupling), and using `RuleEntry` from the configuration package would import the full Pydantic surface unnecessarily.

Per `feedback_simplify_before_building.md`, the detector emits breach records as a flat tuple. It does not group them by rule, by sector, or by position. The consumer (state-delivery's renderer) handles presentation grouping. If the consumer later wants pre-grouped output, that's a renderer concern.

Per `feedback_avoid_numeric_anchors.md`, no numeric thresholds in the detector. The deferred-rule set `_DEFERRED_RULE_IDS` is a closed enumeration documented against `breach-behavior.md`'s per-rule classification table; not a tunable.

Per `feedback_per_producer_schema.md`, the detector emits one record per (position, rule) breach. No discriminated union; one shape per record. The `RegimeTransitionBreach` invariants (overage > 0, overage matches current - limit) are checked at construction time.

The "tightening transition only" short-circuit catches a subtle invariant: a `LOOSENING` transition could in principle arrive at a regime whose multipliers are *tighter* than the prior interpolated values (e.g., loosening from `crisis` to `elevated`, but the `elevated` regime's `min_cash_reserve_pct` multiplier is 1.5 vs. crisis's 2.5 — that's a *loosening* of the cash-reserve constraint, fewer dollars required). The detector trusts the orchestrator's `transition_state` decision (TIGHTENING / LOOSENING / STABLE) and does not re-derive the per-rule direction. If the orchestrator's classification is wrong, that's a state-machine bug surfaced by stories 04b and 09.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/breach_detector.py` exists and defines `detect_regime_transition_breaches`, `RuleMetadata`, `_DEFERRED_RULE_IDS`.
- [ ] `detect_regime_transition_breaches`, `RuleMetadata` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] `transition_state != TIGHTENING` returns `()` regardless of position state.
- [ ] Empty `held_positions` returns `()`.
- [ ] No breaching positions return `()`.
- [ ] Per-position rule (`position_max_size_pct`) emits one breach per breaching position with direct (current - limit) overage.
- [ ] Per-rule aggregate rule (`sector_concentration_*`) emits one breach per contributing position with proportional overage attribution; sum of attributions equals total overage within `1e-9`.
- [ ] All sector-prefixed rules are scanned per the runtime-fanned `risk_budget.entries`.
- [ ] `single_short_max_pct` flags only short positions.
- [ ] Drawdown, position-loss, correlation, thesis-dependency, and the rules in the excluded list are not in `_DEFERRED_RULE_IDS` and produce no breach records.
- [ ] Output tuple is sorted ascending by `(rule_id, position_id)`.
- [ ] Missing rule in `new_effective_limits` or `rule_metadata` raises `ValueError`.
- [ ] Missing rule in `risk_budget.entries` is silently skipped (feature-flag closure).
- [ ] Every emitted `RegimeTransitionBreach` satisfies the typed-record invariants (`overage > 0`, `overage ≈ current_value - new_limit_value`).
- [ ] The function is pure: equal inputs produce equal outputs; the input collections are not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
