---
status: not_started
completed_date:
commit_id:
---

# 05a — Halt-state computation

## Goal

Land the deterministic primitive that derives `HaltState` from a `DrawdownState` and the active risk parameter set. The halt state surfaces to state-delivery's halt-mode header wrappers (state-delivery story 05) and to the continuous monitor's command-vocabulary enforcement; this story owns the *trigger detection* — when daily drawdown ≥ 100% of the daily limit OR cumulative drawdown reaches `DrawdownTier.FULL_HALT`, a halt is active. Otherwise, no halt; the function returns `None`.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` § Drawdown halt mode — Daily drawdown halt — the daily trigger:
  > Trigger: Portfolio equity declines 2.5%+ from day's opening equity, detected by the continuous monitor.
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — the tier-3 (12%) full-halt trigger that is co-equivalent to the daily halt for state-delivery rendering purposes.
- `docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header modifications — confirms `HaltState` is the typed input to halt-mode wrappers; carries `daily_halt_active`, `cumulative_full_halt_active`, `daily_drawdown_pct`, `daily_drawdown_limit_pct` for the banner text.
- `src/alphamind/portfolio_state/records/capital.py` — `DrawdownState`, `DrawdownTier`, `RiskZone`. `DrawdownState.cumulative_tier` carries the tier classifier output (story 04b's primitive populates this — coordinated edit not part of this story's scope).
- `src/alphamind/risk_guardrails/breach_behavior/types.py` — `HaltState` (story 03), `DrawdownState`, `DrawdownTier`, `ActiveRiskParameterSet` re-exports.
- `docs/implementation/06-risk-guardrails/breach-behavior/04b-cumulative-drawdown-tier.md` — sibling primitive that classifies the cumulative tier; its output populates `DrawdownState.cumulative_tier`. This story consumes the tier directly.
- `docs/implementation/06-risk-guardrails/state-delivery/05-halt-mode-header-modifications.md` — sibling story that consumes `HaltState`. State-delivery's `HaltState` definition becomes an import from breach_behavior's canonical type (story 03) when state-delivery dispatches.

## Depends on

