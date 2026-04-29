---
status: in_progress
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

#### 2b. Two emission modes by rule kind

Each deferred rule in `_DEFERRED_RULE_IDS` is classified at the registry level (see §2c) as either *per-position* or *aggregate*. The detector emits records in two distinct shapes accordingly.

**Per-position rules (one record per breaching position).** The rule's limit applies to a per-position contribution; multiple positions can each independently breach. For each position in `held_positions`:

1. Compute the position's contribution to the rule (e.g., for `position_max_size_pct`, the position's `size_pct_of_portfolio`; for `single_short_max_pct`, the same when `direction == short`, skip otherwise).
2. Compare against `new_effective_limits[rule_id]`.
3. If `position_contribution > new_effective_limit`: emit `RegimeTransitionBreach(position_id=position.position_id, rule_id=rule_id, rule_label=rule_metadata[rule_id].label, current_value=position_contribution, new_limit_value=new_effective_limit, overage=position_contribution - new_effective_limit, unit=rule_metadata[rule_id].unit)`.

**Aggregate rules (one record per breaching rule).** The rule's limit applies to a portfolio-level aggregate (sum of contributions); a single breach is the aggregate exceeding the limit, regardless of how many positions contribute. For each aggregate rule:

1. Read the rule's current aggregate from `risk_budget.entry_by_rule_id(rule_id).current` (e.g., for `sector_concentration_tech`, the tech sector's total delta-adjusted exposure).
2. Compare against `new_effective_limits[rule_id]`.
3. If `aggregate_value > new_effective_limit`: emit a single `RegimeTransitionBreach(position_id=None, rule_id=rule_id, rule_label=rule_metadata[rule_id].label, current_value=aggregate_value, new_limit_value=new_effective_limit, overage=aggregate_value - new_effective_limit, unit=rule_metadata[rule_id].unit)`.

The strategist's state header consumes both shapes uniformly. For aggregate breaches, the strategist consults the `Sector exposure breakdown (per position)` block in the same header to identify which positions contribute and plan trims. The detector does not invent per-position attribution for aggregate breaches — the source data (per-position contributions) is already in the strategist's bundle via the breakdown block; duplicating it inside the breach record would inflate the breach count and synthesize arbitrary per-position overage values.

#### 2c. Deferred-classification rule set

The detector scans only the rules whose breach response is `deferred_to_pm` (per `breach-behavior.md` § Per-rule breach response classification). Each rule is classified as **per-position** or **aggregate** for the §2b emission dispatch.

