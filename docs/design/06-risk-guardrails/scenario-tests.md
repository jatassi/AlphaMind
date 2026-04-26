# Guardrail scenario tests

Concrete scenarios to validate the guardrail design across both portfolio profiles. Each scenario defines a portfolio state, a market condition, a proposed action, and then walks through the guardrail layers to determine the outcome.

---

## Part A — Full-system portfolio ($100,000)

### A1. Normal regime — analyst proposes max-conviction tech trade

**Portfolio state:** $100K equity, 6 open positions. Tech sector at 14% exposure. Net long 35%, gross 62%. Cash $22K. Daily P/L: +0.3%. No drawdown.

**Regime:** Normal (VIX 17).

**Proposed action:** Analyst proposes OPEN NVDA long equity, conviction 5, size 4.8% ($4,800, ~36 shares at $133).

**Guardrail walkthrough:**

- *T1 analyst context:* Tech sector headroom = 25% − 14% = 11%. Per-position max = 5%. Proposed 4.8% fits within both. Net long after = 39.8% (limit 60%). Gross after = 66.8% (limit 120%).
- *T1 validation tool:* PASS on all rules. No warnings.
- *Pre-processor:* If no other proposals, cumulative capital = $4,800 vs $22K available. No conflict.
- *T2 PM:* Sees clean headroom. Evaluates thesis quality. Approves at proposed size.
- *T3 engine:* Final check — all rules pass. Order submitted.

**Outcome:** Clean approval. System working as designed at the sweet spot — high conviction, ample headroom.

---

### A2. Normal regime — analyst produces 4 proposals that collectively breach sector limit

**Portfolio state:** $100K equity, 8 open positions. Tech sector at 18% exposure. Semis at 12%. Net long 45%, gross 85%.

**Regime:** Normal (VIX 19).

**Proposed actions:** Analyst proposes: REC-1 AAPL long 3% ($3K), REC-2 MSFT long 2.5% ($2.5K), REC-3 META long 2% ($2K), REC-4 GOOGL long 2% ($2K). All in tech sector.

**Guardrail walkthrough:**

- *T1 validation (cumulative):* REC-1 checks tech at 18% + 3% = 21%. PASS (limit 25%). REC-2 checks cumulative: 21% + 2.5% = 23.5%. PASS but WARNING zone (>70% of 25% limit consumed: 94% consumed). REC-3 checks cumulative: 23.5% + 2% = 25.5%. **FAIL — would breach 25% sector limit.** Tool returns: "reduce size by 75% or drop this proposal."
- *Analyst response:* Drops REC-3 (lowest conviction of the remaining), revises REC-4 to 1.5%. Re-validates REC-4: 23.5% + 1.5% = 25.0%. PASS (exactly at limit — 100% consumed, HARD BLOCK zone). Tool returns WARNING: "sector will be at hard block after this trade."
- *Pre-processor:* Flags cumulative capital $8K, flags tech sector going from 18% → 25% (HARD BLOCK after all trades). Also flags net long going from 45% → 54% — still within 60% but entering WARNING zone.
- *T2 PM:* Sees the pre-processor annotations. May decide to approve only REC-1 and REC-2 and reject REC-4 to preserve headroom. Or approve all three accepting that tech is now at limit.
- *T3 engine:* If PM approves all three, sequential command processing. Each OPEN command checks against the state *after* prior fills. If market movement during execution pushed tech to 24% from a rally in existing positions, the third OPEN could be rejected at T3 even though it passed T1.

**Outcome:** Cumulative tracking at T1 catches the 4th proposal. Pre-processor flags the aggregate sector risk. PM has judgment discretion on how many to approve. **Finding: system correctly surfaces cumulative risk across multiple same-sector proposals.**

---

### A3. Regime transition normal → elevated — existing positions now exceed tightened limits

**Portfolio state:** $100K equity, 10 positions. Tech at 24% (just under 25% normal limit). Semis at 19%. Net long 55%. Gross 105%. Two positions at 4.5% each.

**Regime change:** VIX spikes from 20 → 28 overnight. Distillation layer reclassifies to **elevated**.

**New limits under elevated:** Tech sector limit drops to 20%. Per-position max drops to 3.5%. Net long limit drops to 45%. Gross drops to 90%.

**Guardrail walkthrough:**

- *Tightening is immediate* — new limits active at the start of this invocation.
- **Immediate breaches on existing positions:**
  - Tech sector: 24% vs 20% limit → **4% overage**
  - Semis: 19% vs 20% limit → OK
  - Two positions at 4.5% vs 3.5% limit → **1% overage each**
  - Net long: 55% vs 45% limit → **10% overage**
  - Gross: 105% vs 90% limit → **15% overage**
- *Classification:* Per regime-adaptation.md, regime-transition breaches on existing positions are **deferred to strategist → PM** — no immediate engine action.
- *Exception check:* Is daily drawdown also breached? If the VIX spike came with a market selloff, portfolio may also be in daily drawdown. If daily drawdown hits 2.0% (elevated limit), halt mode activates — **overrides** regime-transition deferral for drawdown. Engine handles the drawdown breach immediately; strategist and PM handle exposure breaches at this invocation.
- *Strategist response:* Sees all breaches flagged as `regime_transition` type with aggregate overage. Using its thesis-level context (target proximity, conviction, catalyst timing), the strategist proposes specific remedies: trim the weakest-thesis tech positions to get sector from 24% → 20%, close or reduce the oversized positions from 4.5% → 3.5%, sell specific longs to bring net long within 45%. Each recommendation includes rationale.
- *PM review:* Receives the strategist's remedy proposals alongside the cross-constraint impact summary. Validates that the proposed trims don't create secondary breaches (e.g., closing a balanced pair of positions might worsen directional exposure). Adjusts sizing or priority as needed, then executes via command envelopes.

**Outcome:** Multiple simultaneous breaches from regime transition. System correctly defers to strategist → PM rather than mechanical forced liquidation. The strategist proposes thesis-informed remedies; the PM validates cross-constraint interactions and executes. **Finding: This separation of concerns is critical here — the strategist knows *which* positions are weakest and should be cut first, while the PM's cross-constraint impact summary prevents fixing one breach while creating another.**

