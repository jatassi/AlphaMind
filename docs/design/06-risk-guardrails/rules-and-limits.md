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

**Purpose:** First deployment tier. The risk management priority at this scale is **signal quality** — generating the maximum volume of clean, attributable thesis outcomes to determine whether the analytical framework produces positive expectancy. Capital preservation is secondary; the structural constraints and bracket stops provide sufficient loss protection at this scale without dedicated exposure management machinery.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `false` | Even a $1 option contract ($100 premium) is 6.7% of portfolio. Options are impractical. |
| `short_selling_enabled` | `false` | Margin requirements at this scale consume too much capital. |
| `fractional_shares_required` | `true` | A single NVDA share (~$133) is 8.9% of portfolio — over the position limit without fractional shares. |
| `active_sectors` | `[tech, semis]` | Start with the two highest-signal sectors. Broader coverage is validated at the small tier. |
| `max_concurrent_positions` | None | **No cap.** The priority is signal volume — more concurrent theses means faster statistical convergence on whether the framework works. With $1,350 deployable capital and $50 min position size, the theoretical max is 27 concurrent positions. In practice, conviction 2–4 sizing at $50–75 each yields 18–22 positions, deploying 60–90% of capital. The exposure rules (sector concentration, net long, gross) provide the structural ceiling on position count — positions can't be added once sector or exposure limits are hit. |
| `min_position_size` | `$50` | 3.3% of portfolio. Large enough that a 5–10% thesis move produces $2.50–5.00 of measurable P/L, and large enough that bid-ask friction is negligible on asset universe names. Faithfully represents live-viable economics — you'd actually take a $50 position with real money at this scale. |

**Rules — what's in the profile:**

The micro profile carries only rules that serve the signal quality priority. Rules are either protecting data quality (ensuring clean trade outcomes) or preventing the portfolio from destroying itself before enough data is collected.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($75) | Prevents over-concentration in a single thesis. With no position count cap, this is the primary constraint on per-trade risk. The $50–75 sizing range is narrow, but micro isn't about sophisticated position sizing — it's about thesis validation volume. |
| Position-level max loss | 30% | Bracket stop backstop. Ensures every trade has a clean exit, which means clean data. A position that drifts to -50% without being stopped is a data quality failure. |
| Sector concentration | 25% | With no position count cap and 2 active sectors, the analyst could cluster heavily in tech or semis. The 25% cap prevents the portfolio from becoming a single-sector bet, which would compromise signal diversity — 20 tech positions resolving against the same catalyst isn't 20 independent data points. |
| Net long exposure | 60% | With 18–22 potential positions, the portfolio can now deploy 60–90% of capital. The net long cap becomes a real constraint that prevents the system from going all-in directionally. At 60% max ($900 deployed), the system has meaningful exposure — a 5% broad decline costs $45 (3% of portfolio). |
| Gross exposure | 120% | Technically redundant with net long at micro (no shorts means gross = net long), but included for profile consistency. Will bind if shorts are ever enabled. |
| Daily drawdown | 2.5% ($37.50) | With 60% deployment ($900), a 4.2% decline across all positions triggers halt. This is reachable in a severe selloff — no longer structurally inert as it was under the 3-position cap. The halt protects against the scenario where a broad market downturn hits 18 correlated positions simultaneously. |
| Cumulative drawdown | 8% ($120) | With higher deployment, cumulative drawdown is reachable over a bad week. At $900 deployed, 13.3% aggregate loss across positions hits the 8% portfolio drawdown. The progressive response (reduced sizing, flagged losers) is now testable. |
| Correlation | 0.70 | With 18–22 positions across only 2 sectors, portfolio correlation will be a genuine concern. Tech and semis are structurally correlated (AI narrative, supply chain linkages). The correlation limit pushes the analyst to find genuinely differentiated theses rather than 15 variations of the same trade. This directly serves signal quality — correlated positions produce redundant data. |
| Min cash reserve | 10% ($150) | Ensures capital for new opportunities as existing theses resolve. With higher deployment, this is a real constraint on total position count. |
| Pending order capital | 30% ($450) | With many concurrent positions, pending limit orders could tie up significant capital. The 30% cap ensures the system doesn't over-commit to entries that may never fill. |

