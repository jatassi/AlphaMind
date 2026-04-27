# Regime adaptation

Guardrail parameters adjust to market conditions. A fixed limit set is either too tight in calm markets or too loose in volatile ones; regime-adaptive guardrails define multiple parameter sets and switch between them based on the distillation layer's volatility classification.

---

## Regime classification mapping

The distillation layer's volatility regime classification (quant 11f in [external.md](../02-distillation-layer/external.md)) uses VIX level, term structure, VVIX, and realized volatility. The four labels map to parameter sets:

| Regime label | Typical VIX range | Market character | Guardrail philosophy |
|---|---|---|---|
| **Low-vol compression** | VIX < 14 | Calm, trending, low realized vol. Steep VIX contango. Calm markets can break violently. | Slightly loosened limits to capture compressed premium and trending opportunities. |
| **Normal** | VIX 14–22 | Typical volatility. Most of the operating time. | Base parameter set from [rules-and-limits.md](rules-and-limits.md). |
| **Elevated** | VIX 22–35 | Above-average volatility. Term structure flattening. Wider daily ranges, more gap risk. | Tightened limits — wider daily ranges mean existing position sizes create more dollar risk. |
| **Crisis** | VIX > 35 | Extreme volatility. VIX backwardation. Correlation spikes, liquidity dries up, severe gap risk. | Heavily tightened limits. Minimal new exposure, focus on capital preservation. |

---

## Parameter sets per regime

Each regime defines a multiplier applied to the base (normal) parameter value from [rules-and-limits.md](rules-and-limits.md). The multiplier adjusts the limit — values < 1.0 tighten, values > 1.0 loosen.

| Rule | Normal (base) | Low-vol | Elevated | Crisis |
|------|:---:|:---:|:---:|:---:|
| **Per-position max size** | 5% | 6% (×1.2) | 3.5% (×0.7) | 2% (×0.4) |
| **Position-level max loss** | 30% / 80% | 30% / 80% (no change) | 25% / 70% (×0.83 / ×0.88) | 20% / 60% (×0.67 / ×0.75) |
| **Sector concentration** | 25% | 28% (×1.12) | 20% (×0.8) | 15% (×0.6) |
| **Net long exposure** | 60% | 70% (×1.17) | 45% (×0.75) | 30% (×0.5) |
| **Net short exposure** | 30% | 35% (×1.17) | 25% (×0.83) | 20% (×0.67) |
| **Gross exposure** | 120% | 130% (×1.08) | 90% (×0.75) | 60% (×0.5) |
| **Daily drawdown** | 2.5% | 2.5% (no change) | 2.0% (×0.8) | 1.5% (×0.6) |
| **Cumulative drawdown** | 8% | 8% (no change) | 6% (×0.75) | 4% (×0.5) |
| **Options delta exposure** | 40% | 45% (×1.13) | 30% (×0.75) | 15% (×0.38) |
| **Portfolio theta** | 0.15% | 0.18% (×1.2) | 0.10% (×0.67) | 0.05% (×0.33) |
| **Portfolio vega** | 1.0% | 1.2% (×1.2) | 0.7% (×0.7) | 0.3% (×0.3) |
| **Total short exposure** | 30% | 35% (×1.17) | 25% (×0.83) | 15% (×0.5) |
| **Single short max size** | 3% | 3.5% (×1.17) | 2.5% (×0.83) | 1.5% (×0.5) |
| **Borrow cost budget** | 0.05%/day | 0.05% (no change) | 0.04% (×0.8) | 0.02% (×0.4) |
| **Min cash reserve** | 10% | 8% (×0.8) | 15% (×1.5) | 25% (×2.5) |
| **Correlation limit** | 0.70 | 0.75 | 0.60 | 0.50 |

**Design rationale for multiplier choices:**

**Low-vol loosening is modest (5–20% wider).** Low-vol environments can break suddenly (VIX historically doubles faster than it halves); modest loosening provides room without creating a position set catastrophic to an overnight spike.

