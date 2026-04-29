---
status: in_progress
completed_date:
commit_id:
---

# 04b — Cumulative drawdown tier classifier & progressive parameter overrides

## Goal

Land the deterministic primitives that classify cumulative drawdown into one of three progressive tiers (`CONSTRAINED` / `HEAVILY_CONSTRAINED` / `FULL_HALT`) and compute the tier-specific parameter overrides that tighten max-position-size and max-gross-exposure during a tier-1 or tier-2 cumulative drawdown response. The classifier consumes the per-rule `progressive_tiers` configuration from `guardrails.yaml`; the override applier returns an updated `ActiveRiskParameterSet` reflecting the tier's restrictions.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — the three-tier progressive table:
  - 8% (100% of limit, tier 1) → max position size 3%, max gross 80%
  - 10% (125% of limit, tier 2) → max position size 2%, max gross 60%
  - 12% (150% of limit, tier 3) → full halt; no new positions; orderly wind-down
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — Recovery: "Each tier's restrictions exit when drawdown depth drops below its threshold. No hysteresis."
- `config/guardrails.yaml` — the shipped `cumulative_drawdown_pct` rule's `progressive_tiers` block:
  ```yaml
  progressive_tiers:
    - { trigger_pct: 8,  max_position_size_pct: 3, max_gross_pct: 80 }
    - { trigger_pct: 10, max_position_size_pct: 2, max_gross_pct: 60 }
    - { trigger_pct: 12, full_halt: true }
  ```
- `src/alphamind/config/models/guardrails.py` — already-shipped `ProgressiveTier` and the validator that enforces `progressive_tiers_only_on_cumulative_drawdown` and monotonically-increasing `trigger_pct`.
- `src/alphamind/portfolio_state/records/capital.py` — `DrawdownTier` enum (CONSTRAINED, HEAVILY_CONSTRAINED, FULL_HALT) and `DrawdownState.cumulative_tier` field; `ActiveRiskParameterSet` and `ActiveRiskParameterEntry` records; `RegimeLabel` and `RegimeTransitionState`.
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Position handling when tightening creates breaches — confirms the cumulative-tier overrides apply *on top of* regime-resolved limits. This story's override applier accepts an `ActiveRiskParameterSet` whose values already reflect regime multipliers; the cumulative-tier overrides further tighten if applicable.
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `DrawdownTier`, `ActiveRiskParameterSet`, `ProgressiveTier` re-exports.
- `docs/implementation/06-risk-guardrails/breach-behavior/04a-zone-classifier.md` — sibling primitive; not a dependency, but compositional context.

## Depends on