**Rules — what's NOT in the profile:**

| Rule | Why it's excluded |
|---|---|
| Options rules (delta, theta, vega) | Options disabled. |
| Short rules (total, single, borrow) | Shorts disabled. |
| Net short exposure | Shorts disabled. |

**Thesis performance review trigger:** In addition to the standard guardrail rules, the micro profile includes a **thesis performance review** — a structured checkpoint that fires after every 20 completed trades (closed positions with resolved theses), or when cumulative realized losses exceed 15% of starting capital ($225), whichever comes first. This is not a mechanical halt or restriction on agent behavior. It's a system pause where the operator evaluates the track record: win rate, average gain vs. average loss, thesis accuracy by sector, bracket stop effectiveness, timing accuracy. The review serves the signal quality priority — its purpose is to answer "is the framework generating positive expectancy?" before too much capital is consumed by a broken approach. If the review is positive, trading resumes. If negative, the operator adjusts the thesis framework, analyst prompt, or bracket configuration before continuing.

**Capital utilization comparison:** Under the previous 3-position design, max deployment was ~15% ($225 of $1,500). Under the revised profile, max deployment is 60% ($900) constrained by the net long exposure limit — a 4× increase in capital at work, producing proportionally more P/L data per unit time.

**What this profile validates:** Thesis quality at volume (are recommendations profitable across 20+ concurrent theses?), PM decision quality under load (approving/rejecting 5–10 proposals per invocation rather than 1–2), bracket architecture, timing, sector concentration management, correlation management within a 2-sector universe, daily and cumulative drawdown mechanics (now reachable with higher deployment).

**What this profile does NOT validate:** Options, shorts, 4-sector diversification, regime adaptation under extreme exposure, margin management.

**Broker requirement:** Must support fractional shares. Commission-free strongly preferred.

### Profile: Small ($5,000–$15,000)

**Purpose:** Second deployment tier. The risk management priority at this scale is **concentration management** — the system has proven thesis quality at micro, and is now scaling up with more positions, a third sector, and (at the upper end) options. The danger is that correlated failure modes emerge as complexity grows: five tech positions that all unwind on the same catalyst, or options greeks concentrating risk in dimensions the equity-only micro tier never tested. Every rule in this profile should either directly measure or constrain concentration, or protect against the compounding losses that concentrated portfolios produce.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `false` at $5K / `true` at $10K+ | At $5K, a $250 option premium is 5% of portfolio — at the per-position limit with no room for error. At $10K+, options become practical: $500 premium is 5%, $200 is 2%, allowing conviction-appropriate sizing. Options introduce three new concentration dimensions (delta, theta, vega) that directly relate to the tier's risk priority. Exact enablement threshold TBD during paper trading. |
| `short_selling_enabled` | `false` | Margin requirements still consume disproportionate capital at this scale. Shorts remain disabled — they introduce directional complexity (gross vs. net exposure, hedge interactions) that belongs at the medium tier where the priority shifts to full exposure management. |
| `fractional_shares_required` | `true` at $5K / `false` at $10K+ | At $5K, many asset universe names require fractional shares for compliant sizing. At $10K+, most positions are large enough for whole shares, but fractional should remain available. |
| `active_sectors` | `[tech, semis, financials]` | Third sector enabled. This is a concentration management lever — financials provide the first genuine diversification against the structurally correlated tech/semis pair. The analyst can now construct portfolios where a tech selloff doesn't hit every position simultaneously. Energy added at medium tier. |
| `max_concurrent_positions` | None | **No cap.** Same reasoning as micro: sector concentration (25%) and correlation (0.70) directly measure and constrain concentration, which is the actual priority. A position count cap is an indirect proxy that limits concentration by limiting quantity rather than measuring whether positions are actually correlated. With $3K–9K deployable capital (60% net long cap) and $75 min position size, the exposure rules naturally limit position count to ~8–12. |
| `min_position_size` | `$75` | At $5K, $75 is 1.5% of portfolio. At $15K, $75 is 0.5%. This is higher than micro's $50 floor because at small, each position should be large enough to justify the thesis-tracking overhead across the strategist, PM, and context packages — and the system has graduated past the "maximize signal volume" phase into "take well-sized, well-diversified positions." At $15K, a $75 position produces $3.75–7.50 of P/L on a 5–10% move — clearly measurable and worth tracking. |

