# Volatility regime context missing in adaptive researcher input — all "unknown" while populated everywhere else upstream

## Symptom

In invocation `inv-20260519T030654Z-60f10023`, the adaptive researcher's input bundle has the VOLATILITY REGIME block entirely populated with `unknown` values:

```
=== VOLATILITY REGIME ===
Regime: unknown
Transition: unknown
Confidence: unknown
Freshness: unknown
```

Meanwhile, the same regime context is fully populated upstream:

* Qualitative researcher input: `regime_label: vol_expansion`, `transition_state: stable`, `indicator_agreement_count: 4`, `invocations_held: 13`, vix_level, realized vol values all present
* Domain researcher inputs: each receives the regime as part of their universal context block
* Synthesizer brief: `[CR-1] vol_expansion (stable, indicator agreement 4/4)` with full underlying values

So the volatility regime IS calculated and IS propagating to most agents — the adaptive researcher's input bundle assembler specifically isn't picking it up.

Real downstream consequence: the adaptive researcher's investigation threads cite the regime context as relevant ("In a vol_expansion regime (VIX 17.26)" appears in multiple adaptive findings). The agent had to re-derive the regime label from VIX level via `macro_data` tool calls — a workaround for missing context that should be in the input bundle. Every entry in its DISTILLATION ANOMALY FLAGS block also says `regime_context=none`.

## Evidence

`analysis/adaptive_researcher/user_message.md` lines 3-7:

```
=== VOLATILITY REGIME ===
Regime: unknown
Transition: unknown
Confidence: unknown
Freshness: unknown
```

`analysis/qualitative_researcher/user_message.md` lines 5-17 (regime populated correctly):

```
## VOLATILITY REGIME
indicator_agreement_count: 4
invocations_held: 13
prior_label: vol_expansion
regime_label: vol_expansion
transition_state: stable
vix_level: 17.26
...
```

`analysis/synthesizer/user_message.md` line 13: `[CR-1] vol_expansion (stable, indicator agreement 4/4)`

`analysis/adaptive_researcher/user_message.md` lines 10-107 (all 49 D-flags carry `regime_context=none`):

```
[D-1] block=q1.price_move_anomaly flag=price_move_anomaly magnitude=2.45 severity=investigate_if_persists
       freshness=2026-05-19T03:06:54Z regime_context=none
```

## Root cause \[hypothesis\]

The adaptive researcher's input bundle assembler is a different code path from the qualitative researcher's, synthesizer's, and domain researchers'. The other bundles' assemblers read the regime state correctly; the adaptive one either:

1. **Reads from a different source/cache** that's empty
2. **Has a hardcoded placeholder** that's not being replaced with the real value (the literal "unknown" pattern matches an unparameterized template)
3. **Reads from the calibration state** which doesn't explicitly carry the current regime label

The `regime_context=none` on every D-flag also suggests the distillation-side flag-emission code isn't passing the regime context to the flag records — a separate but related propagation gap.

## Scope

1. Audit the adaptive researcher's input bundle assembler. Locate where VOLATILITY REGIME / Regime / Transition / Confidence / Freshness are populated and confirm what data source they read from.
2. Wire the assembler to the same regime state source that the qualitative researcher's input uses (which is correctly populated).
3. Audit the distillation flag emitters that produce D-flags. The `regime_context=none` on every flag suggests the regime context isn't being attached at emission time; verify whether it's supposed to carry the regime label per flag.
4. Add a unit/integration test that confirms regime population in the adaptive researcher's input bundle whenever the regime is set elsewhere.

## Acceptance criteria

- [ ] Adaptive researcher's VOLATILITY REGIME block matches the qualitative researcher's block (same values for shared fields)
- [ ] D-flag entries' `regime_context` field carries the active regime label, not `none`, when a regime is set
- [ ] Unit test confirms the adaptive bundle assembler populates regime fields when given a known regime state
- [ ] Adaptive researcher's findings cite the regime context via the input bundle rather than via macro_data tool workarounds

## Verification

* Read the code for the adaptive researcher's input bundle assembler. Confirm it reads from the same regime state source as the qualitative bundle (which works).
* Unit test the adaptive bundle assembler: pass a known regime state (e.g., `vol_expansion`, `stable`, 4/4 indicator agreement); confirm the assembled VOLATILITY REGIME block reflects those values, not "unknown".
* Unit test the D-flag emitter with a known regime context; confirm emitted flags carry `regime_context=vol_expansion` (or the equivalent active value), not `none`.
* Inspect the assembled adaptive `user_message.md` from a future invocation; the VOLATILITY REGIME block should match the qualitative one for the same invocation.

## Notes

This is High severity because regime-dependent severity calibration of the D-flags is a load-bearing input to the adaptive researcher's prioritization. Without the regime context, the agent re-derives the label from VIX level via macro_data tool calls — costing extra tool budget and adding inference variance where the regime classifier already produced an authoritative answer upstream.