---

### A4. Daily drawdown halt triggers mid-session

**Portfolio state:** $100K equity at open. 8 positions. Net long 48%. Daily P/L started at 0%, now at −1.8% after a broad selloff.

**Regime:** Normal. Daily drawdown limit: 2.5%.

**Market event:** At 1:30 PM, another wave of selling. Portfolio equity drops to $97,200 — daily drawdown now 2.8%.

**Guardrail walkthrough:**

- *Continuous monitor detects:* Daily drawdown = 2.8%, exceeding 2.5% limit.
- *Halt mode activates immediately:*
  - Engine rejects all OPEN and ADD commands for the rest of the day.
  - Existing bracket stops remain active (take-profits and invalidation stops still fire).
  - Pending limit orders are NOT cancelled (PM can cancel at next invocation).
- *Next pipeline invocation (scheduled 2 hours later) — agents receive halt-mode context:*
  - **Upstream pipeline (distillation, research):** Runs normally. The broad selloff may have shifted sector dynamics, triggered anomaly alerts, or changed the outlook for current holdings. This output feeds into the strategist's reasoning.
  - **Analyst (watchlist mode):** Context header shows "HALT MODE ACTIVE — WATCHLIST ONLY." Analyst does not generate trade proposals — instead produces a compact watchlist (ticker, thesis summary, estimated conviction) for opportunities worth revisiting post-halt. Token cost is a fraction of full proposal generation.
  - **Strategist (defensive posture):** Still receives full distillation and research output. Shifts focus to defensive position management — which of the 8 positions have deteriorating theses in this selloff? Which stops should be tightened? Are any positions worth closing proactively to limit further drawdown?
  - **PM (risk reduction mode):** Sees halt state with restricted action space (CLOSE, ADJUST, CANCEL only). Reviews pending limit orders placed before halt — should any be cancelled given the selloff? Executes strategist's defensive recommendations. Tightens stops on positions moving against thesis.
- *At 3:55 PM:* Portfolio has recovered slightly to $97,800 (drawdown 2.2%). Halt remains in effect because the 2.5% limit was breached — the halt holds for the rest of the day regardless of recovery.
- *Next day open:* Daily drawdown resets. If cumulative drawdown is still within 8% limit, full operating capacity resumes. Analyst's watchlist from the halt-mode invocation is surfaced for consideration.

**Outcome:** Clean halt with differentiated agent behavior. The upstream pipeline and strategist continue to do valuable work — informing defensive risk management. The analyst avoids wasting tokens on proposals that can't execute, instead preserving analytical signal as a watchlist. The PM operates in a restricted but still meaningful capacity. **Finding: Halt mode is not a system freeze — it's a mode shift where the pipeline redirects its effort from opportunity capture to capital preservation.**

---

### A5. Options trade pushes theta and vega near limits

**Portfolio state:** $100K equity. 4 existing long options positions contributing: delta-adjusted exposure 28% from options, daily theta $120 (0.12%), vega exposure $800/pt (0.80%). Plus 5 equity positions.

**Regime:** Normal. Options delta limit 40%, theta limit 0.15%/day ($150), vega limit 1.0%/pt ($1,000).

**Proposed action:** Analyst proposes OPEN long NVDA 140C 30DTE, 3 contracts at $6.50 ($1,950 premium). Estimated greeks: delta 0.55 per contract (×100×3 = 165 shares equivalent, ~$21,945 delta-adjusted), theta −$18/day per contract (×3 = −$54/day), vega $12/pt per contract (×3 = $36/pt).

**Guardrail walkthrough:**

- *T1 validation tool:*
  - Position size: $1,950 premium = 1.95% of portfolio. PASS.
  - Delta-adjusted exposure from options: 28% + ($21,945/$100K) = 28% + 21.9% = wait — $21,945 is 21.9% of $100K? That seems high for 3 contracts.

  Let me recompute: delta 0.55 × 100 × 3 = 165 share equivalents. At $140 underlying, delta-adjusted = 165 × $140 = $23,100. With +10% conservative buffer = $25,410, which is 25.4% of portfolio. Current options delta = 28%. After = 28% + 25.4% = 53.4%. **FAIL — exceeds 40% limit by 13.4%.**

  Actually wait — the $23,100 delta-adjusted exposure needs to be reconciled. The 28% existing is $28,000. Adding $25,410 = $53,410 → 53.4%. Definitely fails.

  - Theta: 0.12% + ($54/$100K) = 0.12% + 0.054% = 0.174%. **FAIL — exceeds 0.15% limit.**
  - Vega: 0.80% + ($36/$100K×100) — wait, vega is $36/pt for the new position. Total vega = $800 + $36 = $836. That's 0.836%/pt. PASS (limit 1.0%).

  Tool returns: FAIL on options delta and theta. Guidance: "reduce to 1 contract to pass delta (28% + ~8.5% = 36.5%), but theta would still be 0.12% + 0.018% = 0.138% — PASS at 1 contract."

- *Analyst response:* Reduces to 1 contract. Re-validates. Options delta = 28% + 8.5% = 36.5% (PASS, WARNING zone). Theta = 0.138% (PASS, WARNING zone). Vega = 0.812% (PASS). Premium at risk = $650 = 0.65% (PASS).
- *Pre-processor:* If this is the only proposal, no cross-agent issues.
- *T2 PM:* Sees both greeks in WARNING zone after trade. May approve at 1 contract or decide the portfolio already has enough options exposure.

**Outcome:** Options greeks guardrails correctly prevent over-concentration in options. The validation tool's guidance allows the analyst to self-correct to a compliant size. **Finding: The conservative +10% delta buffer is doing real work here — without it, 2 contracts might have passed delta but failed at T3. The buffer's purpose (ensure T1 approval implies T3 approval) is validated.**

---

### A6. Short squeeze scenario — engine protective CLOSE between invocations

**Portfolio state:** $100K equity. Short position in MARA at 2.8% of portfolio ($2,800, 140 shares short at $20). Single short max = 3%.