**Rules — what's in the profile:**

The small profile carries rules that serve the concentration management priority: measuring how diversified the portfolio actually is, constraining greeks concentration from newly-enabled options, and protecting against the compounding losses that concentrated portfolios produce during correlated selloffs.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($250–750) | Prevents any single thesis from dominating the portfolio. At $15K, a 5% position is $750 — meaningful capital where a 30% bracket stop loss costs $225. The per-position cap forces diversification by spreading capital across theses. |
| Position-level max loss | 30% / 80% (options) | Bracket stop backstop for equity, and the first tier where the 80% options threshold is relevant (when options are enabled at $10K+). A $500 options position losing 80% = $400 loss — real money at this scale. |
| Sector concentration | 25% | **Core concentration constraint.** With 3 active sectors and no position count cap, the analyst could load heavily into tech. At $15K, 25% = $3,750 per sector — room for 3–5 positions in the favored sector. The limit forces the analyst to look beyond the obvious sector when it fills up, which is exactly the concentration management behavior this tier is designed to develop. |
| Net long exposure | 60% | Caps total deployment. At $15K, 60% = $9,000 deployed. With 8–12 positions, a broad selloff hitting the full book can cost $450–900 (3–6% of portfolio) on a 5–10% decline. The cap ensures the portfolio has enough cash buffer to absorb correlated losses without existential damage. |
| Gross exposure | 120% | Technically redundant with net long while shorts are disabled (gross = net long). Included for profile consistency — becomes binding at medium when shorts are enabled. |
| Daily drawdown | 2.5% | At $15K with 60% deployment ($9,000), a 4.2% decline across all positions triggers halt. With 8–12 positions across only 3 sectors (tech, semis, financials), a correlated sector selloff can reach this. A broad tech crash hitting 8 tech/semi positions simultaneously is exactly the concentrated failure mode this tier is designed to manage. The halt protects against it. |
| Cumulative drawdown | 8% | At $15K, 8% = $1,200. With $9,000 deployed, a 13.3% aggregate loss hits the limit. Reachable over a bad week of correlated losses. The progressive response (reduced sizing at 8%, further reduction at 10%, full halt at 12%) is testable and meaningful at this tier. |
| Correlation | 0.70 | **Core concentration constraint.** With 8–12 positions across 3 sectors, the weighted average pairwise correlation is the most direct measure of whether the portfolio is genuinely diversified or just holding many versions of the same bet. Tech and semis are structurally correlated (AI narrative, supply chain linkages); financials provide the diversification lever. If the portfolio exceeds 0.70, the analyst needs to find genuinely differentiated theses or the PM needs to reject redundant proposals. This rule is the reason the third sector exists in this profile. |
| Options delta exposure | 40% (when enabled) | At $10K+, options introduce delta concentration risk that equity-only portfolios don't have. Three long calls in tech names stack correlated delta. The 40% cap ensures options delta doesn't dominate the portfolio's directional profile. |
| Portfolio theta | 0.15%/day (when enabled) | At $10K, 0.15% = $15/day. A single 30DTE option might carry $5–10/day theta; two options positions approach the limit. Theta is a guaranteed daily cost — it compounds silently and can drag the portfolio into drawdown territory without any adverse price movement. The cap prevents the portfolio from accumulating a theta bleed that undermines the equity book's returns. |
| Portfolio vega | 1.0%/pt (when enabled) | At $10K, 1.0% = $100/pt of IV movement. With 2–3 options positions, a 5-point IV crush around an event costs up to $500 (5% of portfolio). The vega cap prevents the portfolio from becoming a volatility bet when the intent is directional thesis validation. |
| Min cash reserve | 10% | At $15K, 10% = $1,500 reserve. Ensures capital availability for new positions as existing theses resolve, and provides a buffer against margin requirements if options are enabled. |
| Pending order capital | 30% | With more concurrent positions and limit orders, the 30% cap prevents the system from over-committing to entries that may never fill — tying up capital that could be deployed to higher-conviction opportunities. |