- 02 (package skeleton)
- 03 (canonical types — `HaltState`)
- 04b (cumulative drawdown tier classifier — for the `DrawdownTier` semantics; this story does not call the classifier directly, but consumes its `DrawdownTier.FULL_HALT` output via `DrawdownState.cumulative_tier`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/breach_behavior/halt_state.py`. Tests at `tests/risk_guardrails/breach_behavior/test_halt_state.py`.

### 1. Public function

```python
def compute_halt_state(
    *,
    drawdown_state: DrawdownState,
    active_risk_parameters: ActiveRiskParameterSet,
) -> HaltState | None:
    """Determine whether halt mode is active and produce a HaltState if so.

    Halt mode is active when EITHER:
      1. Daily drawdown ≥ 100% of the daily-drawdown limit
         (i.e., drawdown_state.intraday_drawdown_pct ≥ daily_drawdown_pct rule value), OR
      2. Cumulative drawdown reached tier 3 full halt
         (i.e., drawdown_state.cumulative_tier == DrawdownTier.FULL_HALT).

    Both can be active simultaneously (e.g., a deep selloff that hits both daily and cumulative
    full-halt thresholds in the same session). The returned HaltState reflects both flags.

    When neither condition holds, returns None. The caller does not construct a HaltState; the
    rendering layer's halt-mode wrappers are not invoked; the engine's normal action vocabulary
    applies.

    Args:
        drawdown_state: The current drawdown state from the portfolio-state snapshot. Must
            carry a populated `intraday_drawdown_pct` (the intra-day drawdown from open) and
            a `cumulative_tier` field (None or DrawdownTier).
        active_risk_parameters: The current active parameter set; must contain a
            `daily_drawdown_pct` rule entry (the active daily limit).

    Returns:
        A frozen HaltState if at least one halt condition is active; None otherwise.

    Raises:
        ValueError: when active_risk_parameters lacks the `daily_drawdown_pct` entry (defensive
            — every regime resolves a daily-drawdown limit; absence indicates structural error).
    """
```

### 2. Trigger semantics

**Daily halt.** `intraday_drawdown_pct ≥ daily_drawdown_limit_pct` is the trigger. Boundary inclusivity matches the zone classifier — at exactly 100% of limit, halt fires. Once the daily threshold is crossed within a session, halt remains active for the rest of the session regardless of recovery — this primitive consumes the *current* drawdown state and reflects the *current* condition; if the caller (continuous monitor) needs latched-for-the-session semantics, it persists the latch state separately and feeds a "halt-was-engaged-this-session" flag in. For now, the primitive is stateless and reflects current conditions only. The continuous monitor implementation (execution-layer work tree) layers session-latching atop this primitive when wired.

Per `monitor-control-and-events-schema.md`, halt state is monitor-local and persists across `set_halt_mode` toggles via the `halt_mode_engaged` portfolio state field. That latching is the continuous monitor's domain; this story does not duplicate it.

**Cumulative full halt.** `drawdown_state.cumulative_tier == DrawdownTier.FULL_HALT` is the trigger. Tier 1 (CONSTRAINED) and tier 2 (HEAVILY_CONSTRAINED) are NOT halts — they apply progressive parameter overrides via story 04b's `apply_progressive_tier_overrides`, which tightens position-size and gross-exposure limits without blocking new positions. Only tier 3 maps to halt mode.

**Recovery and no-hysteresis.** Like story 04b's classifier, this primitive is stateless. If `intraday_drawdown_pct` recovers below the daily limit, the next call returns `daily_halt_active=False` (subject to the caller's session-latching layer). The caller controls latching.

### 3. HaltState field population

```python
HaltState(
    daily_halt_active=intraday_drawdown_pct >= daily_drawdown_limit_pct,
    cumulative_full_halt_active=drawdown_state.cumulative_tier == DrawdownTier.FULL_HALT,
    daily_drawdown_pct=drawdown_state.intraday_drawdown_pct,
    daily_drawdown_limit_pct=daily_drawdown_limit_pct,
)
```

The two drawdown numbers in the `HaltState` carry the **active daily** values (current and limit), not the cumulative ones. This matches the design's halt-mode banner template:
```
** HALT MODE ACTIVE — daily drawdown {current}% / {limit}% **
```
which is daily-pct-format unconditionally per state-delivery story 05's notes (when only cumulative is active, the daily values reflect the current daily drawdown, possibly 0% if cumulative tier 3 was reached gradually). The `cumulative_full_halt_active` flag is what state-delivery's wrappers read to decide which mode-line to render (`Mode: WATCHLIST ONLY` for analyst, etc.).

### 4. Tests

Tests at `tests/risk_guardrails/breach_behavior/test_halt_state.py`:

#### Daily-halt-only

- **Daily exactly at 100% of limit:** `intraday_drawdown_pct=2.5`, daily limit 2.5%, cumulative tier None → `HaltState(daily_halt_active=True, cumulative_full_halt_active=False, daily_drawdown_pct=2.5, daily_drawdown_limit_pct=2.5)`.
- **Daily above 100% of limit:** `intraday_drawdown_pct=3.2`, daily limit 2.5%, cumulative tier None → `daily_halt_active=True`; `cumulative_full_halt_active=False`.
- **Daily just under 100% of limit:** `intraday_drawdown_pct=2.4`, daily limit 2.5%, cumulative tier None → returns `None` (no halt).

#### Cumulative-full-halt-only

- **Cumulative tier 3, daily below limit:** `intraday_drawdown_pct=0.8`, daily limit 2.5%, cumulative tier `FULL_HALT` → `HaltState(daily_halt_active=False, cumulative_full_halt_active=True, daily_drawdown_pct=0.8, daily_drawdown_limit_pct=2.5)`.
- **Cumulative tier 1 (CONSTRAINED), daily below limit:** `intraday_drawdown_pct=0.5`, daily limit 2.5%, cumulative tier `CONSTRAINED` → returns `None` (tier 1 is not a halt).
- **Cumulative tier 2 (HEAVILY_CONSTRAINED), daily below limit:** same → returns `None`.
- **Cumulative tier None, daily below limit:** returns `None`.

#### Both halts active

- **Daily at 100%, cumulative tier 3:** `intraday_drawdown_pct=2.5`, daily limit 2.5%, cumulative tier `FULL_HALT` → `HaltState(daily_halt_active=True, cumulative_full_halt_active=True, ...)`. Both flags set.

#### Daily limit lookup

- **Elevated regime (daily limit 2.0%):** `active_risk_parameters` carries `daily_drawdown_pct.value=2.0`. `intraday_drawdown_pct=2.0` → halt active. `intraday_drawdown_pct=1.99` → no halt.
- **Crisis regime (daily limit 1.5%):** `daily_drawdown_pct.value=1.5`. `intraday_drawdown_pct=1.5` → halt active.

#### Validation

- **Missing `daily_drawdown_pct` rule entry raises:** `active_risk_parameters` without the rule → `ValueError` naming the missing rule.

#### Determinism and shape

- **Pure function:** repeated calls with identical inputs produce equal outputs (or both `None`).
- **Frozen output:** assigning to a returned `HaltState` field raises `ValidationError`.
- **`None` returned, not constructed-with-False-flags:** when no halt is active, the function returns `None` (the `HaltState` post-validator forbids both flags False, so this is the only valid return shape for "no halt"). Test asserts `compute_halt_state(...) is None` rather than checking flags on a constructed instance.

#### Worked examples

- **Scenario A4 reproduction:** `intraday_drawdown_pct=2.8`, daily limit 2.5%, no cumulative breach → `daily_halt_active=True`, `cumulative_full_halt_active=False`, `daily_drawdown_pct=2.8`, `daily_drawdown_limit_pct=2.5`.
- **Scenario A10 reproduction:** crisis regime hit; drawdown spikes triggering both daily and cumulative-full-halt. `intraday_drawdown_pct=4.0`, daily limit 1.5% (crisis), cumulative tier `FULL_HALT` → both flags true.

Out of scope:
- Session-latching ("once halt fires, stays for the rest of the session") — the continuous monitor owns persistence across calls.
- The `set_halt_mode` operator action (toggling halt manually via the command center) — that's command-center territory.
- Action-vocabulary enforcement (rejecting OPEN/ADD during halt) — that's the engine's T3 layer + command intake's mode-overlay enforcement.
- Computing `intraday_drawdown_pct` itself — data-layer / portfolio-state assembler.
- Computing `cumulative_tier` itself — story 04b's classifier; its output populates `DrawdownState.cumulative_tier` in the assembler.

## Notes

**Why this primitive returns `Optional[HaltState]` rather than always returning a `HaltState`.** Constructing a `HaltState` with both flags False is forbidden by the model's post-validator (story 03). Returning `None` for "no halt" is the natural way to signal absence; callers check `if halt_state is None: render_normal_header(...) else: render_halt_mode_header(halt_state, ...)`. State-delivery story 05's halt-mode wrappers are only invoked when halt is active; this primitive returns `None` to signal that condition cleanly.

**Why this primitive lives in breach_behavior rather than state_delivery.** Halt detection is a breach-behavior policy decision (the trigger thresholds come from the design's halt-mode contract, not from the rendering layer). The rendering layer consumes the typed input. State-delivery story 05's local `HaltState` definition is an integration artifact — when state-delivery dispatches, it imports the canonical type from here, and any halt detection it might need to perform calls this function.

**Why the primitive does not call story 04b's `classify_cumulative_drawdown_tier` directly.** The cumulative tier is a field on `DrawdownState` populated by the portfolio-state assembler (which calls story 04b's classifier when populating the field). By the time this primitive runs, the tier is already classified and surfaced in the snapshot. Re-classifying here would duplicate work and risk drift between the two callers.

**Per `feedback_simplify_before_building.md`,** the function is one return statement plus the field-population logic. No state, no helper class, no per-trigger plugin architecture.

**Per `feedback_avoid_numeric_anchors.md`,** the daily limit comes from `active_risk_parameters` (resolver-composed); the function does not embed `2.5` or any other percentage. The cumulative tier comes from `DrawdownState.cumulative_tier`; story 04b's classifier owns the tier-trigger thresholds.

**Per `feedback_no_inventing_component_names.md`,** the function name `compute_halt_state` and the `HaltState` typed record both come from state-delivery story 05's design notes and the design doc's "Drawdown halt mode" terminology. The function consumes `DrawdownState`, `DrawdownTier`, and `ActiveRiskParameterSet` — all canonical names.

**Cross-feature sequencing note (already covered in story 03 Notes).** Until state-delivery dispatches, its locally-declared `HaltState` shadows breach-behavior's canonical type. The cutover is mechanical: replace state-delivery's local class with `from alphamind.risk_guardrails.breach_behavior import HaltState`. The orchestrator surfaces this at state-delivery dispatch time.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/halt_state.py` exists and defines `compute_halt_state` with the documented signature.
- [ ] `compute_halt_state` is re-exported from `src/alphamind/risk_guardrails/breach_behavior/__init__.py`.
- [ ] The function is pure — no I/O, no global state, no clock reads.
- [ ] Daily halt boundary inclusive: `intraday_drawdown_pct=2.5`, daily limit 2.5% → returns a HaltState with `daily_halt_active=True`.
- [ ] Daily halt above limit: `intraday_drawdown_pct=3.2`, daily limit 2.5% → `daily_halt_active=True`.
- [ ] Daily below limit, no cumulative halt → returns `None`.
- [ ] Cumulative tier `FULL_HALT`, daily below limit → returns a HaltState with `cumulative_full_halt_active=True`, `daily_halt_active=False`.
- [ ] Cumulative tiers `CONSTRAINED` and `HEAVILY_CONSTRAINED` and `None` (with daily below limit) → returns `None` (these are not halts).
- [ ] Both daily-at-limit and cumulative tier `FULL_HALT` → both flags true in returned HaltState.
- [ ] HaltState's `daily_drawdown_pct` and `daily_drawdown_limit_pct` carry the input drawdown's `intraday_drawdown_pct` and the active daily-limit value (not cumulative values).
- [ ] Active limit changes with regime: with `daily_drawdown_pct.value=2.0` (elevated), `intraday_drawdown_pct=2.0` → halt active; `intraday_drawdown_pct=1.99` → no halt.
- [ ] Missing `daily_drawdown_pct` entry in `active_risk_parameters` raises `ValueError` naming the rule.
- [ ] Returned HaltState is frozen — assigning to a field raises `ValidationError`.
- [ ] When no halt is active, the function returns `None` (test uses `is None`, not flag inspection).
- [ ] Determinism: 100 repeated calls with identical inputs produce identical outputs (both equal HaltState or both None).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
