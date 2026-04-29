---
status: not_started
completed_date:
commit_id:
---

# 04e — Regime-transition breach detection

## Goal

Land the deterministic primitive that detects positions breaching newly-tightened limits when a regime transition activates a tighter parameter set. Produces a tuple of `RegimeTransitionBreach` records — one per (position, breached_rule) — that state-delivery's strategist and PM headers render and the strategist's remedy-proposal logic consumes. Per `breach-behavior.md` and `regime-adaptation.md`, regime-transition breaches on existing positions are deferred to the strategist → PM (no engine action); this story produces the typed surface that flows through that deferral path.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Position handling when tightening creates breaches — the authoritative deferral classification and the strategist/PM remedy responsibility:
  > Regime-transition breaches on existing positions are deferred to the strategist and PM at the next invocation. The strategist proposes remedies; the PM reviews and executes.
- `docs/design/06-risk-guardrails/state-delivery.md` § Strategist guardrail state header — the `Regime-transition breaches (if any):` block format:
  ```
  POS-NVDA-001: 4.2% exceeds elevated regime limit of 3.5% — overage 0.7%
  ```
- `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio manager guardrail state header — the same block surfaces in the PM header.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Forced reduction policy — Per-rule breach response classification — confirms regime-transition breaches inherit the deferred response of the underlying rule (sector concentration, directional exposure, gross exposure, options delta), not the rule's own engine-immediate classification (which applies only to market-movement breaches on those rules where the immediate action exists, e.g., position-level max loss). The detection here is rule-agnostic — it identifies the breach; the response classification is a separate concern documented in story 06 (engine envelope assembler) and the continuous monitor.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Drawdown halt mode — regime-tightening exception: if the regime transition coincides with a drawdown breach, the drawdown response takes precedence (engine handles drawdown immediately; regime-transition exposure breaches still defer to the next invocation's strategist/PM). This story's primitive does not enforce that ordering; it produces the breach records and lets the orchestrator handle precedence.
- `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, `Direction`.
- `src/alphamind/portfolio_state/records/capital.py` — `ActiveRiskParameterSet`, `ActiveRiskParameterEntry`, `RegimeLabel`.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `RegimeTransitionBreach`, `ActiveRiskParameterSet` (re-export).
- `docs/implementation/06-risk-guardrails/state-delivery/04b-strategist-header-renderer.md` and `04c-pm-header-renderer.md` — sibling stories that consume `tuple[RegimeTransitionBreach, ...]` as a typed input. Their tests use hand-constructed fixtures; this story produces the production detector.
- `docs/design/06-risk-guardrails/scenario-tests.md` § A3 (regime transition normal → elevated) — the canonical worked example:
  > Tech sector at 24%, just under 25% normal; new elevated limit 20% → 4% overage. Two positions at 4.5% each vs new 3.5% per-position limit → 1% overage each. Net long 55% vs new 45% limit → 10% overage. Gross 105% vs new 90% limit → 15% overage.

## Depends on

