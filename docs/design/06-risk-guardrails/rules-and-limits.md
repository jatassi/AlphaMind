# Rules & limits

Concrete risk constraints. Each rule has a default value, a portfolio-grounded rationale, and an enforcement-layer specification.

**Dual portfolio approach:** Two parallel portfolios validate at deployment scale and full-capability scale. Guardrail percentages apply identically; differences are in available features and practical position counts. See [dual portfolio profiles](#dual-portfolio-profiles).

- **Primary portfolio ($1,500):** Validates strategy at real deployment capital.
- **Full-system portfolio ($100,000):** Validates complete system — options, multi-sector diversification, full position counts.

**Risk philosophy:** Moderate — balance return capture with capital preservation. Survive a bad week without existential damage while retaining freedom for high-conviction opportunities.

---

## Enforcement tier model

Every rule is checked at one or more of three additive tiers — a T3 rule is also surfaced at higher layers as context. Between T1 and T2, the [proposal pre-processor](../04-decision-layer/proposal-pre-processor.md) adds cross-agent annotations (cumulative exposure, capital requirements) catching breaches in the combined proposal set — see the [guardrails README](README.md).

| Tier | Layer | Style | Meaning |
|------|-------|-------|---------|
| T1 | Analyst / Strategist | **Advisory** | Headroom in state header. Agent self-constrains and validates against the [guardrail validation tool](state-delivery.md#guardrail-validation-tool). Violations caught downstream. |
| T2 | Portfolio Manager | **Judgment-informed** | Headroom surfaced alongside pre-processor annotations. PM validates its own modifications via the same tool. Makes sizing tradeoffs but cannot override engine limits. |
| T3 | Engine | **Authoritative** | Deterministic check on every OMS command. Hard rejection, no override. Rejections return synchronously with per-rule detail and suggested modification. |

A rule's "enforcement tier" below means which tiers check it. T3 rules are mechanically enforced; T1/T2-only rules rely on agent judgment with tool support.

---

## Position size limits

### Per-position maximum

| Parameter | Default | Unit |
|-----------|---------|------|
| Max position size (% of portfolio) | 5% | Portfolio value |
| Max position size (absolute) | $5,000 | USD at $100K portfolio |
| Max premium at risk (options) | 5% | Portfolio value |

**Enforcement:** T1 + T2 + T3

**Rationale:** Aligns with the conviction scale's top sizing band (level 5: 3–5%). A total loss on a max-sized equity position costs 5% — painful but not catastrophic. At 5%, up to 20 max-sized positions are possible; most will be 1–3% (conviction 2–4). "Premium at risk" applies to options since the entire premium can be lost.

**Concretely:** At $100K, max-size equity = $5,000 (~6 shares of NVDA at $850, ~13 of AVGO at $380). Max-size options = $5,000 premium (5 contracts of a $10 option). Conviction 2–3 positions land at $1,000–3,000.

### Position-level maximum loss

| Parameter | Default | Unit |
|-----------|---------|------|
| Max unrealized loss (equity) | 30% | Cost basis |
| Max unrealized loss (options) | 80% | Premium paid |
| Max unrealized loss (strategies) | 80% | Net premium / max risk |

**Enforcement:** T3 only (engine backstop)

**Rationale:** Capital protection complementing thesis-based bracket stops. Bracket stops trigger on underlying price (thesis invalidation); this guardrail triggers on position P/L (mechanical loss control). Separation matters most for options, where IV crush, theta decay, or gamma effects erode value without an underlying move sufficient to trigger price-based stops.

The 30% equity threshold is wider than typical bracket stops (5–15% of underlying price) — a backstop for cases where bracket stops fail or gap through. The 80% options threshold acknowledges higher options variance; an 80% loss on a $2,000 position is $1,600, survivable.

---

## Sector concentration limits

| Parameter | Default | Unit |
|-----------|---------|------|
| Max exposure per sector | 25% | Portfolio value (delta-adjusted) |
| Sectors | Tech, Semis, Financials, Energy | Per [asset-universe.md](../asset-universe.md) |

**Enforcement:** T1 + T2 + T3

**Rationale:** 4 sectors × 25% = 100% theoretical max, but the gross exposure limit and practical sizing keep actual utilization lower. At 25%, a sector holds 5+ positions at typical sizing (3–5% each), allowing multi-thesis sector views while preventing single-sector bets. Semiconductors in particular generate clusters of correlated setups (supply chain linkages).

Delta-adjusted exposure is used rather than notional. An OTM call at 0.3 delta contributes 30% of notional, not 100% — preventing options from consuming disproportionate sector headroom while reflecting actual directional risk.

---

## Directional exposure limits

| Parameter | Default | Unit |
|-----------|---------|------|
| Max net long exposure | 60% | Portfolio value (delta-adjusted) |
| Max net short exposure | 30% | Portfolio value (delta-adjusted) |

**Enforcement:** T1 + T2 + T3

**Rationale:** The asset universe and time horizon create a natural long bias — most high-information-density setups in tech, semis, and financials are directional long theses. The 60% long cap allows a meaningful directional book while ensuring at least 40% remains as cash, offsetting shorts, or unused capacity. The 30% short cap is tighter because (a) short positions have theoretically unlimited loss, (b) short squeezes create outsized drawdowns on the 4–72 hour horizon, and (c) borrow risk adds a non-thesis-related source of forced exits.

The asymmetry reflects equities drifting upward over time, making sustained net short portfolios a structural headwind. The system shorts individual names on conviction; it does not run a net short portfolio.

---

## Gross exposure limit

| Parameter | Default | Unit |
|-----------|---------|------|
| Max gross exposure | 120% | Portfolio value (delta-adjusted) |

**Enforcement:** T1 + T2 + T3

**Rationale:** Gross = total long + total short. The 120% limit allows up to 1.2× capital deployed through longs, shorts, and options leverage. At $100K, $120K of delta-adjusted exposure. Provides room for paired trades (long + short in the same sector) without excessive leverage. The 20% headroom above 100% exists because options create fractional leverage — a small premium buys significant delta exposure, and the system should hold options alongside equity positions without constantly bumping the ceiling.

Gross is the simplest leverage proxy. 100% long / 0% short is fully invested with no leverage. 60% long / 40% short is 100% gross (no leverage) with significant directional positions. 80% long / 40% short is 120% gross — fully utilizing the limit.

---

## Drawdown limits

### Daily drawdown

| Parameter | Default | Unit |
|-----------|---------|------|
| Max daily drawdown | 2.5% | Day's opening equity |

**Enforcement:** T3 (engine-authoritative, triggers halt mode — see [breach-behavior.md](breach-behavior.md))

**Rationale:** At $100K, 2.5% = $2,500. The hardest constraint in the system — the line where mechanical risk reduction takes priority over thesis reasoning. On the 4–72 hour horizon, a 2.5% daily loss represents a significant adverse event suggesting either the market is doing something the system's theses didn't anticipate, or multiple theses are wrong simultaneously. Either way, adding new risk is wrong.

Calibrated to be painful but not panic-inducing — roughly 2× the expected daily P/L volatility of a moderately positioned portfolio.

### Cumulative drawdown

| Parameter | Default | Unit |
|-----------|---------|------|
| Max cumulative drawdown | 8% | High-water mark |

**Enforcement:** T3 (engine-authoritative, triggers progressive risk reduction — see [breach-behavior.md](breach-behavior.md))

**Rationale:** At $100K, 8% = $8,000 from peak equity. A second, longer-term safety net beyond the daily limit. A system losing 1.5% on four consecutive days (each under the daily limit) would be down 6% — close to the cumulative limit. Forces strategic reassessment before losses compound into a recovery-impairing drawdown. Recovery from 8% requires ~8.7% gain — challenging but achievable over weeks of good execution.

---

## Correlation limit

| Parameter | Default | Unit |
|-----------|---------|------|
| Max portfolio pairwise correlation (weighted avg) | 0.70 | Pearson, 20-day trailing |

**Enforcement:** T1 + T2 (advisory and PM-judgment — **not** T3)

**Rationale:** Excluded from T3 because correlation estimates are inherently noisy — a 20-day trailing correlation can shift significantly with a few days of data, and blocking trades on a noisy estimate creates more problems than it solves. Surfaced to the analyst (avoid adding correlated positions) and PM (assess structural vs. transient correlation).

The 0.70 threshold flags portfolios where "diversification" is illusory — positions nominally spanning multiple names or sectors that actually move together. Particularly relevant for the tech/semis pair, where AI-narrative-driven names can temporarily correlate at 0.8+ despite being in different sectors.

Weighted average uses position weight as the weighting factor, so larger positions contribute more. A small speculative position in a correlated name is less concerning than a full-sized one.

---

## Options-specific limits

### Delta-adjusted exposure

| Parameter | Default | Unit |
|-----------|---------|------|
| Max delta-adjusted exposure from options | 40% | Portfolio value |

**Enforcement:** T1 + T2 + T3

**Rationale:** Options provide implicit leverage — a $2,000 premium can control $50,000 of underlying exposure. The delta-adjusted limit bounds effective directional exposure from options even when premium is modest. At 40%, options provide significant directional exposure without dominating the portfolio's risk profile. Independent of per-sector and gross exposure limits.

### Theta exposure

| Parameter | Default | Unit |
|-----------|---------|------|
| Max daily theta (portfolio-wide) | 0.15% | Portfolio value |

**Enforcement:** T1 + T2 + T3

**Rationale:** At $100K, 0.15% = $150/day in time decay. Theta is a guaranteed daily cost for long options. On the 4–72 hour horizon, excessive theta creates a headwind requiring larger moves to overcome. The 0.15% cap limits portfolio bleed to ~3%/month from time decay alone — meaningful position-level theta without structural drag.

### Vega exposure

| Parameter | Default | Unit |
|-----------|---------|------|
| Max absolute vega exposure | 1.0% | Portfolio value per 1-point IV move |

**Enforcement:** T1 + T2 + T3

**Rationale:** At $100K, 1.0% means a 1-point IV move creates a $1,000 P/L swing. Caps sensitivity to IV changes, especially relevant around events (earnings, FOMC) where IV can crush 5+ points overnight. At the cap, a 5-point IV crush costs at most $5,000 (5% of portfolio) — painful but within the cumulative drawdown envelope.

---

## Short-specific limits

| Parameter | Default | Unit |
|-----------|---------|------|
| Max total short exposure | 30% | Portfolio value |
| Max single short position | 3% | Portfolio value |
| Max daily borrow cost (all shorts) | 0.05% | Portfolio value |

**Enforcement:** T1 + T2 + T3

**Rationale:** Short exposure capped at 30% (matching the net short directional limit) because short positions carry asymmetric risk — theoretically unlimited losses, and short squeezes can cause 20%+ moves in a single session. The per-position cap (3%) is tighter than the general 5% because a short squeeze moves faster than a long-side decline.

The borrow cost budget (0.05% of portfolio/day = $50/day at $100K) prevents accumulating expensive-to-borrow shorts that bleed cost. At an 18% annualized borrow rate, $50/day supports ~$100K in short exposure — well above the 30% gross short limit. Binds only for hard-to-borrow names with 50%+ annualized rates, the intended behavior.

---

## Capital sufficiency

| Parameter | Default | Unit |
|-----------|---------|------|
| Minimum cash reserve | 10% | Portfolio value |
| Max reserved capital for pending orders | 30% | Portfolio value |

**Enforcement:** T3 (engine-authoritative)

**Rationale:** The 10% cash reserve ensures dry powder for high-conviction opportunities and margin requirements. 100% deployment is fragile — a single margin call or unexpected opportunity with no available capital is a structural failure. The 30% pending order cap prevents over-committing capital to limit orders that may never fill.

---

## Per-rule enforcement summary

| Rule | T1 (Advisory) | T2 (PM Judgment) | T3 (Engine) | Breach detection between invocations |
|------|:-:|:-:|:-:|---|
| Per-position max size | ✓ | ✓ | ✓ | N/A (command-time only) |
| Position-level max loss | — | — | ✓ | ✓ Continuous monitor |
| Sector concentration | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Net long exposure | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Net short exposure | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Gross exposure | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Daily drawdown | — | ✓ (context) | ✓ | ✓ Continuous monitor |
| Cumulative drawdown | — | ✓ (context) | ✓ | ✓ Continuous monitor |
| Correlation | ✓ | ✓ | — | — |
| Options delta exposure | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Portfolio theta | ✓ | ✓ | ✓ | — (changes only on position entry/exit) |
| Portfolio vega | ✓ | ✓ | ✓ | — (changes only on position entry/exit) |
| Total short exposure | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Single short max size | ✓ | ✓ | ✓ | ✓ Continuous monitor |
| Borrow cost budget | ✓ | ✓ | ✓ | — (accrual-based, checked daily) |
| Min cash reserve | — | ✓ (context) | ✓ | — |
| Pending order capital | — | — | ✓ | N/A (command-time only) |

**"Breach detection between invocations"** indicates whether the continuous monitor checks this rule against live market data between invocations. ✓ rules can be breached by market movement (price changes shift exposure, drawdowns accumulate); N/A and — rules are only relevant at command-submission time or change too slowly to require continuous monitoring.

---

## Portfolio size profiles

Guardrail rules don't scale uniformly across portfolio sizes. [Scenario testing](scenario-tests.md) revealed that at small portfolios, percentage-based exposure and drawdown rules are structurally inert (the portfolio can't deploy enough capital to reach them); at large portfolios, structural constraints like position count are redundant (exposure rules prevent over-deployment). What constrains the portfolio changes categorically with scale — small portfolios are constrained by economics (minimum viable position size, transaction cost proportionality, feature viability), large portfolios by exposure (concentration, leverage, correlation, drawdown).

Each profile defines which features are enabled, which structural constraints apply, and which of the 17 rules are **binding** (active risk protection at this scale), **active but non-binding** (enforced for completeness but unlikely to trigger under normal operation), or **disabled** (feature unavailable, rule omitted from guardrail state headers). Independent of enforcement tier — a binding rule is still checked at all its designated tiers.

One profile runs at a time during both paper trading and live trading. Profiles are validated sequentially: paper trade at a tier → tune until profitable → deploy real capital → accumulate results → paper trade at the next tier. Tier graduation is gated on demonstrated profitability with real capital at the current tier, not paper trading results. Each tier's guardrail profile is informed by real market feedback from the previous tier.

### Profile: Micro ($1,500)

**Purpose:** First deployment tier. Risk management priority at this scale is **signal quality** — maximum volume of clean, attributable thesis outcomes to determine whether the analytical framework produces positive expectancy. Capital preservation is secondary; structural constraints and bracket stops provide sufficient loss protection at this scale without dedicated exposure management machinery.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `false` | Even a $1 option contract ($100 premium) is 6.7% of portfolio. Impractical. |
| `short_selling_enabled` | `false` | Margin requirements consume too much capital at this scale. |
| `fractional_shares_required` | `true` | A single NVDA share (~$133) is 8.9% of portfolio — over the position limit without fractional shares. |
| `active_sectors` | `[tech, semis]` | The two highest-signal sectors. Broader coverage validated at the small tier. |
| `max_concurrent_positions` | None | **No cap.** Priority is signal volume — more concurrent theses means faster statistical convergence. With $1,350 deployable and $50 min position size, theoretical max is 27 positions. In practice, conviction 2–4 sizing at $50–75 yields 18–22 positions deploying 60–90% of capital. Exposure rules (sector concentration, net long, gross) provide the structural ceiling. |
| `min_position_size` | `$50` | 3.3% of portfolio. A 5–10% thesis move produces $2.50–5.00 of measurable P/L; bid-ask friction negligible on asset universe names. Represents live-viable economics. |

**Rules — what's in the profile:**

The micro profile carries only rules that serve signal quality — protecting data quality (clean trade outcomes) or preventing portfolio destruction before enough data is collected.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($75) | Prevents over-concentration in a single thesis. With no position count cap, the primary constraint on per-trade risk. Micro is about thesis validation volume, not sophisticated sizing. |
| Position-level max loss | 30% | Bracket stop backstop. Every trade has a clean exit → clean data. A position drifting to -50% without being stopped is a data quality failure. |
| Sector concentration | 25% | With no position count cap and 2 active sectors, the analyst could cluster heavily in tech or semis. The cap prevents single-sector bets, which compromise signal diversity — 20 tech positions resolving against the same catalyst aren't 20 independent data points. |
| Net long exposure | 60% | With 18–22 potential positions, the portfolio can deploy 60–90% of capital. The net long cap prevents going all-in directionally. At 60% ($900 deployed), a 5% broad decline costs $45 (3% of portfolio). |
| Gross exposure | 120% | Redundant with net long at micro (no shorts means gross = net long), included for profile consistency. Will bind if shorts are enabled. |
| Daily drawdown | 2.5% ($37.50) | With 60% deployment ($900), a 4.2% decline across positions triggers halt. Reachable in a severe selloff. Protects against a broad market downturn hitting 18 correlated positions simultaneously. |
| Cumulative drawdown | 8% ($120) | Reachable over a bad week with higher deployment. At $900 deployed, 13.3% aggregate loss hits the 8% portfolio drawdown. Progressive response (reduced sizing, flagged losers) is testable. |
| Correlation | 0.70 | With 18–22 positions across only 2 structurally correlated sectors (AI narrative, supply chain linkages), correlation is a genuine concern. The limit pushes the analyst to find differentiated theses rather than 15 variations of the same trade. |
| Min cash reserve | 10% ($150) | Capital for new opportunities as existing theses resolve. With higher deployment, a real constraint on total position count. |
| Pending order capital | 30% ($450) | Pending limit orders across many concurrent positions could tie up significant capital. The cap prevents over-commitment to entries that may never fill. |

**Rules — what's NOT in the profile:**

| Rule | Why it's excluded |
|---|---|
| Options rules (delta, theta, vega) | Options disabled. |
| Short rules (total, single, borrow) | Shorts disabled. |
| Net short exposure | Shorts disabled. |

**Thesis performance review trigger:** Beyond the standard rules, the micro profile includes a **thesis performance review** firing every 20 completed trades, or when cumulative realized losses exceed 15% of starting capital ($225), whichever comes first. Not a mechanical halt — a system pause where the operator evaluates the track record (win rate, average gain vs. loss, thesis accuracy by sector, bracket stop effectiveness, timing accuracy). Answers "is the framework generating positive expectancy?" before too much capital is consumed by a broken approach. Positive review → resume. Negative → operator adjusts thesis framework, analyst prompt, or bracket configuration before continuing.

**Capital utilization:** 60% max deployment ($900) constrained by the net long exposure limit, producing proportional P/L data per unit time.

**What this profile validates:** Thesis quality at volume (20+ concurrent theses), PM decision quality under load (5–10 proposals per invocation), bracket architecture, timing, sector concentration management, correlation management within a 2-sector universe, daily and cumulative drawdown mechanics.

**What this profile does NOT validate:** Options, shorts, 4-sector diversification, regime adaptation under extreme exposure, margin management.

**Broker requirement:** Must support fractional shares. Commission-free strongly preferred.

### Profile: Small ($5,000–$15,000)

**Purpose:** Second deployment tier. Risk management priority is **concentration management** — thesis quality is proven at micro; the system now scales up with more positions, a third sector, and (at the upper end) options. The danger is correlated failure modes emerging as complexity grows: five tech positions that all unwind on the same catalyst, or options greeks concentrating risk in dimensions the equity-only micro tier never tested. Every rule in this profile measures or constrains concentration, or protects against compounding losses concentrated portfolios produce.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `false` at $5K / `true` at $10K+ | At $5K, a $250 option premium is 5% — at the per-position limit with no room for error. At $10K+, options become practical: $500 premium is 5%, $200 is 2%. Options introduce three new concentration dimensions (delta, theta, vega) directly related to the tier's priority. Exact threshold TBD during paper trading. |
| `short_selling_enabled` | `false` | Margin requirements still consume disproportionate capital. Shorts introduce directional complexity (gross vs. net, hedge interactions) belonging at medium where the priority shifts to full exposure management. |
| `fractional_shares_required` | `true` at $5K / `false` at $10K+ | At $5K, many asset universe names require fractional shares for compliant sizing. At $10K+, most positions support whole shares; fractional remains available. |
| `active_sectors` | `[tech, semis, financials]` | Third sector enabled — the first genuine diversification against the structurally correlated tech/semis pair. The analyst can construct portfolios where a tech selloff doesn't hit every position simultaneously. Energy added at medium tier. |
| `max_concurrent_positions` | None | **No cap.** Sector concentration (25%) and correlation (0.70) directly constrain concentration. A position count cap is an indirect proxy. With $3K–9K deployable (60% net long cap) and $75 min, exposure rules naturally limit position count to ~8–12. |
| `min_position_size` | `$75` | At $5K, 1.5% of portfolio; at $15K, 0.5%. Higher than micro's $50 floor because each position should justify thesis-tracking overhead across the strategist, PM, and context packages. At $15K, a $75 position produces $3.75–7.50 of P/L on a 5–10% move. |

**Rules — what's in the profile:**

The small profile carries rules serving concentration management: measuring diversification, constraining greeks concentration from newly-enabled options, and protecting against compounding losses during correlated selloffs.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($250–750) | Prevents single-thesis dominance. At $15K, a 5% position is $750; a 30% bracket stop loss costs $225. The cap forces diversification across theses. |
| Position-level max loss | 30% / 80% (options) | Bracket stop backstop for equity. First tier where the 80% options threshold is relevant (options enabled at $10K+). A $500 options position losing 80% = $400 — real money. |
| Sector concentration | 25% | **Core concentration constraint.** With 3 active sectors and no position count cap, the analyst could load into tech. At $15K, 25% = $3,750/sector — room for 3–5 positions per favored sector. Forces looking beyond the obvious sector when it fills. |
| Net long exposure | 60% | Caps total deployment. At $15K, 60% = $9,000. With 8–12 positions, a broad selloff hitting the full book can cost $450–900 (3–6%) on a 5–10% decline. Ensures cash buffer for correlated losses without existential damage. |
| Gross exposure | 120% | Redundant with net long while shorts disabled (gross = net long). Included for profile consistency; binds at medium when shorts are enabled. |
| Daily drawdown | 2.5% | At $15K with 60% deployment ($9,000), a 4.2% decline triggers halt. With 8–12 positions across only 3 sectors, a correlated sector selloff can reach this. A broad tech crash hitting 8 tech/semi positions simultaneously is exactly the concentrated failure mode this tier manages. |
| Cumulative drawdown | 8% | At $15K, 8% = $1,200. With $9,000 deployed, a 13.3% aggregate loss hits the limit. Reachable over a bad week. Progressive response (reduced sizing at 8%, further at 10%, full halt at 12%) is testable. |
| Correlation | 0.70 | **Core concentration constraint.** With 8–12 positions across 3 sectors, weighted average pairwise correlation is the most direct measure of genuine diversification. Tech/semis structurally correlated; financials provide diversification. If >0.70, the analyst needs differentiated theses or the PM rejects redundant proposals. The reason the third sector exists in this profile. |
| Options delta exposure | 40% (when enabled) | At $10K+, options introduce delta concentration risk equity-only portfolios don't have. Three long calls in tech names stack correlated delta. The cap prevents options delta from dominating the portfolio's directional profile. |
| Portfolio theta | 0.15%/day (when enabled) | At $10K, 0.15% = $15/day. A single 30DTE option carries $5–10/day theta; two positions approach the limit. Theta compounds silently and can drag the portfolio into drawdown without adverse price movement. |
| Portfolio vega | 1.0%/pt (when enabled) | At $10K, 1.0% = $100/pt. With 2–3 options positions, a 5-point IV crush around an event costs up to $500 (5%). The cap prevents the portfolio from becoming a volatility bet when the intent is directional thesis validation. |
| Min cash reserve | 10% | At $15K, $1,500 reserve. Capital for new positions as theses resolve; buffer against margin requirements if options enabled. |
| Pending order capital | 30% | With more concurrent positions and limit orders, prevents over-commitment to entries that may never fill. |

**Rules — what's NOT in the profile:**

| Rule | Why it's excluded |
|---|---|
| Net short exposure | Shorts disabled. |
| Total short exposure | Shorts disabled. |
| Single short max size | Shorts disabled. |
| Borrow cost budget | Shorts disabled. |

**What this profile validates:** Concentration management across 3 sectors, correlation-aware portfolio construction, options greeks management at introductory scale, daily drawdown halt under correlated selloff, cumulative drawdown progressive response, sector concentration as an active constraint, the PM's ability to reject redundant proposals.

**What this profile does NOT validate:** Short selling, 4-sector diversification, gross exposure management with a long+short book, margin management, regime adaptation under extreme exposure (meaningful at medium+).

### Profile: Medium ($25,000–$50,000)

**Purpose:** Third deployment tier. Risk management priority is **exposure management** — full feature set online (shorts, options, 4 sectors), with enough capital deployed that aggregate risk interactions dominate. A portfolio with 10+ positions across longs, shorts, and options has risk properties not visible at the individual position level — directional tilt, leverage, greeks concentration, cross-sector correlation, and their interaction effects. Rules orient around measuring and constraining aggregate risk profile.

First tier where **regime adaptation is consequential**. Crisis mode cutting gross from 120% to 60% when running at 85% gross forces real position reductions. The regime adaptation machinery — immediate tightening, gradual loosening, strategist → PM deferral for transition breaches — gets its first genuine workout.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `true` | At $25K, $1,250 max options position (5% premium at risk) supports single-leg strategies; multi-leg viable at $50K. Options are a portfolio construction tool — defined-risk directional exposure and event plays. |
| `short_selling_enabled` | `true` | **Defining feature expansion of this tier.** Margin manageable at $25K+. The 30% short cap allows $7,500–15,000 (2–5 meaningful shorts). Long/short interaction creates the first real distinction between gross and net exposure (e.g., 55% long + 20% short = 75% gross / 35% net). |
| `fractional_shares_required` | `false` | Position sizes large enough for whole shares in all asset universe names. |
| `active_sectors` | `[tech, semis, financials, energy]` | Full 4-sector coverage. Energy added here because the short book creates new hedging dynamics — energy often moves inversely to tech during certain macro regimes. |
| `max_concurrent_positions` | None | Exposure rules are the binding constraints. With 25% sector cap × 4 sectors and per-position sizing, position count naturally lands at 10–15. |
| `min_position_size` | `$75` | At $25K, 0.3% of portfolio; at $50K, 0.15%. Below this, thesis-tracking overhead isn't justified. The system may legitimately want smaller positions as part of a hedging strategy. |

**Rules — what's in the profile:**

All 17 rules are present and binding. Justifications below focus on what's new or different from the small tier.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($1,250–2,500) | Full-sized positions where a 30% bracket stop loss costs $375–750. The cap prevents any single thesis from dominating aggregate exposure. |
| Position-level max loss | 30% / 80% | Both thresholds active. A $2,500 equity position hitting the 30% backstop costs $750; a $1,250 options position hitting 80% costs $1,000. The backstop does real capital protection work. |
| Sector concentration | 25% | At $50K, $12,500/sector — room for 3–5 positions at typical sizing. With 4 sectors, the constraint forces genuine cross-sector diversification. |
| Net long exposure | 60% | **Core exposure management constraint.** At $50K, 60% = $30,000 max long deployment. With shorts providing partial offset, the system can hold more gross long while staying within the net limit (e.g., $35K long + $5K short = 60% net, 80% gross). First tier where net vs. gross creates meaningful portfolio construction decisions. |
| Net short exposure | 30% | **New at this tier.** Caps the short book at $7,500–15,000. Tighter than the 60% long limit because shorts carry asymmetric risk (theoretically unlimited loss, squeeze risk, borrow recall). 3–5 shorts — enough for pair trades and directional hedging, not a net-short portfolio. |
| Gross exposure | 120% | **First tier where it binds independently of net long.** 60% net long + 30% net short = 90% gross — within limit. Add options delta overlay and the ceiling approaches. At $50K, 120% = $60K gross — significant leverage. Prevents simultaneously building a large long book, a large short book, and a large options overlay. |
| Daily drawdown | 2.5% ($625–1,250) | At $50K with $35K deployed, a 3.6% decline triggers halt. Realistic during elevated volatility — fires often enough to be a regular feature of system operation. |
| Cumulative drawdown | 8% ($2,000–4,000) | All progressive tiers reachable: 8% ($4K) → tier 1; 10% ($5K) → tier 2; 12% ($6K) → full halt. A bad week pushes through tier 1 into tier 2. Full progressive mechanism gets a real workout. |
| Correlation | 0.70 | With 10–15 positions across 4 sectors plus a short book, correlation structure is complex. Long/short pairs within a sector may have high pairwise correlation by design (the hedge). The weighted average needs to account for this — a long NVDA / short AMD pair is high-correlation but low directional risk. The PM understands correlation in the context of directional exposure. |
| Options delta exposure | 40% | At $50K, 40% = $20K from options alone. Combined with equity, total directional exposure can reach the net long limit primarily through options leverage — making the risk profile more sensitive to gamma and IV than it appears. The cap ensures the PM consciously manages this. |
| Portfolio theta | 0.15%/day ($37.50–75) | At $50K, $75/day max = $1,500/month if fully consumed — a 3% monthly headwind. Multiple options positions can approach the limit, especially pre-event. Active theta management (rolling, closing, offsetting) becomes a real PM responsibility. |
| Portfolio vega | 1.0%/pt ($250–500/pt) | At $50K, a 5-point IV crush costs up to $2,500 (5%) at the limit. Around FOMC or earnings, IV can crush 5–10 points overnight. Interacts with pre-event tightening overlay. |
| Total short exposure | 30% | Aggregate short book limit. Prevents a short book that looks manageable position-by-position but carries significant aggregate squeeze risk — a sector-wide short squeeze hitting 3 correlated shorts can far exceed what individual position limits suggest. |
| Single short max size | 3% ($750–1,500) | Tighter than the 5% general cap because short squeezes are faster and more severe than long-side declines. A 30% adverse move on a 3% short costs $450 — painful but survivable. |
| Borrow cost budget | 0.05%/day ($12.50–25) | Prevents accumulating expensive-to-borrow shorts. $25/day supports ~$50K in shorts at 18% annualized — above the 30% limit. Binds only for hard-to-borrow names (50%+ annualized). |
| Min cash reserve | 10% ($2,500–5,000) | With shorts consuming margin and options requiring premium, ensures capital for margin calls, new opportunities, operational buffer. |
| Pending order capital | 30% ($7,500–15,000) | With 10–15 positions, pending limit orders in both directions can tie up significant capital. Forces the PM to prioritize. |

**Rules — what's NOT in the profile:**

None. All 17 rules are binding. First profile where no rules are excluded.

**Regime adaptation at medium:** First tier where regime transitions create significant constraint pressure. Normal regime: portfolio at 55% net long and 85% gross. Elevated tightens net long to 45% and gross to 90% — net long now binds, requiring reductions. Crisis tightens to 30% / 60% — roughly halving operating capacity. Strategist → PM deferral, emergency invocation trigger for regime jumps, and gradual loosening on recovery are all meaningfully exercised. Pre-event tightening overlay compounds with regime parameters.

**What this profile validates:** Full guardrail architecture under real constraint pressure. All 17 rules binding. Regime adaptation with meaningful parameter changes. Drawdown halt and progressive response under realistic conditions. Short book management (squeeze risk, borrow costs, margin). Long/short interaction effects. Options greeks management at scale. 4-sector correlation management. Cross-constraint interactions. Emergency invocation under regime jumps. PM ability to manage a complex multi-dimensional risk profile.

### Profile: Large ($100,000+)

**Purpose:** Fourth deployment tier. Risk management priority is **exposure management plus execution quality**. Rule set and feature flags identical to medium — all 17 rules, all features enabled, same percentages. What changes: market impact becomes meaningful, and real dollar losses carry psychological weight.

A $5,000 position (5% of $100K) in a mid-cap name is no longer invisible to the order book. The [paper-evaluation harness](../05-execution-layer/paper-evaluation-harness.md)'s slippage and impact estimates — conservative-but-ignorable at medium sizing — produce meaningful live-execution drag at large. A $2,500 daily drawdown halt is the same percentage as medium's $1,250 halt, but the absolute number tests whether the system (and operator) maintain discipline when losses feel larger.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `true` | Full options capability including multi-leg strategies. 5% premium at risk = $5,000 — comfortable for any strategy. |
| `short_selling_enabled` | `true` | Full short book. 30% exposure = $30K — supports 5–10 positions. |
| `fractional_shares_required` | `false` | Position sizes comfortably above whole-share thresholds for all asset universe names. |
| `active_sectors` | `[tech, semis, financials, energy]` | Full 4-sector coverage, same as medium. |
| `max_concurrent_positions` | None | Exposure rules are the binding constraints. Position count naturally lands at 12–20. |
| `min_position_size` | `$100` | 0.1% of portfolio. Higher than medium's $75 floor — a position below $100 generates negligible P/L relative to a $100K portfolio. A 10% move on $100 is $10. |

**Rules:** All 17 rules binding, same values and justifications as medium with proportionally larger absolute amounts. Regime adaptation fully consequential — crisis cutting gross from $120K to $60K forces liquidation of roughly half the portfolio's exposure.

**What distinguishes large from medium:**

The large profile validates three things medium can't:

1. **Execution quality under market impact.** At $100K, a 5% position is $5,000 — large enough in mid-cap names that the order book is visibly affected. The paper-evaluation harness's impact estimate produces 5–15bp of estimated drag at $5,000 (vs. negligible at medium). Validates that thesis targets account for realistic execution costs — if a thesis targets a 3% move but friction eats 0.3%, the effective target is 2.7%.

2. **Margin pressure at scale.** With 30% short exposure ($30K), margin is a real capital constraint. A 10% adverse move on $30K shorts requires $3,000 additional margin. Combined with the 10% cash reserve, margin calls can create genuine capital squeezes forcing hard PM choices between maintaining hedges and preserving cash. The margin cascade machinery gets its most realistic workout here.

3. **Operator confidence under real dollar pressure.** In live trading, $100K's absolute amounts test whether the operator trusts the system. A 2.5% daily drawdown is $2,500 — gone in an afternoon. An 8% cumulative drawdown is $8,000 from peak. The regime-transition deferral letting the strategist and PM take 2 hours to resolve breaches feels different when the portfolio bleeds $100/minute during a selloff. Not a guardrail design concern, but the reason large is a separate validation stage rather than "medium with more money."

### Profile comparison summary

| Parameter | Micro ($1.5K) | Small ($5–15K) | Medium ($25–50K) | Large ($100K+) |
|---|---|---|---|---|
| Risk priority | Signal quality | Concentration mgmt | Exposure mgmt | Exposure + execution |
| Options | Disabled | Enabled at $10K+ | Enabled | Enabled |
| Shorts | Disabled | Disabled | Enabled | Enabled |
| Sectors | 2 | 3 | 4 | 4 |
| Position count cap | None (exposure-limited) | None (exposure-limited) | None (exposure-limited) | None (exposure-limited) |
| Min position size | $50 | $75 | $75 | $100 |
| Max deployment (net long cap) | 60% ($900) | 60% ($3–9K) | 60% ($15–30K) | 60% ($60K) |
| Drawdown limits reachable? | Yes (with 60% deployment) | Yes | Yes | Yes |
| Regime adaptation meaningful? | Marginally | Marginally | Yes | Yes |
| Fractional shares | Required | Required at $5K | Not required | Not required |
| Rules in profile | 10 (no options/shorts) | 10–14 (options rules at $10K+) | All 17 | All 17 |
| Thesis review trigger | Every 20 trades or 15% loss | TBD | None (drawdown system) | None (drawdown system) |

### Sequential validation lifecycle

Profiles are validated and deployed sequentially:

1. **Paper trade at micro** → tune until confidence established → **deploy real capital at micro**
2. Accumulate real P/L data at micro → **paper trade at small** → tune → **deploy real capital at small**
3. Repeat for medium and large tiers

Each tier graduation is gated on demonstrated profitability with real capital at the current tier. Exact profitability criteria TBD.

### Transitioning between profiles

Profile transition is **manual, not automatic** when capital crosses a tier boundary. The operator reviews portfolio state, confirms readiness for the expanded feature set, and switches the active profile. Prevents a lucky week pushing capital past a boundary and suddenly enabling shorts or removing the position count cap before the operator is comfortable.

If capital shrinks below the current tier's lower bound, the system generates a **profile downgrade advisory** but does not automatically switch. The operator decides whether to tighten or continue at the current tier with reduced capital. Automatic downgrade could force-close positions in newly-disabled features (e.g., options when dropping from small to micro) — an operator decision, not mechanical.

---

## Dependencies

- [Regime adaptation](regime-adaptation.md) — every value in this document has regime-dependent variants (tighter in crisis, slightly looser in low-vol)
- [Guardrail-evaluation library](guardrail-evaluation.md) — reads the rule values and feature flags defined here to project per-rule outcomes against `(current_state, proposed_deltas)`
- [Breach behavior](breach-behavior.md) — defines what happens when each rule is hit
- [Execution layer / architecture](../05-execution-layer/architecture.md) — the engine's guardrail enforcement layer implements the T3 check
- [Portfolio state / raw state](../01-data-layer/internal/portfolio-state.md) — category 4c reports current headroom against each rule
- [Analyst conviction scale](../04-decision-layer/analyst.md) — advisory sizing bands are calibrated against the position size limits
