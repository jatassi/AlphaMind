---
status: in_progress
completed_date:
commit_id:
---

# 09 — Volatility regime classification and universal broadcast

## Goal

Implement the composite volatility regime classification (the layer's most consequential persistent state) plus the regime-transition confidence machinery and the universal broadcast contract that makes the regime label visible to every downstream agent. The four-tier ladder (low-vol compression / vol expansion / crisis-spike / vol normalization), the four-state transition discipline (`stable` / `early-weak` / `early-strong` / `confirmed`), and the regime-skip emergency wiring all live here.

## Reading

- `docs/design/02-distillation-layer/external.md` § 4 Persistent state and composites — Volatility regime classification subsection (the four-tier label + the regime transition detection contract)
- `docs/design/02-distillation-layer/external.md` § Output format — "Volatility regime classification label as universal context" (delivered to every agent)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Regime classification boundaries — VIX boundaries, term-structure backwardation, VVIX percentiles
- `docs/design/02-distillation-layer/threshold-calibration.md` § Regime transition confidence — `regime_transition_confirmed_invocations`, `regime_transition_indicator_agreement_min`, `regime_skip_emergency_trigger`
- `docs/design/06-risk-guardrails/regime-adaptation.md` — downstream consumer specification (how the four-tier label maps to guardrail multipliers; how `early-strong` vs. `early-weak` vs. `confirmed` drives tightening vs. loosening behavior)
- `docs/design/06-risk-guardrails/breach-behavior.md` § Emergency invocation trigger — the `regime_skip_emergency_trigger` consumer
- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — "Volatility regime label (from the distillation layer, section 4): Current regime classification ... Universal context delivered to every analysis-layer agent"
- Stories 03 (`distillation_regime_state` table), 04, 05, 06.

## Depends on

- 04, 05, 06, 07. (Story 07 is included because the supporting indicators — VIX percentile, VVIX percentile, term-structure backwardation — feed off baselines maintained via the refresh primitive.)

## Scope

In scope: under `src/alphamind/distillation/regime.py` —

- **Regime label computation**: per invocation, classify the current regime as one of:
  - `low_vol_compression`: VIX ≤ `regime_low_vol_vix_max` (14.0) AND term structure in steep contango (VX1 - VIX > 1.0) AND VVIX percentile ≤ `regime_vvix_low_percentile` (30) AND realized vol declining (trailing 5-day < trailing 20-day).
  - `vol_expansion`: VIX in [`regime_normal_vix_min`, `regime_normal_vix_max`] OR [`regime_elevated_vix_min`, `regime_elevated_vix_max`] AND term structure flattening (VX1 - VIX trending toward 0 over 5 days) AND realized vol rising.
  - `crisis_spike`: VIX ≥ `regime_crisis_vix_min` (35.0) AND term structure in backwardation (VX1 - VIX ≤ `regime_term_structure_backwardation_threshold` = 0.0) AND VVIX percentile ≥ `regime_vvix_high_percentile` (80).
  - `vol_normalization`: VIX declining from elevated levels (current < trailing 20-day mean by ≥ 5) AND term structure returning to contango (VX1 - VIX > 0 after recent backwardation) AND realized vol declining.
  - The classification is rule-based with an explicit fallback: when no rule definitively fires, default to whichever regime the VIX-band classification alone implies (the VIX boundary segments from `regime_classification` config). Document the fallback path in source.
- **Indicator agreement count**: count how many of the four underlying indicators (VIX level, term structure shape, VVIX percentile, realized vol direction) agree with the chosen regime label. Stored in `distillation_regime_state.indicator_agreement_count`.
- **Transition state machine**: per [`threshold-calibration.md § Regime transition confidence`](../../../design/02-distillation-layer/threshold-calibration.md#regime-transition-confidence) and [`regime-adaptation.md § Transition mechanics`](../../../design/06-risk-guardrails/regime-adaptation.md):
  - On every invocation, read the prior `distillation_regime_state` row.
  - If the new label equals the prior label: increment `invocations_held` by 1, `transition_state = 'stable'`.
  - If the new label differs from the prior label: set `invocations_held = 1` (this is the first invocation at the new label), determine `transition_state`:
    - `confirmed` once the new label has held for ≥ `regime_transition_confirmed_invocations` (2) consecutive invocations.
    - `early-strong` while not yet confirmed AND `indicator_agreement_count >= regime_transition_indicator_agreement_min` (3).
    - `early-weak` while not yet confirmed AND `indicator_agreement_count < 3`.
  - Append a new row to `distillation_regime_state` with `as_of`, `regime_label`, `prior_label`, `transition_state`, the supporting indicator state, `invocations_held`, and `indicator_agreement_count`.
- **Regime-skip emergency trigger**: per `regime_skip_emergency_trigger = True`:
  - If the new label "skips" a level (low-vol → elevated, normal → crisis, low-vol → crisis — any transition that crosses ≥ 2 boundaries on the four-tier ladder), emit a special `regime_skip_emergency` flag in the output.
  - The flag's downstream consumer is the continuous monitor's emergency-invocation trigger per [`breach-behavior.md § Emergency invocation trigger`](../../../design/06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger). This story emits the flag; wiring into the monitor's trigger surface is the monitor's responsibility (out of distillation scope).
- **Universal broadcast contract**: emit a dedicated `OutputBlock` with `block_id = "regime.label"` and `audience = OutputAudience.UNIVERSAL_BROADCAST`. Payload includes:
  - `regime_label` (one of the four labels)
  - `transition_state` (`stable` / `early-weak` / `early-strong` / `confirmed`)
  - `prior_label` (when changed; `None` otherwise)
  - `invocations_held`
  - `regime_skip_emergency` (boolean)
  - `indicator_agreement_count`
  - The supporting indicator snapshot (VIX, term structure basis, VVIX percentile, realized vol)
- **Helper**: `current_regime_label(session) -> str` exposed at the module level so any downstream code (the orchestrator's universal-broadcast assembly, story 11b's `CR` brief, etc.) can read the current label without re-reading the underlying indicators. This is the canonical accessor the rest of the system uses.
- **VIX/VVIX/term-structure data sourcing**: read from `macro_observations` for the underlying series. VIX = `series_id` like `VIXCLS` (FRED); VVIX similarly. The exact series IDs come from the collector's macro source mapping — verify by reading recent rows. VX1 (front-month VIX future) requires an additional series; if unavailable in `macro_observations`, document the gap and emit `regime.label` with a `degraded` calibration state until VX1 ingestion lands.
- Unit tests:
  - Each of the four labels fires correctly under fixture conditions (canonical VIX/VVIX/term-structure inputs that should produce each label).
  - The fallback path produces a sensible label when no rule definitively fires.
  - `indicator_agreement_count` reflects the number of indicators agreeing with the chosen label.
  - Transition state machine: a label change with `indicator_agreement_count >= 3` produces `early-strong`; with `< 3` produces `early-weak`; held for 2 consecutive invocations produces `confirmed`.
  - `invocations_held` increments correctly across consecutive invocations at the same label and resets on label change.
  - `regime_skip_emergency` flag fires when the label change skips ≥ 1 level (low-vol → elevated); does not fire on adjacent-label transitions (low-vol → normal — wait, but `vol_expansion` covers both normal and elevated VIX bands per the four-tier label, so a "skip" must be defined against the four labels, not the three VIX bands; document the skip definition based on the four-label ladder ordering). Recommended ladder ordering for skip detection: `low_vol_compression` < `vol_normalization` < `vol_expansion` < `crisis_spike`; a skip = jumping by ≥ 2 positions in this ordering.
  - The `regime.label` output block carries `audience = UNIVERSAL_BROADCAST`.
  - `current_regime_label(session)` returns the most recent persisted label.
  - First-invocation-after-deploy: with no prior row in `distillation_regime_state`, the new row is written with `prior_label = None`, `invocations_held = 1`, `transition_state = 'stable'` (no transition without a prior).

Out of scope:
- Downstream guardrail multiplier application (lives in [`regime-adaptation.md`](../../../design/06-risk-guardrails/regime-adaptation.md)).
- Continuous monitor's emergency-invocation trigger wiring (monitor's responsibility).
- Per-name volatility regime (covered by story 08a — distinct from the universal market regime).
- Stress overlay or pre-event tightening overlay (operator-driven; lives in `regime-adaptation.md`).

## Notes

The regime label is the most consequential single output the distillation layer produces — it propagates into every analysis agent, every guardrail, and the emergency invocation trigger. The classification logic should err on the side of stability over reactivity: a clean rule that occasionally produces an "intermediate" label is better than a noisy classifier that flickers between adjacent labels. The transition state machine specifically exists to dampen flicker — `early-weak` for unconfirmed changes with weak indicator agreement, `confirmed` only after sustained holding.

The "regime skip" semantics need careful thought. Per `threshold-calibration.md`: "Whether a regime label that skips a level (low-vol → elevated, normal → crisis, low-vol → crisis) triggers an emergency invocation." The examples list VIX-band skips (low-vol band → elevated band), not four-label skips. The four labels (`low_vol_compression`, `vol_expansion`, `vol_normalization`, `crisis_spike`) don't map cleanly to VIX bands — `vol_expansion` covers both `normal` and `elevated` VIX bands. Recommendation: define the skip detection against the underlying VIX-band classification (the segments in `regime_classification` config) rather than the four labels, so the spec's examples work directly. Document this choice — it diverges slightly from the four-label scheme but matches the threshold-calibration intent.

The `indicator_agreement_count` mechanism is what distinguishes `early-strong` from `early-weak`. This is the lever guardrails use to decide whether to immediately tighten or to wait — `early-strong` fires immediate tightening per `regime-adaptation.md`. Get the agreement count right: each indicator votes 0 or 1 based on whether its value is consistent with the chosen label.

The first-invocation case (no prior `distillation_regime_state` row) is a real bootstrap scenario. Handle it with `prior_label = None` and `transition_state = 'stable'` — the system has no transition to evaluate. The guardrails see the label as its starting state, no early/confirmed gating needed.

`current_regime_label(session)` is the single accessor the rest of the codebase should use to read the current regime. This indirection lets future implementations cache the value, switch storage layouts, etc., without churning callers.

## Acceptance criteria

- [ ] All four regime labels can be produced under their documented input conditions.
- [ ] Fallback classification produces a sensible label when rules don't definitively fire.
- [ ] `indicator_agreement_count` equals the number of supporting indicators agreeing with the chosen label.
- [ ] Transition state machine produces `stable` when label is unchanged.
- [ ] `early-strong` produced when label changes with `indicator_agreement_count >= 3`.
- [ ] `early-weak` produced when label changes with `indicator_agreement_count < 3`.
- [ ] `confirmed` produced after `regime_transition_confirmed_invocations` (2) consecutive invocations at the new label.
- [ ] `invocations_held` increments correctly across consecutive same-label invocations and resets on change.
- [ ] `regime_skip_emergency` flag fires when the underlying VIX-band classification skips ≥ 1 band; does not fire on adjacent-band transitions.
- [ ] Universal broadcast `OutputBlock` for `regime.label` carries `audience = UNIVERSAL_BROADCAST` and includes all documented payload fields.
- [ ] `current_regime_label(session)` returns the most recently persisted label.
- [ ] First-invocation-after-deploy correctly populates `prior_label = None`, `invocations_held = 1`, `transition_state = 'stable'`.
- [ ] Skip-definition choice (VIX-band vs. four-label ladder) is documented in source.
- [ ] Unit tests cover all of the above with fixture data.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