**Rules — what's NOT in the profile:**

| Rule | Why it's excluded |
|---|---|
| Net short exposure | Shorts disabled. |
| Total short exposure | Shorts disabled. |
| Single short max size | Shorts disabled. |
| Borrow cost budget | Shorts disabled. |

**What this profile validates:** Concentration management across 3 sectors (is the system genuinely diversifying or clustering?), correlation-aware portfolio construction, options greeks management at introductory scale, the daily drawdown halt under correlated selloff conditions, cumulative drawdown progressive response, sector concentration as an active constraint, the PM's ability to reject redundant proposals.

**What this profile does NOT validate:** Short selling, 4-sector diversification, gross exposure management with a long+short book, margin management, regime adaptation under extreme exposure (meaningful at medium+).

### Profile: Medium ($25,000–$50,000)

**Purpose:** Third deployment tier. The risk management priority at this scale is **exposure management** — the full feature set is online (shorts, options, 4 sectors), and the portfolio deploys enough capital that aggregate risk interactions become the dominant concern. A portfolio with 10+ positions across longs, shorts, and options has risk properties that aren't visible at the individual position level — directional tilt, leverage, greeks concentration, cross-sector correlation, and the interaction effects between them. The rules in this profile are oriented around measuring and constraining the portfolio's aggregate risk profile, and protecting against the compounding drawdowns that occur when aggregate exposure is mismanaged.

This is also the first tier where **regime adaptation is consequential**. Crisis mode cutting gross exposure from 120% to 60% when the portfolio is running at 85% gross forces real position reductions. The regime adaptation machinery — immediate tightening, gradual loosening, strategist → PM deferral for transition breaches — gets its first genuine workout here.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `true` | At $25K, a $1,250 max options position (5% premium at risk) is comfortable for single-leg strategies. Multi-leg strategies viable at $50K. Options are now a portfolio construction tool, not just a feature to validate — the PM can use them for defined-risk directional exposure and event plays. |
| `short_selling_enabled` | `true` | **The defining feature expansion of this tier.** Margin requirements are manageable at $25K+. The 30% short exposure cap allows $7,500–15,000 in short positions (2–5 meaningful shorts). The long/short interaction creates the first real distinction between gross and net exposure — a portfolio at 55% long and 20% short is 75% gross but only 35% net. This interaction is the core of exposure management. |
| `fractional_shares_required` | `false` | Position sizes are large enough for whole shares in all asset universe names. |
| `active_sectors` | `[tech, semis, financials, energy]` | Full 4-sector coverage. Energy is added here because the short book creates new hedging dynamics — energy names often move inversely to tech during certain macro regimes, giving the system a natural diversification lever that didn't exist in the long-only small tier. |
| `max_concurrent_positions` | None | No cap. Exposure rules are the binding constraints. With 25% sector cap × 4 sectors and per-position sizing, position count naturally lands at 10–15. |
| `min_position_size` | `$75` | At $25K, $75 is 0.3% of portfolio. Below this, thesis-tracking overhead isn't justified. At $50K, $75 is 0.15% — small, but the system may legitimately want smaller positions as part of a hedging strategy. |

**Rules — what's in the profile:**

All 17 rules are present and binding. At this scale, every rule serves the exposure management priority. The justifications below focus on what's new or different from the small tier — rules that appeared in earlier profiles retain their prior justifications plus the exposure management dimension.

