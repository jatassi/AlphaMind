# Guardrail-evaluation library

Deterministic math shared by every guardrail check. Three call sites compose these primitives with their own orchestration: the agent-side [validation tool](state-delivery.md#guardrail-validation-tool), the [proposal pre-processor's](../04-decision-layer/proposal-pre-processor.md) combined-set check, and the engine's authoritative T3 check via the [guardrail enforcement layer](../05-execution-layer/architecture.md#3-guardrail-enforcement-layer).

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `current_state` | Phase 1 portfolio snapshot — positions with delta-adjusted exposures, sector exposures, directional exposures, capital, drawdown state | Baseline against which deltas are projected |
| `proposed_deltas` | Caller — one or more proposed exposure changes, each carrying instrument, direction, size | The change being evaluated |
| `active_regime` | [Regime adaptation](regime-adaptation.md) — regime label and per-rule multipliers | Resolves effective limits for this invocation |
| `active_profile` | [Rules & limits — profiles](rules-and-limits.md#portfolio-size-profiles) — active features, sectors, base rule values | Defines which rules are in scope and their base values |
| Underlying price | Real-time market data stream | Black-Scholes input for options |
| IV surface | Data pipeline ([programmatic distillation — quantitative](../02-distillation-layer/external.md)) | Black-Scholes input for options |
| Risk-free rate | [Configuration management](../configuration-management.md) | Black-Scholes input for options |

---

## Primitives

### Active regime parameter resolution

For each rule active under `active_profile`, the base value is multiplied by the rule-specific `active_regime` multiplier. The product is the effective limit. See the multiplier table in [regime adaptation](regime-adaptation.md).

### Feature-flag early-exit

When a `proposed_delta` references an instrument class disabled by `active_profile` (e.g., `asset_type: option` on `options_enabled: false`, or `direction: short` on `short_selling_enabled: false`), per-rule evaluation is skipped — result is `FAIL` with `reason: feature_disabled`. Disabled features have no rule rows in the output.

### Delta-adjusted exposure

Equity positions contribute their full notional, signed by direction. Options and strategies contribute delta-adjusted notional:

1. **Black-Scholes greeks.** Delta, gamma, theta, vega computed from underlying price, IV, risk-free rate, and time to expiration. Shares the model with the continuous monitor's [greeks refresh](../05-execution-layer/architecture.md#4d-greeks-refresh-orchestration).
2. **IV sourcing.** Interpolates the data pipeline's IV surface to the proposal's strike/expiration. When no surface exists, falls back to the underlying's trailing 30-day realized volatility.
3. **Conservative buffer.** Absolute delta is inflated by a configurable margin (default +10%) toward more exposure — a computed delta of 0.45 becomes 0.495. Overstating exposure means borderline trades may be rejected; over-limit trades are never approved. Configurable per regime; tighter regimes use higher buffer.
4. **Strategy aggregation.** Per-leg greeks sum to strategy-level net delta, gamma, theta, vega. The buffer applies to net delta's absolute value.

### Per-rule projection and evaluation

For each rule in scope, `projected = current + Σ(contribution from each proposed delta)`. Status:

- `PASS` — within limit, outside warning zone
- `WARNING` — within limit, inside warning zone (see [breach behavior](breach-behavior.md))
- `FAIL` — beyond limit

Capital projects through the same primitive alongside sector concentration, directional exposure, gross exposure, and Greeks.

---

## Output shape

The canonical per-rule object, reused verbatim by every caller:

```json
{
  "rule": "sector_concentration_tech",
  "status": "PASS",
  "current": 18.3,
  "limit": 25.0,
  "projected_after": 22.1,
  "headroom_remaining": 2.9,
  "unit": "% of portfolio (delta-adjusted)"
}
```

Rule IDs are the canonical names from [rules and limits](rules-and-limits.md). For options and strategies, the library returns the computed greeks (`delta`, `gamma`, `theta`, `vega`) and the resulting `delta_adjusted_exposure` alongside the per-rule list.

---

## Caller orchestration

Each caller composes library output with its own framing:

| Caller | Composition |
|---|---|
| [Guardrail validation tool](state-delivery.md#guardrail-validation-tool) | Tracks cumulative state across calls within an agent invocation; wraps the per-rule list with `overall`, `cumulative_impact_note`, and `failure_guidance` |
| [Proposal pre-processor](../04-decision-layer/proposal-pre-processor.md) | Projects a multi-proposal batch in one call; emits `breaches[]` with signed per-proposal `contributors`; declares the projection's `basis` |
| [Engine guardrail enforcement layer](../05-execution-layer/architecture.md#3-guardrail-enforcement-layer) | Integrates the check into the OMS write transaction; returns the synchronous rejection payload to the PM; persists validation-time greeks as the position's initial greeks |

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — base rule values, profile feature flags, per-rule enforcement classification
- [Regime adaptation](regime-adaptation.md) — multipliers applied to base values for effective-limit resolution
- [Programmatic distillation — quantitative](../02-distillation-layer/external.md) — IV surface input to Black-Scholes
- [Continuous monitor — greeks refresh](../05-execution-layer/architecture.md#4d-greeks-refresh-orchestration) — shares the Black-Scholes model and (after fill) the refreshed greeks
- [Configuration management](../configuration-management.md) — risk-free rate, conservative buffer values per regime
