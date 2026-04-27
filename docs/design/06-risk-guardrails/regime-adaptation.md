# Regime adaptation

Guardrail parameters adjust to market conditions. A fixed limit set is either too tight in calm markets or too loose in volatile ones; regime-adaptive guardrails define multiple parameter sets and switch between them based on the distillation layer's volatility classification.

---

## Regime classification mapping

The distillation layer's volatility regime classification (quant 11f in [external.md](../02-distillation-layer/external.md)) uses VIX level, term structure, VVIX, and realized volatility. The four labels map to parameter sets:

| Regime label | Typical VIX range | Market character | Guardrail philosophy |
|---|---|---|---|
| **Low-vol compression** | VIX < 14 | Calm, trending, low realized vol. Steep VIX contango. Calm markets can break violently. | Slightly loosened limits to capture compressed premium and trending opportunities. |
| **Normal** | VIX 14–22 | Typical volatility. Most of operating time. | Base parameter set from [rules-and-limits.md](rules-and-limits.md). |
| **Elevated** | VIX 22–35 | Above-average volatility. Term structure flattening. Wider daily ranges, more gap risk. | Tightened limits — wider daily ranges mean existing position sizes create more dollar risk. |
| **Crisis** | VIX > 35 | Extreme volatility. VIX backwardation. Correlation spikes, liquidity dries up, severe gap risk. | Heavily tightened limits. Minimal new exposure, capital preservation focus. |

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
| **Thesis-dependency risk flag** | 25% | 28% (×1.12) | 20% (×0.8) | 15% (×0.6) |

**Design rationale for multiplier choices:**

**Low-vol loosening is modest (5–20% wider).** Low-vol environments can break suddenly (VIX historically doubles faster than it halves); modest loosening provides room without creating a position set catastrophic to an overnight spike.

**Elevated tightening is moderate (20–30% tighter).** Reduced sizes and caps; elevated-vol markets still offer opportunities (wider swings create setups), but each dollar carries more risk.

**Crisis tightening is aggressive (40–60% tighter).** Capital preservation mode: gross exposure drops to 60%, position sizes halve, options exposure cut to a fraction. A level-5 conviction thesis remains actionable but sized so even a wrong-footed trade costs a small fraction of portfolio value.

**Drawdown limits don't loosen in low-vol.** Drawdown limits are survival constraints. Loosening them in calm markets creates the scenario where a sudden vol spike hits a system already running deeper drawdown, compounding damage. Drawdown budget is regime-invariant; exposure limits driving daily P/L swings are regime-adaptive.

**Cash reserve increases in crisis.** Min cash rises to 25% — dry powder for recovery trades or margin calls. The most aggressive regime-adaptive change.

---

## Transition mechanics

### Tightening (toward higher vol regime)

**Speed: immediate.** Tighter parameters take effect at the start of the next invocation; no phase-in.

**Rationale:** Tightening is driven by sudden events (selloffs, geopolitical shocks). The system needs tighter limits before the next trades, not gradual tightening while the market falls apart.

**Implementation:** The guardrail layer reads the active regime at invocation start. On tightening, the new parameter set loads and all subsequent checks (state headers, validation tool, engine T3) use it.

### Loosening (toward lower vol regime)

**Speed: gradual over 3 invocations.** Linear interpolation:

| Invocation | Parameter value |
|---|---|
| Transition detected | 33% of the way from old limit to new limit |
| +1 invocation | 67% of the way |
| +2 invocations | Full new (looser) limit |

**Rationale:** Loosening signals are often false — VIX can decline for a day and spike again. Gradual loosening prevents aggressive book expansion on a one-day decline. Three invocations (~6 hours, or 2 sessions overnight) provides confirmation without excessively delaying recovery.

**Implementation:** Active limit = `old_limit + (new_limit - old_limit) × (invocations_since_transition / 3)`. A new tightening during loosening takes effect immediately; tightening always overrides loosening.

### Transition logging

Every transition is logged as a risk/guardrail activity log event with previous regime, new regime, direction, and snapshot of old/new parameter sets. PM and strategist see it in their state headers with a `regime_changed` flag.

---

## Position handling when tightening creates breaches

A position opened at 5% (normal regime, compliant) is immediately in breach when the regime shifts to elevated (3.5% limit). Every tightening transition potentially creates breaches in existing positions.