**Regime:** Normal.

**Market event:** Between invocations (overnight), MARA drops 25% to $15 on a crypto crash. The short is now worth $2,100 — but the portfolio gained $700 unrealized. Then crypto reverses violently. MARA spikes to $28 (+40% from $20 entry). Short position is now worth $3,920 (140 × $28), loss = $1,120. Position size = 3.92% of portfolio.

**Guardrail walkthrough:**

- *Continuous monitor detects:*
  - Single short position at 3.92% > 3.0% limit. **Breach.**
  - Position-level max loss: ($28 − $20) / $20 = 40% loss on the short. Exceeds 30% equity max loss. **Breach.**
- *Breach classification:*
  - Position-level max loss is **immediate: close position.** This takes priority.
  - Single short size is also **immediate: partial close** but position-level max loss already triggers full close.
- *Engine action:* Issues CLOSE command for full MARA short position. Creates engine-originated command envelope with `engine_guardrail` provenance, rule = `position_level_max_loss`, position = MARA short.
- *Secondary breach check:* Closing the short reduces short exposure (good) but doesn't create any new breaches since it's reducing risk.
- *Thesis resolution:* Marked as `invalidated-stopped-correctly` with `engine_guardrail` provenance. The feedback loop can analyze whether the thesis was wrong or the stop was too wide.
- *PM context at next invocation:* Sees the engine-originated CLOSE in the activity log. Full traceability via the command envelope.

**Outcome:** Engine correctly closes the position on two independent triggers (max loss and size limit). **Finding: Two rules firing simultaneously on the same position is handled cleanly — the more severe action (full close vs partial trim) wins. No conflict.**

---

### A7. Margin call cascade during elevated regime

**Portfolio state:** $98K equity (down from $100K). Elevated regime. 3 short positions totaling 22% short exposure ($21,560): COIN short 8%, SQ short 7%, HOOD short 7%. Margin held = $15K.

**Regime:** Elevated. Max short exposure = 25%.

**Market event:** All three short names gap up 12% on a crypto rally. Short losses total ~$2,587. Broker issues margin call for $3,000 additional margin.

**Guardrail walkthrough:**

- *Margin call takes absolute priority* over all other guardrail rules.
- *Position selection for margin liquidation:*
  1. Worst risk/reward ratio: COIN has moved farthest past its thesis invalidation level. Selected first.
  2. Engine issues CLOSE for COIN short (partial or full depending on margin requirement). Full close of COIN ($8K notional) more than satisfies $3K margin call.
- *Post-liquidation state:* Short exposure drops from 22% to ~14%. Margin freed.
- *Cascade check:* Does closing the COIN short create new breaches?
  - Net long exposure: was 30%, COIN short was providing 8% offset. Net long after = 38%. Elevated limit = 45%. OK.
  - Gross exposure: was 52% + 22% = 74%. After: 52% + 14% = 66%. Elevated limit = 90%. OK.
  - No secondary breaches.
- *Cascade logging:* Command envelope with `margin_cascade` sub-type, linked by cascade_id.
- *PM sees at next invocation:* Margin call event, COIN close, thesis resolution, updated portfolio state.

**Outcome:** Margin call handled cleanly with no cascading breaches. **Finding: In this case the cascade was benign because closing a short reduces overall risk. The more dangerous cascade scenario would be if the margin call forced closing a long that was providing sector balance — worth testing (see A9).**

---

### A8. Cumulative drawdown progressive response — system enters tier 2

**Portfolio state:** Peak equity was $105K (high-water mark). Current equity: $94,500. Cumulative drawdown = 10% (just crossed the 125%-of-limit tier). Daily P/L today: −0.5%.

**Regime:** Normal.

**Progressive response tier 2 activates:**
- Max position size reduced to 2% (from 5%).
- Max gross exposure reduced to 60% (from 120%).
- Existing positions with unrealized loss > 15% flagged for PM review.

**Proposed action:** Analyst proposes OPEN AMD long, conviction 4, size 2.5% ($2,362).

**Guardrail walkthrough:**

- *T1 analyst context:* Should show "CUMULATIVE DRAWDOWN: 10.0% / 8.0% [TIER 2]. Max position size: 2%. Max gross: 60%."
- *T1 validation tool:* FAIL on size: 2.5% > 2% max in tier 2. Tool returns: "reduce size to 2% or below."
- *Analyst response:* Revises to 2% ($1,890). Re-validates: size 2% ≤ 2% max. PASS.
- *Pre-processor:* Flags positions with >15% unrealized loss. Say POS-JPM-003 is down 18% and POS-XOM-007 is down 16%. Both flagged for PM review.
- *T2 PM:* Sees the AMD proposal at 2% alongside the drawdown state (10% cumulative, tier 2). The PM exercises judgment: is a conviction-4 trade worth taking during a significant drawdown? The mechanical guardrails have already constrained the size to 2% — the PM's role is to decide whether even that reduced exposure is warranted given the portfolio's current state. PM might approve (the thesis is strong enough), reduce further, or reject (not worth the risk during drawdown recovery). Also reviews the flagged losing positions — may decide to close POS-JPM-003 (weakest thesis) to free capital and reduce drawdown exposure.
- *T3 engine:* If PM approves, checks 2% position max (tier 2 override). 2% PASS. Checks 60% gross max. Current gross 45%. After = 47%. PASS.

**Outcome:** Progressive drawdown response constrains the system through objective mechanical limits (reduced position size and gross exposure). Conviction-based filtering is left to the PM, where it belongs. **Finding: Conviction is a subjective analyst assessment — using it as a mechanical gate creates perverse incentives for the analyst to inflate conviction scores to bypass the gate. The reduced position sizing (2% max vs 5% normal) is the real protection: even if the PM approves a lower-conviction trade during drawdown, the damage is mechanically bounded. The PM's judgment about whether a trade's conviction justifies entry during drawdown is exactly the kind of decision the PM agent is designed to make.**

---

### A9. Pre-event tightening overlay before FOMC