**Elevated tightening is moderate (20–30% tighter).** The system still operates but with meaningfully reduced sizes and caps. Elevated-vol markets still offer opportunities (wider swings create setups), but each dollar carries more risk.

**Crisis tightening is aggressive (40–60% tighter).** Capital preservation mode: gross exposure drops to 60%, position sizes halve, options exposure cut to a fraction. A level-5 conviction thesis is still actionable but sized so even a wrong-footed trade costs a small fraction of portfolio value.

**Drawdown limits don't loosen in low-vol.** Drawdown limits are survival constraints. Loosening them in calm markets creates the scenario where a sudden vol spike hits a system already running deeper drawdown, compounding damage. The drawdown budget is regime-invariant; the exposure limits that drive daily P/L swings are regime-adaptive.

**Cash reserve increases in crisis.** Min cash rises to 25%, ensuring dry powder for recovery trades or margin calls. The most aggressive regime-adaptive change — in crisis, the system should be mostly in cash.

---

## Transition mechanics

### Tightening (toward higher vol regime)

**Speed: immediate.** Tighter parameters take effect at the start of the next invocation; no phase-in.

**Rationale:** Tightening transitions are driven by sudden events (selloffs, geopolitical shocks). The system needs tighter limits before the next trades, not gradual tightening while the market falls apart.

**Implementation:** The guardrail layer reads the active regime at invocation start. On tightening, the new parameter set loads and all subsequent checks (state headers, validation tool, engine T3) use it.

### Loosening (toward lower vol regime)

**Speed: gradual over 3 invocations.** Linear interpolation:

| Invocation | Parameter value |
|---|---|
| Transition detected | 33% of the way from old limit to new limit |
| +1 invocation | 67% of the way |
| +2 invocations | Full new (looser) limit |

**Rationale:** Loosening signals are often false — VIX can decline for a day and spike again. Gradual loosening prevents aggressive book expansion on a one-day decline. Three invocations (approximately 6 hours, or 2 sessions overnight) provides enough confirmation without excessively delaying recovery.

**Implementation:** Active limit = `old_limit + (new_limit - old_limit) × (invocations_since_transition / 3)`. A new tightening during loosening takes effect immediately; tightening always overrides loosening.

### Transition logging

Every transition is logged as a risk/guardrail activity log event with previous regime, new regime, direction, and snapshot of old/new parameter sets. PM and strategist see it in their state headers with a `regime_changed` flag.

---

## Position handling when tightening creates breaches

The critical interaction: a position opened at 5% of portfolio (normal regime, within limits) is immediately in breach when the regime shifts to elevated (3.5% limit). This is an expected scenario, not an edge case — every tightening transition potentially creates breaches in existing positions.

### Classification: deferred, not immediate

Regime-transition breaches on existing positions are **deferred to the strategist and PM** at the next invocation, not immediately actioned by the engine. The **strategist proposes remedies** (which positions to trim, close, or hold) based on thesis strength and constraint proximity; the **PM reviews, adjusts if necessary, and executes** via command envelopes. The reasoning:

1. **The positions are pre-existing and were compliant when opened.** The breach is caused by the regime change, not by a new action. Mechanical forced reduction on a position that was legally sized at entry is disorienting and potentially value-destroying.
2. **The strategist has thesis-level context the engine doesn't.** A position at 4.5% of portfolio that's 90% of the way to its target is a different situation than one at 4.5% that's moving against the thesis. The strategist — which maintains ongoing position assessments — is best placed to propose which positions to trim and which to hold through the tighter regime.
3. **Multiple positions may breach simultaneously.** A regime shift from normal to elevated could put 3–4 positions into breach. The strategist should propose a coordinated resolution as a package — trimming some, closing others, recommending holds on the highest-conviction positions — rather than the PM reasoning through each breach independently.
4. **The PM provides the execution judgment layer.** The PM may accept the strategist's recommendations as-is, adjust sizing, reorder priority, or override a recommendation based on cross-constraint interactions the strategist doesn't fully see (e.g., trimming position A to cure a sector breach would create a directional exposure breach).

### Exception: drawdown-related tightening