- 02 (package skeleton)
- 03 (canonical types — `RegimeTransitionBreach`, `ActiveRiskParameterSet`, `PositionRecord`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/regime_transition.py`. Tests at `tests/risk_guardrails/breach_behavior/test_regime_transition.py`.

### 1. Public function

```python
def detect_regime_transition_breaches(
    *,
    open_positions: tuple[PositionRecord, ...],
    sector_resolver: Callable[[PositionRecord], str | None],
    sector_exposure_pct: dict[str, float],          # {sector: current_pct_of_portfolio}
    net_long_pct: float,
    net_short_pct: float,
    gross_exposure_pct: float,
    options_delta_pct: float,                       # 0.0 when options disabled
    prior_active_risk_parameters: ActiveRiskParameterSet,
    current_active_risk_parameters: ActiveRiskParameterSet,
) -> tuple[RegimeTransitionBreach, ...]:
    """Identify positions breaching newly-tightened limits after a regime transition.

    Compares per-position and portfolio-level exposure against current_active_risk_parameters
    and emits a RegimeTransitionBreach record for each violation that did NOT exist under
    prior_active_risk_parameters. The "did not exist before" check distinguishes regime-driven
    breaches from pre-existing breaches that have other causes — only the regime transition
    is the intended breach surface for this primitive.

    Detection covers six rule types:
      - position_max_size_pct (per-position): one breach per position above the new per-position cap
      - sector_concentration_pct (per active sector): one breach per sector above the new sector cap
      - net_long_pct (portfolio): one breach if net long exceeds the new long cap
      - net_short_pct (portfolio): one breach if net short exceeds the new short cap
      - gross_exposure_pct (portfolio): one breach if gross exceeds the new gross cap
      - options_delta_pct (portfolio): one breach if options delta exceeds the new options-delta cap

    Each breach record has its own `position_id` field. For per-position rules
    (position_max_size_pct), it is the breaching position's id. For portfolio-level rules
    (sector concentration, directional exposure, gross, options delta), `position_id` is set
    to a synthetic prefix-based id `"PORTFOLIO-{rule_id}"` because the breach is portfolio-wide
    rather than tied to one position; the strategist's remedy logic resolves which positions
    contribute and proposes targeted closes/trims.

    Args:
        open_positions: All currently-open positions.
        sector_resolver: Function mapping a position to its sector key (None for unmapped).
        sector_exposure_pct: Current per-sector exposure as % of portfolio (delta-adjusted).
        net_long_pct, net_short_pct, gross_exposure_pct: Current portfolio-level exposures.
        options_delta_pct: Current portfolio-level options delta exposure.
        prior_active_risk_parameters: The pre-transition parameter set (used to determine which
            breaches are NEW under the transition).
        current_active_risk_parameters: The post-transition parameter set.

    Returns:
        A tuple of RegimeTransitionBreach records, one per detected breach. Order:
          1. Per-position breaches first, sorted by position_id ascending.
          2. Portfolio-level breaches second, in the canonical rule order:
             sector_concentration_{sector_key_ascending}, net_long_pct, net_short_pct,
             gross_exposure_pct, options_delta_pct.
        Empty tuple when no breaches are introduced by the transition.

    Raises:
        ValueError: when current_active_risk_parameters lacks a required rule entry, or when
            sector_resolver returns a key not in sector_exposure_pct.
    """
```

### 2. New-vs-pre-existing breach discrimination

The primitive emits a `RegimeTransitionBreach` only when the breach is *new under the transition* — i.e., the `current_value` exceeds the *current* limit but did NOT exceed the *prior* limit. This filtering distinguishes two scenarios:

- **Scenario A (the design's target):** Regime tightens normal → elevated. Position at 4.5% was compliant under normal (5% cap) but breaches under elevated (3.5% cap). Emit the breach.
- **Scenario B (already-breaching pre-transition):** Position at 5.5% was *already breaching* normal's 5% cap (the regime was running under a separate breach response — engine action or PM deferral); after transition to elevated (3.5% cap), the position is still breaching but the breach is not regime-driven. Do NOT re-emit; the existing breach response handles it.

Implementation sketch:
1. For each rule, look up the prior limit (from `prior_active_risk_parameters`) and current limit (from `current_active_risk_parameters`).
2. Skip the rule if the current limit is ≥ prior limit (no tightening; loosening cannot create a regime-transition breach).
3. For each subject (per-position or per-sector or portfolio-level), compute `current_value`.
4. If `current_value > current_limit` AND `current_value ≤ prior_limit`, emit the breach.
5. If `current_value > current_limit` AND `current_value > prior_limit`, the breach pre-existed; skip.

Boundary semantics: equality is non-breaching for both prior and current (consumption exactly at limit is BLOCKED per the zone classifier, but for this primitive's purposes "breach" means strictly above the limit because the regime-transition narrative is "tightening shifts a position past the line"). The 0.001%-margin-of-error edge case for floating point comparison: tolerate `math.isclose(current_value, current_limit, abs_tol=1e-9)` as non-breaching.

### 3. Per-rule limit lookup

The function accepts the full `ActiveRiskParameterSet` as input rather than individual rule values because:
1. Several rules need lookups (six in scope).
2. The `regime_label` field on the parameter set populates `RegimeTransitionBreach.regime_label` directly (no parameter passing required).
3. The per-rule entry's `unit` field populates `RegimeTransitionBreach.unit` directly (avoids hardcoding unit strings in this module).

Helper:
```python
def _limit_for_rule(parameters: ActiveRiskParameterSet, rule_id: str) -> float:
    for entry in parameters.entries:
        if entry.rule_id == rule_id:
            return entry.value
    msg = f"rule_id {rule_id!r} missing from ActiveRiskParameterSet entries"
    raise ValueError(msg)


def _unit_for_rule(parameters: ActiveRiskParameterSet, rule_id: str) -> str:
    for entry in parameters.entries:
        if entry.rule_id == rule_id:
            return entry.unit
    msg = f"rule_id {rule_id!r} missing from ActiveRiskParameterSet entries"
    raise ValueError(msg)
```

### 4. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_regime_transition.py`:

#### Per-position breaches

- **Scenario A — new per-position breach:** prior `position_max_size_pct=5.0`, current `position_max_size_pct=3.5` (elevated regime); two positions at 4.5% each. Output has two `RegimeTransitionBreach` records with `rule_breached="position_max_size_pct"`, `current_value=4.5`, `new_regime_limit=3.5`, `overage=1.0`, `regime_label=ELEVATED`, sorted by `position_id` ascending.
- **Scenario B — pre-existing per-position breach NOT re-emitted:** prior `position_max_size_pct=5.0`, current `position_max_size_pct=3.5`; one position at 5.5% (breached prior; still breaches current). Output is empty (or excludes that position).
- **Mixed prior-and-new:** three positions at 5.5%, 4.5%, 3.0%. Prior limit 5.0%, current limit 3.5%. Output has one breach (the 4.5% — the 5.5% pre-existed, the 3.0% is compliant).

#### Per-sector breaches

- **New sector concentration breach:** prior `sector_concentration_pct=25.0`, current `sector_concentration_pct=20.0` (elevated); tech sector at 24% (was compliant prior, breaches current). Output has one breach with `rule_breached="sector_concentration_pct"`, `position_id="PORTFOLIO-sector_concentration_tech"` (or similar canonical synthetic id), `current_value=24.0`, `new_regime_limit=20.0`, `overage=4.0`.
- **Multiple sectors breaching:** tech 24%, semis 22%; both above current 20% cap; both compliant under prior 25% cap. Output has two breaches in alphabetical sector order (semis first, then tech if alphabetic).

#### Portfolio-level breaches

- **Net long breach:** prior `net_long_pct=60.0`, current `net_long_pct=45.0` (elevated); current net long 55% (compliant prior; breaches current). One breach with `rule_breached="net_long_pct"`, `position_id="PORTFOLIO-net_long_pct"`, `overage=10.0`.
- **Gross breach:** prior 120%, current 90%; current gross 105%. One breach with `overage=15.0`.
- **Options delta breach:** prior `options_delta_pct=40.0`, current `options_delta_pct=30.0`; current options delta 38%. One breach.
- **Options delta when options disabled:** when `options_delta_pct=0.0` and the rule is absent from `current_active_risk_parameters` (options disabled in profile), no breach is emitted; the function does not raise.

#### Mixed scenario A3 worked example

- **Full A3 reproduction:** input matches the design's A3 walkthrough (tech at 24%, two positions at 4.5%, net long 55%, gross 105%, options enabled but no options exposure). Output has 5 breaches: position1 max_size, position2 max_size, sector_concentration_tech, net_long_pct, gross_exposure_pct. Order: per-position first (position_id ascending), then portfolio-level in the canonical rule order. Each `overage` matches the documented value.

#### No-transition cases

- **Loosening transition produces no breaches:** prior `position_max_size_pct=3.5`, current `position_max_size_pct=5.0`; positions at 4.5% (compliant under both). Output empty.
- **Same regime — no transition — no breaches:** prior and current parameters identical; output empty regardless of position state.
- **No positions:** empty `open_positions` produces empty output (only portfolio-level rules might still breach if their thresholds hit zero, but with empty positions all aggregates are zero and no breach can be regime-introduced).

#### Validation

- **Missing rule entry raises:** `current_active_risk_parameters` lacks `position_max_size_pct` → `ValueError`.
- **Sector resolver mismatch raises:** `sector_resolver` returns "biotech" but `sector_exposure_pct` does not include "biotech" → `ValueError`.
- **Determinism:** repeated calls with identical inputs produce equal outputs.
- **Frozen output records:** assigning to a returned `RegimeTransitionBreach` field raises `ValidationError`.

Out of scope:
- The strategist's remedy-proposal logic (e.g., "trim the weakest-thesis tech position") — that's the strategist agent's domain (see `decision/strategist.md`).
- The PM's cross-constraint validation of remedies — PM's domain.
- The drawdown-precedence rule when both drawdown and regime transition fire simultaneously — that's orchestrator-side ordering, not a detection concern.
- Detecting *which* regime transition occurred (e.g., low-vol → crisis vs. normal → elevated) — that's regime-adaptation's domain. This primitive consumes the resolved parameter sets, not the transition reasoning.
- Computing the prior parameter set — the caller (continuous monitor or scheduled invocation entry point) tracks the prior set across invocations.
- Stress-overlay or pre-event-overlay-driven tightening — those are also handled by regime-adaptation; if they manifest as parameter-set changes between invocations, this primitive would emit breach records the same way. Out of scope here means: the trigger reasoning is regime_adaptation; this primitive operates on parameter-set deltas regardless of trigger.

## Notes

**Why detect new-vs-pre-existing rather than emit all breaches.** The strategist's remedy-proposal flow is keyed to *changes* in the breach surface. A position that has been over-limit for two invocations under the same regime is the engine's or the PM's prior-decision territory; surfacing it again as a regime-transition breach would mislead the strategist into proposing a remedy for a breach that already has a separate response in flight. The discrimination keeps state-delivery's `Regime-transition breaches (if any):` block focused on the transition's actual impact.

**Why a synthetic `PORTFOLIO-{rule_id}` position_id rather than omitting position_id.** The `RegimeTransitionBreach` model (story 03) declares `position_id: str` as required. State-delivery's strategist header renders the breach line per-record. For per-position rules, the position_id is the actual position. For portfolio-level rules, the breach is not tied to a single position — but the renderer still needs a stable identifier for layout purposes. The synthetic id gives the renderer a placeholder string the strategist's prompt machinery can treat as "portfolio-level" (e.g., by stripping the prefix and substituting prose). An alternative would be to add a separate `is_portfolio_level: bool` field to the model; the synthetic-id approach is lighter-weight and maintains schema simplicity.

**Per `feedback_simplify_before_building.md`,** detection is one function over a fixed rule set (six rules in scope per the design). No plugin architecture, no per-rule registry, no detector-class hierarchy — just one function with explicit per-rule branches that read like the design table.

**Per `feedback_avoid_numeric_anchors.md`,** all limits come from `ActiveRiskParameterSet` entries; the function does not embed any threshold percentages (5%, 25%, 60%, etc.) — they are read from the resolved parameter set.

**Per `feedback_no_inventing_component_names.md`,** the function name `detect_regime_transition_breaches` matches the design's "regime-transition breach" terminology. Rule IDs come from `guardrails.yaml` (canonical names already in `RuleEntry.id`).

**Cross-feature dependency.** This primitive's caller is the continuous monitor or the invocation orchestrator that tracks the prior parameter set across invocations. The `prior_active_risk_parameters` is typically the prior invocation's state — sourced from the activity log or from a checkpoint maintained by the scheduler. Until those are wired (execution layer's state-persistence work tree), the test suite uses hand-constructed prior/current parameter sets directly. Production wiring is a follow-up integration story in the execution-layer work tree.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/regime_transition.py` exists and defines `detect_regime_transition_breaches` with the documented signature.
- [ ] `detect_regime_transition_breaches` is re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] The function is pure — no I/O, no global state, no clock reads.
- [ ] Per-position scenario: two positions at 4.5%, prior limit 5.0%, current limit 3.5% → two breaches with `current_value=4.5`, `new_regime_limit=3.5`, `overage=1.0`.
- [ ] Pre-existing breach not re-emitted: position at 5.5% with prior limit 5.0%, current limit 3.5% → no breach for that position.
- [ ] Mixed pre-existing-and-new positions: only the new breach is emitted.
- [ ] Sector breach: sector at 24%, prior limit 25%, current limit 20% → one breach with `rule_breached="sector_concentration_pct"`, `position_id` matches the canonical synthetic format.
- [ ] Multiple sector breaches sorted by sector key ascending.
- [ ] Net-long, net-short, gross, and options-delta portfolio-level breaches each emit one breach record per rule with the documented `position_id` synthetic format.
- [ ] Options delta absent from `current_active_risk_parameters` (options disabled) does not produce a breach and does not raise.
- [ ] Loosening transition (current limit ≥ prior limit) produces no breaches for that rule, regardless of current state.
- [ ] Identical prior and current parameter sets produce empty output.
- [ ] A3 worked example reproduces the documented five-breach output (two per-position, sector, net-long, gross) in the documented order.
- [ ] Missing required rule in `current_active_risk_parameters` raises `ValueError` naming the rule.
- [ ] Sector resolver returning a sector key not in `sector_exposure_pct` raises `ValueError` naming the missing key.
- [ ] Output records are frozen — assigning to a field raises `ValidationError`.
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