| Rule | Value | Why it's in the profile |
|---|---|---|
| Per-position max size | 5% ($1,250–2,500) | Full-sized positions where a 30% bracket stop loss costs $375–750. The 5% cap prevents any single thesis from dominating the portfolio's aggregate exposure. At $50K, even a max-sized position is only one piece of a 10–15 position portfolio. |
| Position-level max loss | 30% / 80% | Both equity and options thresholds are active and meaningful. A $2,500 equity position hitting the 30% backstop costs $750; a $1,250 options position hitting 80% costs $1,000. These losses are significant at this scale — the backstop is doing real capital protection work, not just ensuring data quality. |
| Sector concentration | 25% | At $50K, 25% = $12,500 per sector — room for 3–5 positions at typical sizing. With 4 active sectors, the constraint forces genuine cross-sector diversification. The system can't fill one sector and ignore the others without running out of headroom. |
| Net long exposure | 60% | **Core exposure management constraint.** At $50K, 60% = $30,000 max long deployment. With shorts providing partial offset, the system can hold more gross long exposure while staying within the net limit — e.g., $35K long and $5K short = 60% net, 80% gross. This is the first tier where the net vs. gross distinction creates meaningful portfolio construction decisions. |
| Net short exposure | 30% | **New at this tier.** Caps the short book at $7,500–15,000. The 30% limit is tighter than the 60% long limit because shorts carry asymmetric risk (theoretically unlimited loss, squeeze risk, borrow recall). At $50K, 30% supports 3–5 short positions — enough for genuine pair trades and directional hedging, not enough to run a net-short portfolio. |
| Gross exposure | 120% | **First tier where this rule binds independently of net long.** A portfolio at 60% net long and 30% net short is 90% gross — well within the 120% limit. But add options delta overlay and the ceiling approaches. At $50K, 120% = $60K gross exposure — significant leverage. The gross limit prevents the system from building a large long book, a large short book, and a large options overlay simultaneously, which would create a portfolio with far more risk than any individual metric suggests. |
| Daily drawdown | 2.5% ($625–1,250) | At $50K with $35K deployed, a 3.6% decline across the portfolio triggers halt. This is a realistic single-day scenario during elevated volatility — not an extraordinary event. The halt fires often enough at this tier to be a regular feature of system operation, not a rare emergency. |
| Cumulative drawdown | 8% ($2,000–4,000) | At $50K, the progressive response tiers are all reachable: 8% ($4K) triggers tier 1, 10% ($5K) triggers tier 2, 12% ($6K) triggers full halt. A bad week can push through tier 1 into tier 2. The full progressive mechanism — reduced sizing, flagged losers, PM review, eventual fallback to engine-driven closure — gets a real workout. |
| Correlation | 0.70 | With 10–15 positions across 4 sectors plus a short book, the correlation structure is complex. Long/short pairs within a sector may have high pairwise correlation by design (that's the hedge). The weighted average correlation metric needs to account for this — a long NVDA / short AMD pair is high-correlation but low directional risk. The PM must understand correlation in the context of directional exposure, not just in isolation. |
| Options delta exposure | 40% | With multiple options positions, options delta can quietly dominate the portfolio's directional exposure. At $50K, 40% = $20K of delta-adjusted exposure from options alone. Combined with the equity book, total directional exposure could reach the net long limit primarily through options leverage — which means the portfolio's risk profile is more sensitive to gamma and IV changes than it appears. The delta cap ensures the PM consciously manages this. |
| Portfolio theta | 0.15%/day ($37.50–75) | At $50K, $75/day max theta = $1,500/month if fully consumed. That's a 3% monthly headwind that the equity and options book must overcome just to break even. Multiple options positions can approach this limit, especially around events when the system might hold several pre-event positions. Active theta management — rolling, closing, offsetting — becomes a real PM responsibility. |
| Portfolio vega | 1.0%/pt ($250–500/pt) | At $50K, a 5-point IV crush costs up to $2,500 (5% of portfolio) if vega is at the limit. Around FOMC or earnings, IV can crush 5–10 points overnight. The vega cap interacts with the pre-event tightening overlay — the PM should be aware of vega exposure heading into known events. |
| Total short exposure | 30% | Aggregate short book limit, matching the net short directional cap. At $50K, $15K max short exposure. The aggregate limit prevents the system from building a large short book that looks manageable position-by-position but carries significant squeeze risk in aggregate — if a sector-wide short squeeze hits 3 correlated shorts simultaneously, the combined loss can far exceed what individual position limits suggest. |
| Single short max size | 3% ($750–1,500) | Tighter than the 5% general position cap because individual short squeezes are faster and more severe than long-side declines. At $50K, a 3% short is $1,500. A 30% adverse move costs $450 — painful but survivable. At 5% ($2,500), the same squeeze costs $750 and starts interacting with drawdown limits. |
| Borrow cost budget | 0.05%/day ($12.50–25) | Prevents the system from accumulating expensive-to-borrow shorts that silently bleed cost. At $50K, $25/day supports approximately $50K in short exposure at an 18% annualized rate — well above the 30% limit. The budget only binds for hard-to-borrow names (50%+ annualized), which is the intended behavior. |
| Min cash reserve | 10% ($2,500–5,000) | With shorts consuming margin and options requiring premium, the cash reserve ensures capital availability for margin calls, new opportunities, and operational buffer. At this scale, the 10% reserve is a meaningful capital allocation decision — $5K held back is $5K not deployed. |
| Pending order capital | 30% ($7,500–15,000) | With 10–15 positions, pending limit orders across both long and short directions can tie up significant capital. The 30% cap forces the PM to prioritize which entries are worth waiting for. |

**Rules — what's NOT in the profile:**

None. All 17 rules are present and binding at this tier. This is the first profile where no rules are excluded.

**Regime adaptation at medium:** This is the first tier where regime transitions create significant constraint pressure. Under normal regime, the portfolio might run at 55% net long and 85% gross. An elevated regime tightens net long to 45% and gross to 90% — the net long limit now binds and requires reductions. A crisis regime tightens to 30% net long and 60% gross — roughly halving the portfolio's operating capacity. The strategist → PM deferral for regime-transition breaches, the emergency invocation trigger for regime jumps, and the gradual loosening on recovery are all meaningfully exercised. The pre-event tightening overlay interacts with regime parameters — an elevated regime during FOMC week compounds tightening effects.

**What this profile validates:** The full guardrail architecture under real constraint pressure. All 17 rules binding. Regime adaptation with meaningful parameter changes. Drawdown halt and progressive response under realistic conditions. Short book management (squeeze risk, borrow costs, margin). Long/short interaction effects (gross vs. net, directional hedging). Options greeks management at scale. 4-sector correlation management. Cross-constraint interactions (fixing one breach can create another). Emergency invocation under regime jumps. The PM's ability to manage a complex, multi-dimensional risk profile.

### Profile: Large ($100,000+)

**Purpose:** Fourth deployment tier. The risk management priority at this scale is **exposure management plus execution quality**. The rule set and feature flags are identical to medium — all 17 rules, all features enabled, same percentage values. What changes is that scale introduces two risks that don't exist at medium: market impact and the psychological weight of real dollar losses.

A $5,000 position (5% of $100K) in a mid-cap name is no longer invisible to the order book. The [paper-evaluation harness](../05-execution-layer/paper-evaluation-harness.md)'s slippage and impact estimates — which were conservative-but-ignorable at medium sizing — start producing meaningful live-execution drag at large. A $2,500 daily drawdown halt ($100K × 2.5%) is the same percentage as medium's $1,250 halt, but the absolute number tests whether the system (and operator) can maintain discipline when the losses feel larger.

**Feature flags:**

| Flag | Value | Rationale |
|---|---|---|
| `options_enabled` | `true` | Full options capability including multi-leg strategies. At $100K, 5% premium at risk = $5,000 — comfortable for any single-leg or multi-leg strategy in the asset universe. |
| `short_selling_enabled` | `true` | Full short book. 30% exposure = $30K in shorts — a substantial short portfolio supporting 5–10 positions. |
| `fractional_shares_required` | `false` | Position sizes are comfortably above whole-share thresholds for all asset universe names. |
| `active_sectors` | `[tech, semis, financials, energy]` | Full 4-sector coverage, same as medium. |
| `max_concurrent_positions` | None | Exposure rules are the binding constraints. Position count naturally lands at 12–20 depending on sizing. |
| `min_position_size` | `$100` | At $100K, $100 is 0.1% of portfolio. The higher floor (vs. $75 at medium) reflects that at this scale, a position below $100 generates negligible P/L relative to the portfolio and doesn't justify thesis-tracking overhead. A 10% move on a $100 position is $10 — barely registering on a $100K portfolio. |

**Rules:** All 17 rules are present and binding, with the same values and justifications as the medium profile. Every rule that serves exposure management at $25–50K serves it at $100K+ with proportionally larger absolute dollar amounts. Regime adaptation is fully consequential — crisis mode cutting gross from $120K to $60K forces liquidation of roughly half the portfolio's exposure.

**What distinguishes large from medium:**

The large profile validates three things medium can't:

1. **Execution quality under market impact.** At $100K, a 5% position is $5,000 — large enough in mid-cap names (e.g., some energy or financial names) that the order book is visibly affected. The paper-evaluation harness's impact estimate, which produced negligible adjustments at medium's $1,250–2,500 position sizes, starts producing 5–15bp of estimated drag at $5,000. This validates that the system's thesis targets account for realistic execution costs at scale. If a thesis targets a 3% move but execution friction eats 0.3%, the effective target is 2.7% — the system needs to know this.

2. **Margin pressure at scale.** At $100K with 30% short exposure ($30K), margin requirements are a real capital constraint. A 10% adverse move on $30K of shorts requires $3,000 in additional margin. Combined with the cash reserve (10% = $10K), margin calls can create genuine capital squeezes that force the PM to make hard choices between maintaining hedges and preserving cash. The margin cascade machinery gets its most realistic workout here.

3. **Operator confidence under real dollar pressure.** In live trading (post paper-trade graduation), the absolute dollar amounts at $100K test whether the operator trusts the system's judgment. A 2.5% daily drawdown is $2,500 — gone in an afternoon. An 8% cumulative drawdown is $8,000 from peak. The regime-transition deferral that lets the strategist and PM take 2 hours to resolve breaches feels very different when the portfolio is bleeding $100/minute during a selloff. This isn't a guardrail design concern, but it's the reason the large tier exists as a separate validation stage rather than just "medium with more money."

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
2. Accumulate real P/L data at micro → **paper trade at small** (informed by micro lessons) → tune → **deploy real capital at small**
3. Repeat for medium and large tiers

Each tier graduation is gated on demonstrated profitability with real capital at the current tier. Exact profitability criteria TBD.

### Transitioning between profiles

When the portfolio's capital grows past a tier boundary (e.g., from $15K into the medium tier), the profile transition is **manual, not automatic**. The operator reviews the current portfolio state, confirms readiness for the expanded feature set, and switches the active profile. This prevents a scenario where a lucky week pushes capital past a boundary and the system suddenly enables shorts or removes the position count cap before the operator is comfortable with those features.

On the way down — if capital shrinks below the current tier's lower bound — the system generates a **profile downgrade advisory** but does not automatically switch. The operator decides whether to tighten the profile or continue at the current tier with reduced capital. Automatic downgrade could force-close positions in features that become disabled (e.g., options positions when dropping from small to micro), which should be an operator decision, not a mechanical one.

---

## Dependencies

- [Regime adaptation](regime-adaptation.md) — every value in this document has regime-dependent variants (tighter in crisis, slightly looser in low-vol)
- [Guardrail-evaluation library](guardrail-evaluation.md) — reads the rule values and feature flags defined here to project per-rule outcomes against `(current_state, proposed_deltas)`
- [Breach behavior](breach-behavior.md) — defines what happens when each rule is hit
- [Execution layer / architecture](../05-execution-layer/architecture.md) — the engine's guardrail enforcement layer implements the T3 check
- [Portfolio state / raw state](../01-data-layer/internal/portfolio-state.md) — category 4c reports current headroom against each rule
- [Analyst conviction scale](../04-decision-layer/analyst.md) — advisory sizing bands are calibrated against the position size limits