**Portfolio state:** $100K equity. Normal regime. Net long 52%. Gross 88%. 9 positions. Tech 20%, semis 15%, financials 12%.

**Market event:** FOMC rate decision scheduled tomorrow at 2:00 PM. Current invocation is 2 invocations before the event.

**Pre-event overlay activates:**
- Max position size: reduced by 20% from normal 5% = 4%.
- No new positions in the final invocation before the event (PM may override with documented rationale).
- PM receives prompt to review pending orders for FOMC sensitivity.

**Proposed action:** Analyst proposes OPEN GS long, conviction 3, size 3.5%. This is the final invocation before the event.

**Guardrail walkthrough:**

- *T1 analyst context:* Should show "PRE-EVENT OVERLAY: FOMC 2026-03-17 14:00. Max position: 4%. No new positions this invocation (final pre-event)."
- *T1 validation:* FAIL — no new positions permitted in the final invocation before a high-impact event. Tool returns: "Pre-event block: no new entries in final invocation. Consider for post-event."
- *Analyst:* Notes GS for potential post-event proposal.

**Proposed action #2 (earlier invocation — not the final one):** Analyst proposes OPEN JPM long, conviction 4, size 3.8%.

- *T1 validation:* Size 3.8% ≤ 4% pre-event max. Financials sector 12% + 3.8% = 15.8% vs 25% limit. PASS on all mechanical checks. But financials are directly rate-sensitive to FOMC. The validation tool doesn't have a "sector-event-sensitivity" check — it passes purely on the numbers.
- *T2 PM:* This is where judgment matters. The PM should recognize that a financials long *before FOMC* carries event risk the mechanical guardrails don't capture. PM might reject despite guardrail compliance, or reduce size further, or add a tighter time-based invalidation ("close before FOMC if thesis hasn't resolved").
- *T3 engine:* If PM approves, checks pass.

**Outcome:** Pre-event overlay provides two mechanical protections: reduced sizing (20% tighter) and a hard block on new positions in the final pre-event invocation. Sector-specific event sensitivity (e.g., "don't take financials longs before FOMC") is left to PM judgment. **Finding: The overlay is deliberately sector-agnostic in its mechanical rules. Adding sector-specific event sensitivity would require mapping event types to affected sectors — significant complexity for marginal benefit over PM judgment. This is a valid design choice but worth documenting as an explicit PM responsibility during pre-event windows.**

---

### A10. Low-vol regime — system loosens, then rapid tightening when VIX spikes

**Portfolio state:** $100K equity. Low-vol regime for 2 weeks. VIX 12. Portfolio has grown comfortable: gross exposure 118% (low-vol limit: 130%), net long 65% (low-vol limit: 70%), 12 positions, theta $170/day (low-vol limit: 0.18% = $180/day).

**Market event:** Unexpected geopolitical event. VIX jumps from 12 → 38 in a single session. Regime reclassifies to **crisis**.

**New crisis limits:** Gross 60%, net long 30%, theta 0.05% ($50/day), per-position max 2%.

**Guardrail walkthrough:**

- *Continuous monitor detects:* Regime reclassification from low-vol → crisis (skipping normal and elevated). This is a regime jump — triggers an **emergency pipeline invocation** immediately rather than waiting for the next scheduled run.
- *Tightening is immediate.* New crisis limits active at the start of the emergency invocation.
- **Massive simultaneous breaches:**
  - Gross: 118% vs 60% limit → **58% overage** (nearly double the limit)
  - Net long: 65% vs 30% limit → **35% overage** (more than double the limit)
  - Theta: $170/day vs $50/day limit → **$120/day overage** (3.4× the limit)
  - Multiple positions above 2% per-position max
  - Drawdown: VIX spike likely caused significant portfolio losses → may also be in daily or cumulative drawdown breach
- *Emergency invocation fires:* All agents receive emergency context header: "EMERGENCY INVOCATION — trigger: Regime jump: low-vol → crisis (VIX 12 → 38)." The scheduled cadence resets from this point.
- *If daily drawdown also breached:* Halt mode activates concurrently. Engine handles drawdown breach immediately (closes worst-performing positions). Agents receive both emergency and halt-mode context. Exposure breaches are deferred to strategist → PM.
- *If daily drawdown NOT yet breached:* All exposure breaches are deferred to strategist → PM per regime-transition classification. Agents are not in halt mode — full action space available, but the emergency flag signals urgency.
- *Strategist faces the heaviest lift:* Must propose remedies to roughly halve gross exposure, halve net long, and cut theta by 70%. This likely means recommending closure of 5–6 positions. The strategist prioritizes using thesis-level context: weakened risk/reward, proximity to invalidation, catalyst timing. Each recommendation includes rationale.
- *PM reviews and executes:* Receives the strategist's remedy package alongside the cross-constraint impact summary. Validates that the proposed closures don't cascade into secondary breaches. May reorder priority or adjust sizing. Executes via command envelopes.
- *Practical concern:* Can the strategist and PM process this volume in a single invocation? The strategist must assess 12 positions and propose 5–6 closures with rationale; the PM must validate cross-constraint interactions for each and issue command envelopes. Context window strain is a real concern. Consider whether the strategist's ongoing position assessments should include pre-computed "if crisis regime, recommend closing this position" conditional logic to reduce in-the-moment reasoning load.
- *Second invocation (scheduled ~2 hours after emergency):* Agents reassess. If the first invocation resolved 3–4 breaches, the remaining breaches are smaller and the workload is manageable. If the VIX has stabilized, no further emergency triggers. If VIX is still elevated but stable, the system continues working through the overage at the normal cadence.

**Outcome:** The emergency invocation trigger eliminates the up-to-2-hour gap between the market event and the agent response. The system handles the regime jump with immediate tightening, an emergency invocation for agent reasoning, and strategist → PM resolution. **Finding: Without the emergency trigger, this scenario was the most dangerous gap in the design — 2 hours of massive breach overage with only mechanical engine actions available. The emergency invocation ensures the agents start reasoning about the response within minutes of the event, not hours. The 30-minute cooldown prevents runaway invocations if the VIX continues whipsawing during the session.**