### Classification: deferred, not immediate

Regime-transition breaches on existing positions are **deferred to the strategist and PM** at the next invocation. The **strategist proposes remedies** (trim, close, or hold) based on thesis strength and constraint proximity; the **PM reviews, adjusts, and executes** via command envelopes. Reasoning:

1. **Positions were compliant when opened.** The breach is caused by regime change, not a new action. Mechanical forced reduction on an entry-time-compliant position is disorienting and potentially value-destroying.
2. **The strategist has thesis-level context the engine doesn't.** A position 90% of the way to its target is different from one moving against thesis; the strategist's ongoing position assessments are best placed to propose which to trim.
3. **Multiple simultaneous breaches.** A normal → elevated shift could put 3–4 positions in breach. The strategist proposes a coordinated package rather than the PM reasoning through each breach independently.
4. **PM execution judgment.** The PM may accept, adjust, reorder, or override based on cross-constraint interactions the strategist doesn't fully see (e.g., trimming A to cure a sector breach would create a directional breach).

### Exception: drawdown-related tightening

If a regime transition coincides with a drawdown breach (often the case — vol spikes cause drawdowns), the drawdown response takes precedence. Engine handles drawdown immediately; strategist and PM handle regime-transition exposure breaches at the next invocation.

### Strategist and PM context for regime-transition breaches

After tightening, breaching positions are flagged in state headers with:

- **Breach type:** `regime_transition`
- **Rule breached and overage:** e.g., "position NVDA at 4.5% of portfolio, elevated regime limit 3.5%, overage 1.0%"
- **Aggregate impact:** total portfolio overage across all regime-transition breaches

**Strategist responsibility:** For each breaching position, propose a remedy — trim to compliance (with quantity), close, or hold with documented thesis rationale. Recommendations are informed by thesis strength, target proximity, and position-level risk/reward.

**PM responsibility:** Reviews each proposal, validates cross-constraint interactions, adjusts sizing or priority, and executes via command envelopes. May override (e.g., close a position the strategist recommended holding if cross-constraint picture demands it). Addresses breaches within the invocation — executing trims/closes, or documenting hold rationale (e.g., "within 2% of target, holding through the transition") in the command envelope for the feedback loop.

### No forced curing deadline

No mechanical deadline for cure. The PM addresses breaches promptly, but the engine doesn't escalate to forced reduction if the PM holds for another invocation. The PM's judgment on trimming a winning position in volatile markets is the kind of decision the system supports.

If a regime-transition-breach position triggers a different immediate-action rule (e.g., position-level max loss), the engine acts on that rule normally. The deferral applies only to the regime-specific exposure breach.

---

## Override conditions

Two categories temporarily modify the active parameter set beyond regime classification:

### Scheduled high-impact events

On days with known catalysts (FOMC, CPI/PPI, major earnings clusters), a **pre-event tightening overlay** applies regardless of current regime:

- Max position size: reduced 20% from active regime value for the 2 invocations preceding the event
- Pending order review: PM prompted to review all pending orders for event sensitivity
- No new positions in the final invocation before the event (PM may override with documented rationale)

Overlay lifts at the first invocation after the event, unless the event triggered a regime transition (regime-adaptive values then apply).

**Implementation:** Pipeline schedule includes a catalog of known event dates (FOMC, economic calendar), updated quarterly. Applied mechanically by calendar, not market conditions.

### Distillation layer anomaly alerts

If funding stress composite or market-wide liquidity score breach alert thresholds (defined in [external.md](../02-distillation-layer/external.md)), a **stress overlay** tightens exposure-related limits (sector concentration, directional exposure, gross exposure) by an additional 15% for the alert duration. Catches stress visible in credit spreads, funding, or liquidity before it manifests as a VIX-level regime change.

---

## Dependencies

- [Rules & limits](rules-and-limits.md) — defines the base rules that regime adaptation modifies
- [Programmatic distillation](../02-distillation-layer/external.md) — section 4 provides the volatility regime classification that drives adaptation
- [Breach behavior](breach-behavior.md) — regime transitions can create breaches; breach behavior defines the response
- [Portfolio state / raw state](../01-data-layer/internal/portfolio-state.md) — category 4d reports the active parameter set and regime label