| Rule ID | Emission kind |
|---|---|
| `position_max_size_pct` | per-position |
| `single_short_max_pct` | per-position (long positions skipped; emits regardless of `total_short_pct`'s 110%-of-limit boundary, since this detector covers the deferred surface only) |
| `sector_concentration_*` (per active sector) | aggregate |
| `net_long_pct` | aggregate |
| `net_short_pct` | aggregate |
| `gross_exposure_pct` | aggregate |
| `options_delta_pct` | aggregate |

Rules excluded from the detector (immediate-action or non-position-level):
- `daily_drawdown_pct`, `cumulative_drawdown_pct` — drawdown breaches handled by breach-behavior; not regime-transition surface
- `position_max_loss_equity_pct`, `position_max_loss_options_pct` — engine acts immediately on these
- `total_short_pct` — handled per breach-behavior's > 110% rule; not deferred
- `correlation_max`, `thesis_dependency_flag_pct` — T1/T2-only advisory rules, not engine-enforced; the strategist already sees them in headroom
- `borrow_cost_budget_pct_per_day`, `min_cash_reserve_pct`, `pending_order_capital_pct`, `portfolio_theta_*`, `portfolio_vega_*` — not position-level breach surfaces; the strategist sees them via the headroom block

The deferred set is a closed list maintained as a module-level constant `_DEFERRED_RULE_IDS`. The per-position vs. aggregate classification is encoded as a parallel `_RULE_EMISSION_KIND: Mapping[str, Literal["per_position", "aggregate"]]` constant; the detector dispatches on this mapping to choose the §2b shape.

### 3. `RuleMetadata` import

`RuleMetadata` is the canonical typed record declared in `02-package-skeleton-and-types.md` § 2j. Imported here:

```python
from alphamind.risk_guardrails.regime_adaptation.types import RuleMetadata
```

Canonical fields: `rule_id: str`, `label: str`, `unit: str`.

The orchestrator (story 09) constructs the `Mapping[str, RuleMetadata]` from `RuleRegistry` (the rules-and-limits work tree's accessor). The typed wrapper exists so the breach detector does not depend on `RuleRegistry` directly (looser coupling).

### 4. Sector-concentration rule-id fanning

The configured `sector_concentration_pct` rule id is per-sector at runtime — the resolver fans `sector_concentration_pct` out to `sector_concentration_tech`, `sector_concentration_semis`, etc., one per active sector. The detector reads the per-sector ids from `risk_budget.entries` (which already carry the per-sector ids; the resolver did the fan-out).

For each sector-prefixed rule id in `risk_budget.entries`:
- Read the sector total from `risk_budget.entry_by_rule_id(rule_id).current`.
- Look up `new_effective_limits[rule_id]` for the same per-sector id.
- Apply the aggregate-rule emission rule from §2b: if total > limit, emit one `RegimeTransitionBreach(position_id=None, rule_id=<sector-prefixed id>, ...)`.

The detector treats each sector-prefixed rule id independently — a portfolio with breached tech and breached semis emits two records (one per sector). This matches `_RULE_EMISSION_KIND[sector_concentration_*] == "aggregate"`.

### 5. Short-circuit on missing data

- If `held_positions` is empty, return `()` immediately (no positions = no breaches).
- If `new_effective_limits` is missing a rule_id present in `risk_budget.entries`, raise `ValueError` with the missing rule_id — surfaces resolver-output drift.
- If `rule_metadata` is missing a rule_id from `_DEFERRED_RULE_IDS` that the detector needs, raise `ValueError`.
- If `risk_budget.entries` is missing a rule_id from `_DEFERRED_RULE_IDS`, the detector skips the rule (the rule is not in scope for this profile / feature flags closed it).

### 6. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_breach_detector.py`:

- **No tightening transition returns empty.** `transition_state=STABLE` with positions over their limits returns `()`. `transition_state=LOOSENING` with positions over their limits returns `()`.
- **No positions returns empty.** `held_positions=()` with `transition_state=TIGHTENING` returns `()`.
- **No breaches returns empty.** Positions all under their new effective limits AND aggregates within their new limits, `transition_state=TIGHTENING` returns `()`.
- **Per-position breach (`position_max_size_pct`):** one position at 4.5% with new effective limit 3.5%. Returns one breach: `position_id="POS-..."`, `rule_id="position_max_size_pct"`, `current_value=4.5`, `new_limit_value=3.5`, `overage=1.0`.
- **Per-position breach across multiple positions:** three positions at 4.0%, 5.0%, 3.0% with new effective limit 3.5%. Returns two breaches (the 4.0 and 5.0 positions, each with its own `position_id`); the 3.0 position is not breaching.
- **Aggregate breach (`sector_concentration_tech`):** sector tech total at 28% with new effective limit 20%; three tech positions contributing 12%, 10%, 6%. Returns **one** breach: `position_id=None`, `rule_id="sector_concentration_tech"`, `current_value=28.0`, `new_limit_value=20.0`, `overage=8.0`. The per-position contributions are NOT in this record — the strategist reads them from the sector breakdown block.
- **Multiple sectors with breaches:** sector tech at 28%/20% and sector semis at 18%/15%; both breaching. Returns two breaches (one per sector), both with `position_id=None`.
- **Aggregate breach for `net_long_pct`:** net_long aggregate at 65% with new effective limit 45%. Returns one breach: `position_id=None`, `rule_id="net_long_pct"`, `current_value=65.0`, `new_limit_value=45.0`, `overage=20.0`.
- **`gross_exposure_pct` aggregate breach:** same shape — one record, `position_id=None`.
- **`options_delta_pct` aggregate breach:** same shape — one record, `position_id=None`.
- **`single_short_max_pct` per-position breach:** short positions individually checked against the rule; one record per breaching short position with `position_id` set.
- **`single_short_max_pct` does not flag long positions:** positions with `direction != short` are skipped by the per-position scan for this rule.
- **Sorted output:** result tuple is sorted ascending by `(rule_id, position_id_or_empty_string)` — aggregate records (`position_id=None`) sort before per-position records sharing the same `rule_id`. Verified by inspecting the tuple ordering for a fixture with multiple rules and positions.
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

The dual breach mode (per-position vs. aggregate) reflects how the strategist's state header consumes the records and how the rules are themselves structured. For per-position rules (`position_max_size_pct`, `single_short_max_pct`), each position is independently checked against the rule's per-position limit; a breach is intrinsically tied to a specific position, and the detector emits one record per breaching position. For aggregate rules (`sector_concentration_*`, `net_long_pct`, `net_short_pct`, `gross_exposure_pct`, `options_delta_pct`), a single portfolio-level value is checked against the limit; a breach is the aggregate exceeding the limit, regardless of which positions contribute. The detector emits exactly one record per breaching aggregate rule, with `position_id=None` and `current_value` set to the aggregate. Per-position contributions are NOT split into per-position breach records — the strategist already has the per-position breakdown in its state header (the `Sector exposure breakdown (per position)` block) and uses it to plan trims.

Per `feedback_no_inventing_component_names.md`, the function name `detect_regime_transition_breaches` mirrors the design's "regime-transition breaches" terminology. The `RuleMetadata` typed record exists because the breach detector should not depend on `RuleRegistry` directly (looser coupling), and using `RuleEntry` from the configuration package would import the full Pydantic surface unnecessarily.

Per `feedback_simplify_before_building.md`, the detector emits breach records as a flat tuple. It does not group them by rule, by sector, or by position. The consumer (state-delivery's renderer) handles presentation grouping. If the consumer later wants pre-grouped output, that's a renderer concern. The detector also does not invent per-position attribution for aggregate breaches — synthesizing proportional overage values across contributing positions would inflate the breach count and fabricate arithmetic that does not appear in the design.

Per `feedback_avoid_numeric_anchors.md`, no numeric thresholds in the detector. The deferred-rule set `_DEFERRED_RULE_IDS` is a closed enumeration documented against `breach-behavior.md`'s per-rule classification table; not a tunable. The per-rule emission classification `_RULE_EMISSION_KIND` is similarly closed.

Per `feedback_per_producer_schema.md`, the detector emits one record per breach (per breaching position for per-position rules, per breaching rule for aggregate rules). One shape per record; no discriminated union. The `RegimeTransitionBreach` invariants (`overage > 0`, `overage == current_value - new_limit_value`, `position_id` optional) are checked at construction time.

The "tightening transition only" short-circuit catches a subtle invariant: a `LOOSENING` transition could in principle arrive at a regime whose multipliers are *tighter* than the prior interpolated values (e.g., loosening from `crisis` to `elevated`, but the `elevated` regime's `min_cash_reserve_pct` multiplier is 1.5 vs. crisis's 2.5 — that's a *loosening* of the cash-reserve constraint, fewer dollars required). The detector trusts the orchestrator's `transition_state` decision (TIGHTENING / LOOSENING / STABLE) and does not re-derive the per-rule direction. If the orchestrator's classification is wrong, that's a state-machine bug surfaced by stories 04b and 09.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/breach_detector.py` exists and defines `detect_regime_transition_breaches`, `_DEFERRED_RULE_IDS`, `_RULE_EMISSION_KIND`. `RuleMetadata` is imported from `alphamind.risk_guardrails.regime_adaptation.types` (canonical declaration in story 02); not redeclared here.
- [ ] `detect_regime_transition_breaches` is re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`. (`RuleMetadata` is already re-exported via story 02.)
- [ ] `transition_state != TIGHTENING` returns `()` regardless of position state.
- [ ] Empty `held_positions` returns `()`.
- [ ] No breaches return `()`.
- [ ] Per-position rule (`position_max_size_pct`) emits one record per breaching position with `position_id` set, `current_value=position_contribution`, `overage=position_contribution - new_limit_value`.
- [ ] Aggregate rule (`sector_concentration_*`) emits exactly ONE record per breaching rule with `position_id=None`, `current_value=aggregate_value`, `overage=aggregate_value - new_limit_value`. No per-position split.
- [ ] All sector-prefixed rules are scanned per the runtime-fanned `risk_budget.entries`; each breaching sector emits one record (multiple sectors → multiple records).
- [ ] `net_long_pct`, `net_short_pct`, `gross_exposure_pct`, `options_delta_pct` aggregate breaches each emit a single record with `position_id=None`.
- [ ] `single_short_max_pct` flags only short positions; emits per-position records.
- [ ] Drawdown, position-loss, correlation, thesis-dependency, and the rules in the excluded list are not in `_DEFERRED_RULE_IDS` and produce no breach records.
- [ ] Output tuple is sorted ascending by `(rule_id, position_id_or_empty_string)`; aggregate records sort before per-position records sharing the same `rule_id`.
- [ ] Missing rule in `new_effective_limits` or `rule_metadata` raises `ValueError`.
- [ ] Missing rule in `risk_budget.entries` is silently skipped (feature-flag closure).
- [ ] Every emitted `RegimeTransitionBreach` satisfies the typed-record invariants (`overage > 0`, `overage ≈ current_value - new_limit_value`); `position_id` is `None` for aggregate-kind rules and a non-empty string for per-position-kind rules.
- [ ] The function is pure: equal inputs produce equal outputs; the input collections are not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