---

### A11. Synchronized HTB buy-in cascade across a sector

**Portfolio state:** $100K equity. Three HTB short positions in small-cap biotech: BTX short 2.5% ($2.5K, 100 shares at $25), BIO short 2.5% ($2.5K, 50 shares at $50), CRL short 2% ($2K, 20 shares at $100). Aggregate short exposure 7% (limit 30%). Net long 28%, gross 35%. Daily P/L flat.

**Regime:** Normal.

**Market event:** Biotech sector experiences an unexpected positive regulatory catalyst (a surprise FDA decision accelerates pipeline approvals across a cohort of small-caps). Small-cap biotech ETF (XBI) gaps up 12% at open. All three HTB shorts rally 25–30% intraday. The broker's share lenders — long-biased funds with concentrated exposure to the same cohort — begin recalling borrowed shares to meet their own customers' sell-into-strength orders. Within a single trading session, the broker issues synchronized forced buy-in notices on all three positions.

This is the scenario the aggregate short cap was designed for. AlphaMind's Alpaca-hosted universe is ETB-only and HTB shorting is unsupported, so the literal forced-buy-in-from-lender-recall mechanic never fires in our deployment; the closest real-broker analogue is Alpaca's ETB→HTB overnight auto-closure when a previously-ETB name's borrow supply collapses. Either way, this scripted walkthrough validates the guardrail behavior under synchronized multi-position forced closures; the specific trigger mechanism is secondary to the response.

**Guardrail walkthrough:**

- *Fill reports arrive in sequence* as each buy-in resolves. Each carries `forced_buy_in: true`. BTX closes at ~$32 (28% loss on the short), BIO at ~$64 (28%), CRL at ~$128 (28%). Realized loss across the three: 0.28 × ($2.5K + $2.5K + $2K) = ~$1.96K (roughly 2.0% of portfolio).
- *Continuous monitor response per position:*
  - Each forced buy-in closes its own position — the OMS processes the fill as an involuntary closure. No engine-originated CLOSE is required; the buy-in *is* the close.
  - Position-level max loss rule: each short hits 28% loss, under the 30% equity max threshold. No protective action needed beyond the buy-in itself.
  - Single short size rule: the short position ticks above its entry size briefly on the rally (unrealized), but the buy-in closes it before sustained breach. No engine action.
- *Aggregate guardrail response:*
  - Aggregate short exposure drops from 7% → 0% as the three closures complete. No breach — reductions never trigger engine action.
  - Daily drawdown: ~2.0%, under the 2.5% limit. No halt engaged. If the combined loss had been larger (e.g., higher conviction sizing or deeper rallies), the halt would have engaged per A4's pattern and blocked any new OPEN/ADD until daily reset.
- *Emergency invocation trigger check:* synchronized forced buy-ins are not themselves a trigger condition — the trigger list is regime jumps, multi-rule simultaneous breaches, drawdown velocity, and margin calls. None apply here. No emergency fires.
- *PM context at next scheduled invocation:*
  - Three engine-originated `forced_buy_in` entries in the activity log, distinct from guardrail-breach-driven CLOSEs.
  - Strategist consumes the `engine_originated_closure_signal` anti-pattern cue (per [strategist.md](../04-decision-layer/strategist.md)): does any held long biotech position depend on the shorts persisting as a sector-weakness signal? Are there other HTB shorts in adjacent small-cap sectors that might be vulnerable to the same recall dynamic?
  - PM sees the 2.0% daily drawdown and factors it into sizing of any new proposals this session.

**Outcome:** Synchronized multi-position forced closures are handled cleanly by the closure mechanism itself — no engine-originated protective action is required because the broker-driven close *is* the close. The aggregate short cap is not exercised as a *constraint* in this scenario; it is exercised as a *bound* that kept the cumulative loss at a survivable ~2%. If the pre-squeeze aggregate exposure had been closer to the 30% cap, the same 28%-per-position forced loss would have translated to an ~8% portfolio hit, tripping the daily drawdown halt and engaging tier-1 cumulative drawdown restrictions. **Finding: the aggregate short cap does its job indirectly — by limiting how large a synchronized-squeeze loss can be, it bounds exposure to a tail scenario that per-position limits alone would not surface. The continuous monitor's engine-originated CLOSE authority is not the active safeguard here; the aggregate cap applied at position-entry time is. This is the scenario that motivates treating the aggregate short cap as a first-class portfolio-level guardrail rather than a derived consequence of per-position limits.**

---

## Part B — Primary portfolio ($1,500)

### B1. Normal regime — first trade on empty portfolio

**Portfolio state:** $1,500 cash, zero positions. Fresh portfolio.

**Regime:** Normal. Feature flags: options disabled, shorts disabled, fractional shares required, active sectors: tech + semis, max concurrent positions: 3.

**Proposed action:** Analyst proposes OPEN NVDA long, conviction 3, size 2% ($30, ~0.23 fractional shares at $133).

**Guardrail walkthrough:**

- *T1 analyst context:* Available capital: $1,350 (after 10% cash reserve = $150). Per-position max: $75 (5%). Tech sector: 0% / 25%. Net long: 0% / 60%. "Options: DISABLED. Short selling: DISABLED."
- *T1 validation:* Size $30 = 2%, within 5% max. PASS on all rules. But: $30 position at $133/share = 0.23 shares. Is this above `min_position_size: $20`? Yes, $30 > $20. PASS.
- *Pre-processor:* Single proposal, no conflicts.
- *T2 PM:* Approves. Straightforward first position.
- *T3 engine:* Checks position size (2% ≤ 5%). Checks that it's not an options or short trade (feature flags). PASS. Submits fractional share order.

**Outcome:** Clean first trade. **Finding: At $30, this position will generate meaningful data for thesis validation even if the absolute P/L is tiny. A 5% move on $30 is $1.50. The system's P/L tracking needs to handle very small dollar amounts without rounding errors becoming proportionally significant.**

---

### B2. Portfolio at max capacity — 3 positions, analyst proposes a 4th