- 02 (package skeleton)
- 03 (canonical types — for `DrawdownTier`, `ProgressiveTier`, `ActiveRiskParameterSet`, `ActiveRiskParameterEntry` re-exports)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py`. Tests at `tests/risk_guardrails/breach_behavior/test_drawdown_tiers.py`.

### 1. Tier classifier

```python
def classify_cumulative_drawdown_tier(
    *,
    current_drawdown_pct: float,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> DrawdownTier | None:
    """Classify cumulative drawdown into a progressive response tier.

    Returns None when current_drawdown_pct is below every tier's trigger_pct (no tier active).

    Tier mapping (assumes the three-tier shipped configuration; the function generalizes
    to any monotonically-increasing trigger sequence):
        current ≥ tier 3 trigger_pct AND tier 3 has full_halt=True → DrawdownTier.FULL_HALT
        current ≥ tier 2 trigger_pct                              → DrawdownTier.HEAVILY_CONSTRAINED
        current ≥ tier 1 trigger_pct                              → DrawdownTier.CONSTRAINED
        current < tier 1 trigger_pct                              → None

    Args:
        current_drawdown_pct: The portfolio's current cumulative drawdown from HWM, in percent.
            Must be ≥ 0.
        progressive_tiers: The ordered tuple of ProgressiveTier configurations from the
            cumulative_drawdown_pct rule's progressive_tiers field. Must be non-empty and
            monotonically increasing in trigger_pct (already enforced upstream by the
            GuardrailsConfig validator; defensive check here on tuple emptiness).

    Returns:
        The tier the drawdown falls into, or None if below the first trigger.

    Raises:
        ValueError: when current_drawdown_pct is negative or progressive_tiers is empty.
    """
```

**Tier mapping rationale.** The shipped configuration declares three tiers — tier 1 (CONSTRAINED) at 8%, tier 2 (HEAVILY_CONSTRAINED) at 10%, tier 3 (FULL_HALT) at 12%. The classifier maps each tier index in `progressive_tiers` to a `DrawdownTier` enum value as follows:

- The last tier (highest `trigger_pct`) with `full_halt=True` maps to `DrawdownTier.FULL_HALT`.
- The second-to-last tier (or any tier with both `max_position_size_pct` and `max_gross_pct` set and `full_halt=False`) maps to `DrawdownTier.HEAVILY_CONSTRAINED`.
- The first tier (lowest `trigger_pct`) maps to `DrawdownTier.CONSTRAINED`.

The mapping uses the tier *position* in the ordered list, not the `trigger_pct` value. This decouples the classifier from the specific 8/10/12 thresholds — operator tuning (e.g., 7/9/11) does not require code changes.

**Recovery / no hysteresis.** The classifier is stateless. If `current_drawdown_pct` recovers from 11% to 9%, the next call returns `HEAVILY_CONSTRAINED` (down from `FULL_HALT`); from 9% to 7%, it returns `CONSTRAINED`; below 8%, it returns `None`. Per the design — "No hysteresis. If the portfolio recovers to 7% drawdown, full operating capacity returns."

### 2. Override applier

```python
def apply_progressive_tier_overrides(
    *,
    active_risk_parameters: ActiveRiskParameterSet,
    tier: DrawdownTier | None,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> ActiveRiskParameterSet:
    """Return an ActiveRiskParameterSet with per-tier overrides applied.

    Tier 1 (CONSTRAINED) and tier 2 (HEAVILY_CONSTRAINED) override:
      - position_max_size_pct: replaced with the tier's max_position_size_pct
      - gross_exposure_pct: replaced with the tier's max_gross_pct

    Tier 3 (FULL_HALT) does not override individual rule values — the full-halt response
    blocks new OPEN/ADD entirely (enforced by the engine's halt-mode action vocabulary
    via state-delivery's PM halt-mode header), so the per-rule values are preserved
    unchanged. The HaltState produced by story 05a carries the full-halt signal; this
    story's override applier returns the input unchanged when tier == FULL_HALT.

    When tier is None, returns the input unchanged.

    The override replaces only the two named rule entries; all other entries (sector
    concentration, options greeks, drawdown limits, etc.) flow through unchanged. The
    returned ActiveRiskParameterSet preserves regime_label, transition_state,
    transition_invocations_remaining, parameter_change_flag, and active_overlays from
    the input. (active_overlays gains "cumulative_drawdown_tier_{1|2|3}" — see below.)

    Args:
        active_risk_parameters: The pre-override parameter set, typically the regime-resolved
            output from regime adaptation.
        tier: The current cumulative drawdown tier, or None for no tier active.
        progressive_tiers: The ordered tuple of ProgressiveTier configurations.

    Returns:
        An ActiveRiskParameterSet with the tier overrides applied (or the input unchanged
        when tier is None or FULL_HALT).

    Raises:
        ValueError: when tier is not None but progressive_tiers does not carry a corresponding
            entry (e.g., tier=HEAVILY_CONSTRAINED but progressive_tiers has only one entry).
        KeyError: when active_risk_parameters does not contain entries for position_max_size_pct
            or gross_exposure_pct (defensive — the rule IDs are canonical and should always be
            present).
    """
```

**Override identity.** The override replaces the `value` and `regime_multiplier_applied` fields of the matching `ActiveRiskParameterEntry` (the latter is reset to a synthetic `1.0` because the override is a tier-level replacement, not a regime multiplier; the `base_value` field stays at its original base — operator-tunable per `rules-and-limits.md`'s base values). The `active_overlays` member is extended with the tier tag and re-sorted alphabetically:

- `tier=CONSTRAINED` → tag `"cumulative_drawdown_tier_1"` appended; resulting tuple sorted ascending by string value.
- `tier=HEAVILY_CONSTRAINED` → tag `"cumulative_drawdown_tier_2"`; sorted.
- `tier=FULL_HALT` → tag `"cumulative_drawdown_tier_3"`; sorted.

**`active_overlays` ordering.** Per `regime_adaptation/08`'s convention, `active_overlays: tuple[str, ...]` is alphabetically sorted by string value. The override applier preserves this invariant: after appending the tier tag, the resulting tuple is re-sorted via `tuple(sorted(combined_tags))`. For example, an input `("pre_event", "stress")` with tier 1 produces `("cumulative_drawdown_tier_1", "pre_event", "stress")`, not `("pre_event", "stress", "cumulative_drawdown_tier_1")`.

The overlay name surfaces in state-delivery's PM header `Active regime overrides:` block (per state-delivery story 04c), making the tier visible to the agent.

**Why tier=FULL_HALT is a no-op for rule values.** During full halt, the engine blocks all new OPEN/ADD commands at the action-vocabulary layer (per `config/modes/halt.yaml` — `pm.allowed_command_types: [CLOSE, ADJUST, CANCEL]`). The remaining commands (CLOSE/ADJUST/CANCEL) do not consume position-size or gross-exposure headroom; tightening their values is meaningless. The `cumulative_drawdown_tier_3` overlay tag still appears so the operator and agent see the active tier; the rule values flow through unchanged.

**Composition with regime overrides.** The input `active_risk_parameters` typically comes from regime adaptation's resolver and already reflects regime multipliers (e.g., elevated regime: `position_max_size_pct: 3.5%`). Applying tier-1 overrides yields the documented post-tier values (e.g., position size 3% vs. the pre-tier 3.5%). When the regime-resolved value is *tighter* than the tier override (e.g., crisis regime: `position_max_size_pct: 2%`, vs. tier 1: 3%), the override would *loosen* the limit — undesirable. This story's applier resolves the conflict by taking `min(regime_value, tier_value)` for each overridden field. Tier overrides only tighten; they never loosen.

### 3. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_drawdown_tiers.py`:

#### Tier classification

- **Below tier 1 → None:** `current_drawdown_pct=7.99` against shipped tiers (8/10/12) returns `None`.
- **Tier 1 boundary (inclusive):** `current_drawdown_pct=8.0` returns `DrawdownTier.CONSTRAINED`.
- **Tier 1 mid-range:** `current_drawdown_pct=9.5` returns `DrawdownTier.CONSTRAINED`.
- **Tier 2 boundary:** `current_drawdown_pct=10.0` returns `DrawdownTier.HEAVILY_CONSTRAINED`.
- **Tier 2 mid-range:** `current_drawdown_pct=11.5` returns `DrawdownTier.HEAVILY_CONSTRAINED`.
- **Tier 3 boundary:** `current_drawdown_pct=12.0` returns `DrawdownTier.FULL_HALT`.
- **Tier 3 well past:** `current_drawdown_pct=20.0` returns `DrawdownTier.FULL_HALT`.
- **Recovery — no hysteresis:** sequence of calls with `current_drawdown_pct = [13.0, 11.5, 9.5, 7.5]` produces `[FULL_HALT, HEAVILY_CONSTRAINED, CONSTRAINED, None]`. The classifier carries no state.
- **Negative drawdown rejected:** `current_drawdown_pct=-1.0` raises `ValueError` with the value in the message.
- **Empty tiers tuple rejected:** `progressive_tiers=()` raises `ValueError`.
- **Custom tiers (8/12 only):** with two-tier configuration `[(trigger_pct=8, max_pos=3, max_gross=80), (trigger_pct=12, full_halt=True)]`, `current_drawdown_pct=9.0` returns `DrawdownTier.CONSTRAINED` (only one non-halt tier, so it's tier 1) and `current_drawdown_pct=12.5` returns `DrawdownTier.FULL_HALT`. (No `HEAVILY_CONSTRAINED` because the configuration omits the middle tier.)

#### Override application

- **Tier=None → unchanged:** `apply_progressive_tier_overrides(tier=None, ...)` returns the input set's content unchanged (deep equality, including `active_overlays`).
- **Tier=CONSTRAINED on shipped tiers — position size and gross overrides applied:** input set with `position_max_size_pct.value=5.0` and `gross_exposure_pct.value=120.0` becomes `position_max_size_pct.value=3.0` and `gross_exposure_pct.value=80.0`; `regime_multiplier_applied` for both updated entries becomes `1.0`; `base_value` preserved; other entries unchanged; `active_overlays` includes `"cumulative_drawdown_tier_1"`.
- **Tier=HEAVILY_CONSTRAINED on shipped tiers:** corresponding values become 2.0 and 60.0; overlay tag is `"cumulative_drawdown_tier_2"`.
- **Tier=FULL_HALT on shipped tiers:** rule values flow through unchanged; `active_overlays` includes `"cumulative_drawdown_tier_3"`; no `position_max_size_pct` or `gross_exposure_pct` value change.
- **Override never loosens:** input with `position_max_size_pct.value=2.5` (already tighter than tier 1's 3.0, e.g., due to crisis regime) → output retains `value=2.5` after applying tier 1; `min(regime, tier)` semantics. Same for `gross_exposure_pct.value=70.0` against tier 1's 80.0.
- **Missing rule entry raises:** input set without `position_max_size_pct` raises `KeyError` naming the missing rule.
- **Insufficient tier coverage raises:** `tier=HEAVILY_CONSTRAINED`, `progressive_tiers=(tier1_only,)` raises `ValueError`.
- **Idempotency:** applying tier 1 overrides twice produces the same output as applying once (the overlay tag does not duplicate; the rule values stabilize at the override).

#### Composition

- **Composes with regime adaptation:** input set carries `regime_label=ELEVATED, position_max_size_pct.value=3.5, regime_multiplier_applied=0.7, base_value=5.0`. Applying tier 1 (`max_pos=3.0`) yields `value=3.0, regime_multiplier_applied=1.0, base_value=5.0` (tier override wins because 3.0 < 3.5; regime label preserved; transition state preserved).
- **Determinism:** two calls with identical inputs produce equal outputs (both functions are pure).

Out of scope:
- The decision to *enter* halt mode based on cumulative tier — story 05a (halt state computation) reads tier 3 as one of two halt-mode triggers.
- Per-rule sub-selection (e.g., positions > 10% loss flagged at tier 1, > 15% at tier 2). The design's "Existing positions with unrealized loss > 10% flagged for PM review" is state-delivery / strategist-context territory; the override applier here only tightens position-size and gross-exposure rule values. The flagging is a separate primitive (deferred to a future scenario-tests story or to the strategist context primitive in regime-adaptation if it lands there).
- Computing the cumulative drawdown value itself (`current_drawdown_pct`) — that's data-layer / portfolio-state computation; this primitive consumes it.

## Notes

**Why this primitive lives in breach_behavior rather than portfolio_state.** The `DrawdownTier` enum lives in `portfolio_state/records/capital.py` because it is a typed field on `DrawdownState`. The *classification logic* — the mapping from `current_drawdown_pct` and `progressive_tiers` configuration to the tier — is breach-policy and lives here. The portfolio-state assembler imports this classifier when populating `DrawdownState.cumulative_tier`. (As of this story's commit, the assembler accepts `cumulative_tier` as an input field; a coordinated edit in the rules-and-limits production-populator story can wire the assembler to call `classify_cumulative_drawdown_tier` directly. That is a follow-up integration, not part of this story's scope.)

**Why a single classifier rather than per-tier predicates.** The three tiers are encoded in the `progressive_tiers` configuration. One classifier handles the shipped 3-tier setup and any future configuration changes (e.g., a 4-tier extension or a 2-tier simplification) without code changes. Per `feedback_simplify_before_building.md`.

**Per `feedback_avoid_numeric_anchors.md`,** the classifier and override applier do not hardcode the 8/10/12 trigger percentages, the 3%/2% position-size overrides, or the 80%/60% gross overrides. All numeric values come from `progressive_tiers` configuration, sourced from `guardrails.yaml`. The `tier_position → DrawdownTier` mapping is the only encoded structural assumption (last tier with `full_halt=True` is FULL_HALT; preceding tiers are HEAVILY_CONSTRAINED then CONSTRAINED), and that maps to the canonical three-name enum which is itself documented.

**Per `feedback_no_inventing_component_names.md`,** the function names (`classify_cumulative_drawdown_tier`, `apply_progressive_tier_overrides`) name the design's "cumulative drawdown progressive response" and "progressive tier" terminology directly. The overlay tag format `cumulative_drawdown_tier_{N}` matches the design's "tier 1 / tier 2 / tier 3" wording with `N` as the 1-indexed position.

**Min-not-max composition.** The "tier override never loosens" rule (`min(regime_value, tier_value)`) reflects the design's semantics: progressive drawdown response is a tightening overlay; if the regime has already tightened more aggressively (crisis), the regime's tighter limit prevails. The PM and strategist see a single resolved limit value and reason against it; the layered derivation is invisible to the agents.

**The `active_overlays` extension.** The shipped `ActiveRiskParameterSet.active_overlays` already carries strings naming active overlays (e.g., `"pre_event_tightening"`, `"stress_overlay"`). This story's tag format `cumulative_drawdown_tier_{N}` follows the same convention. State-delivery's PM header surfaces overlays in its `Active regime overrides:` block; the tag flows through automatically once both stories land.

**Integration site (called by).** `apply_progressive_tier_overrides` is consumed by the Phase 1 enforcement-layer composition step described in `state-delivery.md § Portfolio state ingestion payload` ("Guardrail state is computed by the enforcement layer at the start of Phase 1"). That composition layer is not yet a feature in this repo — it lands as a future story under the execution-layer / continuous-monitor work tree. **Until that feature ships, the integration is a documented gap:** the canonical sequence is `regime_adaptation.resolve_regime_adaptation()` → `breach_behavior.classify_cumulative_drawdown_tier()` → `breach_behavior.apply_progressive_tier_overrides()`, with the resulting `ActiveRiskParameterSet` flowing into state-delivery and engine T3. Consumers needing the composition before the dedicated feature lands (e.g., the breach-behavior E2E story 08, scenario tests) compose the two function calls inline. See `docs/project-tracker.md § Backlog` for the tracking entry on the missing wiring.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py` exists and defines `classify_cumulative_drawdown_tier` and `apply_progressive_tier_overrides` with the documented signatures.
- [ ] Both functions are re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] Both functions are pure — no I/O, no global state, no clock reads.
- [ ] `classify_cumulative_drawdown_tier(current_drawdown_pct=7.99, progressive_tiers=shipped_tiers)` returns `None`.
- [ ] `classify_cumulative_drawdown_tier(current_drawdown_pct=8.0, ...)` returns `DrawdownTier.CONSTRAINED` (boundary inclusive).
- [ ] `classify_cumulative_drawdown_tier(current_drawdown_pct=10.0, ...)` returns `DrawdownTier.HEAVILY_CONSTRAINED`.
- [ ] `classify_cumulative_drawdown_tier(current_drawdown_pct=12.0, ...)` returns `DrawdownTier.FULL_HALT`.
- [ ] `classify_cumulative_drawdown_tier(current_drawdown_pct=20.0, ...)` returns `DrawdownTier.FULL_HALT`.
- [ ] Recovery sequence `[13.0, 11.5, 9.5, 7.5]` produces `[FULL_HALT, HEAVILY_CONSTRAINED, CONSTRAINED, None]` with no hysteresis (each call independent of the prior).
- [ ] Negative `current_drawdown_pct` raises `ValueError` whose message names the value.
- [ ] Empty `progressive_tiers` tuple raises `ValueError`.
- [ ] Two-tier custom configuration (8% non-halt + 12% full-halt) maps `current=9` → `CONSTRAINED` and `current=12.5` → `FULL_HALT` (with no `HEAVILY_CONSTRAINED` produced because no middle tier exists).
- [ ] `apply_progressive_tier_overrides(tier=None, ...)` returns input unchanged.
- [ ] `apply_progressive_tier_overrides(tier=CONSTRAINED, ...)` overrides `position_max_size_pct.value` and `gross_exposure_pct.value` to the tier 1 configuration values; appends `"cumulative_drawdown_tier_1"` to `active_overlays`; preserves all other entries and metadata fields.
- [ ] `apply_progressive_tier_overrides(tier=HEAVILY_CONSTRAINED, ...)` applies tier 2 overrides similarly; overlay tag is `"cumulative_drawdown_tier_2"`.
- [ ] `apply_progressive_tier_overrides(tier=FULL_HALT, ...)` does not override `position_max_size_pct` or `gross_exposure_pct` values; appends `"cumulative_drawdown_tier_3"` to `active_overlays`.
- [ ] Override never loosens: when input `position_max_size_pct.value < tier.max_position_size_pct`, the value is preserved; `min(regime, tier)` semantics.
- [ ] `KeyError` raised when input set lacks `position_max_size_pct` or `gross_exposure_pct` entries.
- [ ] `ValueError` raised when `tier=HEAVILY_CONSTRAINED` but `progressive_tiers` has only one entry (insufficient coverage).
- [ ] Idempotency: applying tier 1 twice yields output equal to applying once (no duplicate overlay tag; values stable).
- [ ] `active_overlays` ordering invariant preserved: applying tier 1 to an input set with `active_overlays=("pre_event", "stress")` produces `("cumulative_drawdown_tier_1", "pre_event", "stress")` (alphabetically sorted), not `("pre_event", "stress", "cumulative_drawdown_tier_1")`.
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