If a regime transition coincides with a drawdown limit breach (which it often will — volatility spikes cause drawdowns), the drawdown breach response takes precedence. The engine handles the drawdown breach per its classification (immediate action), and the strategist and PM handle the regime-transition exposure breaches at the next invocation (strategist proposes remedies, PM reviews and executes).

### Strategist and PM context for regime-transition breaches

When guardrail state headers are generated after a regime tightening, any positions that now breach the tighter limits are flagged with:

- **Breach type:** `regime_transition`
- **Rule breached and overage:** e.g., "position NVDA at 4.5% of portfolio, elevated regime limit 3.5%, overage 1.0%"
- **Aggregate impact:** total portfolio overage across all regime-transition breaches

**Strategist responsibility:** The strategist sees these breaches in its guardrail state header alongside its ongoing position assessments. For each breaching position, the strategist proposes a remedy action — trim to compliance (with specific quantity), close entirely, or hold with documented thesis rationale. The strategist's recommendations are informed by thesis strength, target proximity, and position-level risk/reward — context the PM doesn't have at the same depth.

**PM responsibility:** The PM receives the strategist's remedy proposals alongside the breach data. The PM reviews each proposal, validates it against cross-constraint interactions (e.g., would trimming this position create a secondary breach?), adjusts sizing or priority as needed, and executes via command envelopes. The PM may also override a strategist recommendation — for example, closing a position the strategist recommended holding if the cross-constraint picture demands it.

The PM is expected to address these breaches within the invocation — either executing the strategist's recommended trims/closes, or documenting a specific rationale for holding (e.g., "position is within 2% of target price, will resolve within 4 hours, holding through the regime transition"). Documented hold rationale is logged in the command envelope for the feedback loop.

### No forced curing deadline

There is no mechanical deadline by which regime-transition breaches must be cured. The PM is expected to address them promptly, but the engine does not escalate to forced reduction if the PM holds a breaching position for an additional invocation. The rationale: the PM's judgment about whether to trim a winning position in a volatile market is exactly the kind of decision the system is designed to support, not override.

However: if a regime-transition breach position subsequently triggers a different immediate-action rule (e.g., position-level max loss), the engine acts on that rule as normal. The regime-transition deferral applies only to the regime-specific exposure breach, not to other rules.

---

## Override conditions

Two categories of market events can temporarily modify the active parameter set beyond what regime classification dictates:

### Scheduled high-impact events

On days with known high-impact catalysts — FOMC rate decisions, CPI/PPI releases, major earnings clusters — the system applies a **pre-event tightening overlay** regardless of the current volatility regime:

- Max position size: reduced by 20% from active regime value for the 2 invocations preceding the event
- Pending order review: the PM receives a prompt to review all pending orders for sensitivity to the upcoming event
- No new positions in the final invocation before the event (PM may override with documented rationale)

The overlay lifts at the first invocation after the event, unless the event triggered a regime transition (in which case the regime-adaptive values apply).

**Implementation:** The pipeline schedule includes a catalog of known event dates (FOMC schedule, economic calendar). The catalog is updated quarterly. The overlay is applied mechanically based on the calendar, not based on market conditions.

### Distillation layer anomaly alerts

If the distillation layer's funding stress composite or market-wide liquidity score breach their alert thresholds (defined in [external.md](../02-distillation-layer/external.md)), the system applies a **stress overlay** that tightens the active parameter set by an additional 15% on exposure-related limits (sector concentration, directional exposure, gross exposure) for the duration of the alert condition. This catches stress conditions that haven't yet manifested as VIX-level regime changes but are visible in credit spreads, funding markets, or liquidity drying up.

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — defines the base rules that regime adaptation modifies
- [Programmatic distillation](../02-distillation-layer/external.md) — section 4 provides the volatility regime classification that drives adaptation
- [Breach behavior](breach-behavior.md) — regime transitions can create breaches; breach behavior defines the response
- [Portfolio state / raw state](../01-data-layer/internal/portfolio-state.md) — category 4d reports the active parameter set and regime label