**Portfolio state:** $1,500 equity. 3 positions: NVDA 2.5% ($37.50), AMD 2% ($30), AAPL 2% ($30). Cash: $1,402.50 (93.5%). Tech sector: 4.5%, semis: 2%. Net long: 6.5%.

**Regime:** Normal. Max concurrent positions: 3.

**Proposed action:** Analyst proposes OPEN MSFT long, conviction 4, size 3% ($45).

**Guardrail walkthrough:**

- *T1 analyst context:* Should show "Max concurrent positions: 3/3 [HARD BLOCK]. No new positions unless an existing position is closed."
- *T1 validation:* FAIL — max concurrent positions reached. Tool returns: "Portfolio at max concurrent positions (3). Close an existing position to make room."
- *Analyst:* Cannot submit a new OPEN proposal. Can note the MSFT opportunity for the strategist/PM to consider — if a weaker position should be closed to make room.

**Meanwhile, strategist:** Independently assessing all 3 positions. If POS-AAPL-001 has a stale thesis, the strategist might recommend CLOSE. If the PM closes AAPL, the next invocation has room for the MSFT entry.

**Outcome:** Max concurrent position limit works as a hard gate at T1. **Finding: The `max_concurrent_positions` constraint is critical for the primary portfolio — without it, the analyst could propose positions until capital runs out, but at $1,500, having more than 3 equity positions means each is so small that transaction costs (bid-ask spread) become proportionally significant. This feature flag serves as a practical quality gate, not just a risk limit.**

---

### B3. Position grows beyond max size due to market movement

**Portfolio state:** $1,500 equity. NVDA position entered at $30 (2% of portfolio). NVDA rallies 30%. Position now worth $39 → 2.6% of portfolio.

**Regime:** Normal. Per-position max: 5% ($75).

**Guardrail walkthrough:**

- *Continuous monitor:* 2.6% < 5%. No breach. No action.
- *Strategist at next invocation:* Notes the position is up 30% from entry. Evaluates thesis — is it near target? Should partial profits be taken? This is thesis-driven decision-making, not guardrail-driven.

**Now imagine NVDA rallies further.** Position entered at $30, NVDA up 80%. Position worth $54 → 3.6% of portfolio. Still under 5%.

**Now imagine extreme case.** NVDA doubles. Position worth $60 → 4% of portfolio. Still under 5%.

**At what point does the guardrail actually bind?** $30 entry → need 150% gain for position to reach 5% ($75). That's NVDA going from $133 to $332. On a 4–72 hour time horizon, this essentially never happens for a 2% position in a mega-cap.

**Outcome:** Position-level size guardrail almost never binds via market movement at this portfolio scale — the positions are too small relative to the limit. **Finding: For the primary portfolio, the `max_concurrent_positions` and `min_position_size` constraints are the binding constraints, not the percentage-based position size limit. The 5% limit is designed for the $100K portfolio where positions are $5,000 — meaningful size that can grow into limit territory. At $1,500, the practical constraints are structural (how many positions can you maintain) rather than exposure-based (how big can one position get). This is fine — the percentage rules don't hurt anything, they just aren't the binding factor.**

---

### B4. Daily drawdown halt at $1,500

**Portfolio state:** $1,500 equity at open. 3 positions: NVDA long $40 (2.7%), AMD long $35 (2.3%), TSM long $30 (2.0%). Total invested: $105 (7%). Cash: $1,395 (93%).

**Regime:** Normal. Daily drawdown limit: 2.5% = $37.50.

**Market event:** Broad tech selloff. All three positions drop 15%.

**P/L impact:** NVDA: −$6, AMD: −$5.25, TSM: −$4.50. Total daily loss: −$15.75, or −1.05% of portfolio.

**Guardrail walkthrough:**

- *Daily drawdown: 1.05% vs 2.5% limit.* Not even at WARNING zone (60% = 1.5%).
- *No halt. No escalation.* Normal operations continue.

**To actually hit halt:** Need −$37.50 daily loss. With $105 invested, that requires all positions to drop ~36% in a single day. For mega-cap tech stocks, this is essentially a market-structure-failure event (circuit breakers would halt trading well before this).

**What if the portfolio were fully invested?** Max 3 positions at 5% each = $225 invested (15% of portfolio). A 15% decline = −$33.75 (2.25% drawdown). Still under the 2.5% halt. Need ~17% decline across all positions in a single day.

**Outcome:** The daily drawdown limit almost never triggers at $1,500 because the portfolio is structurally under-invested — 93% cash with only 7% deployed. Even at maximum investment (15% deployed), you'd need an extraordinary single-day decline to trigger halt. **Finding: This is a significant asymmetry between the two portfolios. At $100K with 85% gross exposure, a 3% market decline could easily trigger the 2.5% daily drawdown halt. At $1,500 with 7–15% deployment, the same market decline causes a 0.2–0.5% portfolio drawdown — nowhere near the halt. The drawdown limits are effectively inert for the primary portfolio under normal conditions. This isn't necessarily a problem — the primary portfolio's risk is naturally limited by low deployment — but it means the primary portfolio won't validate the drawdown halt/progressive response mechanics. Those can only be tested on the full-system portfolio.**

---

### B5. Regime transition to crisis — what actually changes for primary portfolio?

**Portfolio state:** $1,500 equity. 2 positions: NVDA 2.5% ($37.50), AMD 2% ($30). Cash 95.5%.

**Regime change:** Normal → Crisis. VIX spikes to 40.

**New crisis limits for relevant rules:**
- Per-position max: 2% ($30)
- Sector concentration: 15%
- Net long: 30%
- Gross: 60%
- Min cash reserve: 25% ($375)
- Daily drawdown: 1.5% ($22.50)

**Guardrail walkthrough:**

- *Position-level breaches:* NVDA at 2.5% exceeds crisis max of 2%. AMD at 2% is exactly at limit.
- *Sector/exposure:* Tech 2.5% vs 15% — massive headroom. Net long 4.5% vs 30% — massive headroom. Gross 4.5% vs 60% — massive headroom.
- *Cash reserve:* $1,432.50 cash = 95.5% vs 25% minimum — far exceeds.

**The only binding constraint is the per-position max on NVDA.** Strategist proposes trimming NVDA from 2.5% to 2% — sell $7.50 worth of shares. PM reviews and executes.

**Outcome:** Crisis regime tightening barely affects the primary portfolio. The exposure-related rules (sector, directional, gross) have enormous headroom because the portfolio is 95% cash. **Finding: The primary portfolio is so structurally conservative (by necessity — can't deploy much capital with $1,500) that regime adaptation is largely irrelevant. The portfolio's risk is already well below crisis-level limits in normal mode. This means regime adaptation is essentially untestable on the primary portfolio. Again, this is fine — it's a feature of the $100K portfolio validation.**

---

### B6. Min position size constraint — analyst proposes a very small trade

**Portfolio state:** $1,500 equity. 2 existing positions. Cash $1,440.

**Regime:** Normal.

**Proposed action:** Analyst proposes OPEN AVGO long, conviction 2, size 0.5% ($7.50).

**Guardrail walkthrough:**

- *T1 validation:* Size $7.50 < `min_position_size` $20. FAIL — "Position size $7.50 is below minimum viable size of $20. Increase size or drop recommendation."
- *Analyst:* Increases to conviction 2 advisory band lower end = 0.5% = $7.50. Still fails. Advisory band for conviction 2 is 0.5–1.5%. At 1.5% = $22.50, which is above the $20 minimum.
- *Analyst revises to 1.5%:* $22.50. PASS on min size. PASS on all other rules.

**Outcome:** Min position size prevents impractically tiny trades. **Finding: The min_position_size constraint interacts with the conviction scale bands. At $1,500, conviction 1 band (0.25–0.75% = $3.75–$11.25) is entirely below the $20 minimum. Conviction 2 lower end (0.5% = $7.50) is also below. Only conviction 2 at 1%+ ($15 — still below $20) or conviction 3+ at the lower band begins to be viable. This effectively means the primary portfolio can only take conviction 2+ trades at the upper end of the sizing band, or conviction 3+ trades. This is actually a desirable constraint — at $1,500, you shouldn't be taking low-conviction speculative positions. But it should be documented that the effective minimum conviction for the primary portfolio is ~2–3, not 1.**

---

### B7. Analyst proposes a trade in a disabled sector

**Portfolio state:** $1,500 equity. Active sectors: tech, semis. 1 position in tech.

**Regime:** Normal.

**Proposed action:** Analyst proposes OPEN XOM long (energy sector), conviction 4.

**Guardrail walkthrough:**

- *T1 analyst context:* "Active sectors: [tech, semis]. Other sectors: DISABLED."
- *T1 validation:* FAIL — "Energy sector is not in the active sector list for this portfolio."
- *Alternatively:* This should be caught before the validation tool — the analyst's guardrail state header should make active sectors unambiguous, and the analyst shouldn't generate proposals for disabled sectors in the first place.

**Outcome:** Sector restriction works correctly. **Finding: The `active_sectors` feature flag is straightforward. But consider an edge case: what if the synthesizer's output highlights a strong energy opportunity? The analyst sees it in the synthesizer brief but knows it can't act on it. Should the analyst note "strong XOM setup identified but energy sector disabled for this portfolio" in its output? This could be useful for the $100K portfolio's analyst (running in parallel on the same synthesizer output) as a cross-portfolio signal.**

---

### B8. Attempting to deploy options — feature flag enforcement

**Portfolio state:** $1,500 equity. 1 position. Analyst received synthesizer output highlighting unusual NVDA options activity.

**Proposed action:** Analyst (incorrectly or by design) proposes OPEN NVDA 135C, 1 contract, $3.50 premium ($350).

**Guardrail walkthrough:**

- *T1 analyst context:* "Options: DISABLED for this portfolio."
- *T1 validation:* FAIL — `feature_disabled: options`. Immediate rejection, no further rule checking.
- *Analyst:* Should not have generated this proposal given context. If it did, the validation tool catches it. Analyst can re-propose as an equity position if the thesis supports a directional long.

**If the analyst's context was somehow corrupted and the proposal reached the PM:**
- *T2 PM validation:* Same check — `feature_disabled: options`. FAIL.
- *T3 engine:* Same check. FAIL. Would never execute.

**Outcome:** Triple-layer defense against disabled features. **Finding: The feature flag check should be the very first check in the validation tool — before any exposure or sizing computation. This avoids wasting compute on greek calculations for a trade that's immediately rejected. Also note: at $350 premium, this would be 23% of the portfolio. Even if options were enabled, the 5% max position size would reject it. The feature flag catches it first, but the sizing limit provides a second independent gate.**

---

### B9. Cumulative drawdown on the primary portfolio

**Portfolio state:** Started at $1,500. High-water mark $1,520 (after some early gains). Current equity: $1,400. Cumulative drawdown: 7.9% — just under the 8% limit.

**This is notable because:** The portfolio has lost $120 from peak. At $1,500 starting with 7–15% deployment, how did the portfolio lose $120? That requires losing $120 on ~$105 invested — a 114% loss, which is impossible on long equity (max loss is 100%, and bracket stops should fire well before that).

**Let me reconstruct plausibly:** Portfolio peaked at $1,520 after several winning trades. Then 3 consecutive losing trades, each stopped out at the bracket invalidation level. Each position was ~$40 (2.7%), each lost 30% = −$12. Three losses = −$36. But that only brings equity to $1,484, which is a 2.4% drawdown. Not close to 8%.

**To reach 7.9% drawdown from $1,520:** Need equity at $1,400 = $120 loss. With max deployment of ~$225 (3 positions at 5%), and 30% max loss per position = $67.50 total if all 3 hit max loss simultaneously. Still only 4.4% drawdown.

**Conclusion:** At the primary portfolio's structural deployment level, reaching 8% cumulative drawdown requires either: (a) many sequential losing trades over multiple weeks, or (b) something breaking in the bracket stops (gaps through stops without fills). Under normal operation with functioning stops, the primary portfolio cannot realistically reach 8% cumulative drawdown within a short timeframe.

**Outcome:** The cumulative drawdown limit is extremely unlikely to bind on the primary portfolio. **Finding: This confirms the pattern from B4 and B5 — the primary portfolio's low deployment rate means drawdown-based guardrails are structurally inert. The portfolio's actual risk protection comes from bracket stops (thesis-based), the max concurrent position limit, and the small absolute position sizes. The percentage-based drawdown system is validated by the $100K portfolio, not this one.**

---

### B10. Regime loosening on primary portfolio — any benefit?

**Portfolio state:** $1,500 equity. Low-vol regime. 2 positions at 2% each. Cash 96%.

**Loosened limits (low-vol):**
- Per-position max: 6% ($90)
- Sector concentration: 28%
- Net long: 70%
- Gross: 130%
- Min cash reserve: 8% ($120)

**Does loosening help?** The portfolio's binding constraints are:
1. Max concurrent positions: 3 (unchanged by regime)
2. Available capital: $1,440 (unchanged by regime)
3. Min position size: $20 (unchanged by regime)
4. Conviction bands: determine sizing within 0.25–5% (unchanged by regime)

The loosened per-position max (6% vs 5%) means a conviction-5 trade could be sized at $90 instead of $75. That's $15 more per position. The loosened net long (70% vs 60%) is irrelevant — the portfolio is at 4% net long.

**Outcome:** Regime loosening provides marginal benefit to the primary portfolio — at most ~$15 additional sizing capacity per conviction-5 trade. **Finding: The primary portfolio's constraints are structural (position count, capital, minimum size), not regime-sensitive (exposure limits). Regime adaptation exists for completeness and percentage-rule consistency across both portfolios, but its practical impact on the primary portfolio is negligible.**

---

## Synthesis — findings and issues

### Validated design elements

1. **Cumulative T1 tracking** (A2): The guardrail validation tool's cumulative tracking across multiple proposals within an invocation works correctly and catches the common scenario of individually-compliant proposals that collectively breach a limit.

2. **Regime transition deferral** (A3, A10): Deferring regime-transition breaches to strategist → PM rather than mechanical forced reduction is the right call — the strategist has thesis-level context to propose which positions to trim, and the PM validates cross-constraint interactions before executing.

3. **Options greeks guardrails** (A5): Delta, theta, and vega limits correctly constrain options concentration. The conservative delta buffer ensures T1 approvals survive to T3.

4. **Feature flag enforcement** (B7, B8): Triple-layer defense against disabled features works cleanly. Feature checks should be first in the validation pipeline.

5. **Engine-originated protective actions** (A6): Position-level max loss and short-size forced reduction work correctly between invocations, with clean traceability.

6. **Margin cascade** (A7): Priority ordering and cascade detection work as designed.

7. **Pre-event overlay** (A9): Correctly tightens sizing and blocks new positions in the final pre-event invocation, while leaving sector-specific event sensitivity to PM judgment.

### Issues and gaps identified

**Issue 1 — Halt mode agent behavior (A4) [RESOLVED]:** During halt, each agent receives modified context/instructions. The analyst switches to watchlist mode (compact opportunity tracking, no full proposals). The strategist shifts to defensive posture (focus on existing position risk, thesis deterioration, stop tightening). The PM operates in risk-reduction mode (CLOSE, ADJUST, CANCEL only). The upstream pipeline (distillation, research) runs normally, informing the strategist's defensive reasoning. Design specified in [breach-behavior.md](breach-behavior.md#agent-behavior-during-halt-mode) and [state-delivery.md](state-delivery.md#halt-mode-header-modifications).

**Issue 2 — Two-regime jump severity (A10) [RESOLVED]:** A jump from low-vol to crisis creates extreme overages. The continuous monitor now triggers an **emergency pipeline invocation** when a regime jump is detected, eliminating the up-to-2-hour gap between the market event and agent response. The emergency context header includes the trigger detail (e.g., "Regime jump: low-vol → crisis") which provides the urgency signal. The 30-minute cooldown prevents runaway invocations during sustained volatility. Design specified in [breach-behavior.md](breach-behavior.md#emergency-invocation-trigger).

**Issue 3 — Primary portfolio drawdown inertness (B4, B5, B9):** The percentage-based drawdown limits (daily 2.5%, cumulative 8%) are structurally inert on the primary portfolio because deployment is so low (7–15% of capital). These mechanics can only be validated on the $100K portfolio. This should be documented as an explicit scope limitation of the primary portfolio's validation. Not a design flaw — the primary portfolio is protected by structural conservatism rather than drawdown guardrails.

**Issue 4 — Effective minimum conviction at $1,500 (B6):** The `min_position_size: $20` constraint makes conviction 1 trades impossible and conviction 2 trades viable only at the upper end of the sizing band. The primary portfolio's effective conviction floor is ~2–3. This is actually desirable (don't take speculative positions with $1,500) but should be documented in the dual portfolio profile.

**Issue 5 — Regime adaptation irrelevance at $1,500 (B5, B10):** Regime adaptation has negligible practical impact on the primary portfolio. Constraints that bind are structural (position count, min size, capital) rather than regime-sensitive. This is expected and acceptable but should be noted as a testing gap for the primary portfolio.

**Issue 6 — Cross-portfolio signal sharing (B7):** When the primary portfolio's analyst can't act on an opportunity due to sector restrictions, that signal is lost. Consider having the primary analyst note disabled-sector opportunities in its output, which could inform the $100K portfolio's analyst or serve as a future record when the primary portfolio eventually expands its sector coverage.

**Issue 7 — Strategist/PM workload during crisis transition (A10):** A low-vol → crisis jump with 12 positions may require closing 5–6 positions in one invocation. The strategist must assess all 12 positions and propose remedies with thesis-based rationale; the PM must validate cross-constraint interactions and issue command envelopes for each. This is a significant load on both agents and may strain context windows. Consider whether the strategist's ongoing position assessments should include pre-computed "if crisis regime, recommend closing this position" conditional logic to reduce in-the-moment reasoning load during regime jumps.
