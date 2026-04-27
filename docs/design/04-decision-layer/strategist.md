# Strategist

Operates in a fresh context window. **Mandate:** evaluate every open position against current market conditions and recommend portfolio actions. Connects synthesizer information to existing thesis narratives, classifies thesis health, identifies cross-position dynamics, and recommends actions with rationale. Sole agent responsible for thesis-status assessment.

Runs in parallel with the [analyst](analyst.md). Outputs converge at the [portfolio manager](portfolio-manager.md), which evaluates both holistically.

---

## Inputs

Delivered at invocation start. Source documents are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Synthesizer brief | [synthesizer.md](../03-analysis-layer/synthesizer.md) | Prose synthesis with embedded `[SA-*]`, `[QR-*]`, `[AR-*]`, `[CR-*]` references — the new information against which existing theses are evaluated |
| Full thesis records (component level) | [portfolio-state.md §3a](../01-data-layer/internal/portfolio-state.md), [thesis-model.md](../05-execution-layer/thesis-model.md) | Every active thesis at component level (entry rationale, target rationale, invalidation rationale per leg, key assumptions, `prior_status`). Strategist is the sole consumer that receives components inline — thesis-status classification is per-component |
| Position details | [portfolio-state.md §1a, §2a](../01-data-layer/internal/portfolio-state.md), [position-model.md](../05-execution-layer/position-model.md) | Per-position P/L trajectory, current market value, position age, distance to target/stop, risk/reward ratio at current price |
| Activity log | [portfolio-state.md §5](../01-data-layer/internal/portfolio-state.md) | Intra-invocation changelog (5a), PM decision log sliding window (5b), per-position modification trail (5c) — continuity with prior invocations and engine-originated actions |
| Pending orders | [portfolio-state.md §4b](../01-data-layer/internal/portfolio-state.md) | Unfilled orders with age, fill-probability context, and the justifying thesis. Drives [Pending order review](#pending-order-review) |
| Strategist guardrail state header | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) | Formatted text block at top of prompt: regime label, sector/directional headroom, position-level constraint proximity, sector exposure breakdown, drawdown state, regime-transition breaches with `BREACH-N` IDs, hard blocks |
| Abandoned openings block | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (abandoned-openings section) | Prior-invocation OPEN abandonments — portfolio-awareness context; analyst owns re-evaluation |
| Abandoned position actions block | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (abandoned-position-actions section) | Prior-invocation ADD / ADJUST / CLOSE / CANCEL abandonments — for re-evaluation on current grounds. See [Abandoned position actions from prior invocation](#abandoned-position-actions-from-prior-invocation) |

Tools available during reasoning:

| Tool | Source | Use |
|---|---|---|
| Source-brief retrieval | [decision-layer overview — Information flow](README.md#information-flow) | Pull a section of an analysis brief by reference ID. See [Source brief retrieval](#source-brief-retrieval) |
| Guardrail validation tool | [state-delivery.md — Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) | Deterministic pre-submission check on exposure-changing proposals. See [Pre-submission guardrail validation](#pre-submission-guardrail-validation) |

The volatility regime label is delivered as the `Regime:` line in the guardrail state header.

**Token budget:** the input bundle scales with portfolio complexity — 400–700 tokens for primary portfolio (1–4 positions), 1,500–2,500 tokens for full-system portfolio (6–15 positions), excluding the synthesizer brief. See [Presentation order and token budget](#presentation-order-and-token-budget).

---

## Output

A single document conforming to the [strategist output schema](strategist-output-schema.md) (Draft 2020-12). The schema is the authoritative contract; the field lists in [Output structure](#output-structure) are the readable reference. Each per-position assessment carries **structured fields** (consumed by the [proposal pre-processor](proposal-pre-processor.md)) and **narrative fields** (consumed by the [portfolio manager](portfolio-manager.md)). The output also carries pending-order assessments and portfolio-level observations.

Mode is `normal` under standard conditions, `defensive_posture` under halt mode — see [Halt mode and defensive-posture behavior](#halt-mode-and-defensive-posture-behavior).

---

## Responsibilities

- **Thesis status classification.** For each open position, assess thesis health by cross-referencing thesis narrative and key assumptions against current context. Classify on-track, partially-realized, at-risk, stale, or invalidated (see [thesis-model.md](../05-execution-layer/thesis-model.md)). Signal-level reasoning — e.g., "`[QR-7]` directly contradicts the supply chain signal that supported entry, moving thesis from on-track to at-risk."
- **Action recommendation.** Recommend hold, reduce, close, adjust-bracket, or add. Non-hold recommendations include concrete parameters (quantity, order type, bracket changes, close rationale type).
- **Cross-position reasoning.** Identify portfolio-level dynamics no single-position assessment would catch: correlation shifts, shared-catalyst dependency overlaps, sector concentration trends, rebalance opportunities.
- **Pending order review.** Evaluate unfilled orders — maintain, modify, or cancel given current conditions.

---

## Output structure

The strategist produces a position assessment for every open position plus any pending orders. The PM receives the output as a complete book review alongside the analyst's new trade proposals, with [proposal pre-processor](proposal-pre-processor.md) annotations layered on top.

**Per-position assessment — structured fields:**

- **Assessment ID:** unique within invocation (e.g., `SA-1`, `SA-2`)
- **Position ID and thesis ID:** which position this assessment covers
- **Underlying:** root ticker — used by the pre-processor for same-name conflict detection
- **Sector:** which sector
- **Thesis status:** on-track, partially-realized, at-risk, stale, or invalidated. Signal criteria in [Thesis status classification methodology](#thesis-status-classification-methodology)
- **Prior status:** classification from the previous invocation — surfaces transitions (e.g., on-track → at-risk)
- **Recommended action:** hold, reduce, close, adjust-bracket, or add. Selection criteria in [Action decision logic](#action-decision-logic)
- **Action parameters** (required for non-hold):
  - close: quantity (partial or full), order type (market or limit with price), close rationale type (thesis-invalidated, target-reached, conviction-reduced, risk-management)
  - reduce: quantity, order type
  - adjust-bracket: specific changes (new stop level, target, time horizon, event invalidation)
  - add: additional quantity, entry order parameters, bracket adjustment if needed
- **Exposure impact** (non-hold): change in delta-adjusted exposure for the sector, plus net directional impact. For add: populated by the guardrail validation tool. For close/reduce: computed from current position data
- **Guardrail validation result** (for add, and for reduce/close whose exposure impact interacts with a flagged constraint): pass/fail summary from pre-submission check
- **Remedy flag** (optional): identifier of a regime-transition or market-movement breach this assessment addresses (e.g., `BREACH-1`). See [Regime-transition remedy proposals](#regime-transition-remedy-proposals)

**Per-position assessment — narrative fields:**

- **Status rationale:** signal-level reasoning with source references (e.g., `[QR-7]`, `[SA-TECH-3]`) — what supports or undermines the thesis. Transitions must explain what drove them
- **Action rationale:** what new information drives the recommended action. For close with thesis-invalidated: the specific invalidation reason
- **Reduce rationale** (reduce only): why partial rather than full, and how the quantity maps to the weakened thesis portion (or breach overage, for remedy reductions)
- **Add conviction justification** (add only): the strengthening signal absent at entry. A validating signal ("the thesis is playing out") is a hold reason, not an add reason
- **Adjustment rationale** (adjust-bracket only): what new information makes the original parameter wrong
- **Remedy rationale** (when remedy flag is present): why this action (trim-to-compliance / close / hold-with-rationale) is the right response given the thesis state
- **Cross-position observations** (optional): portfolio-level dynamics relevant to this position — e.g., "correlation with POS-AMD-001 has increased from 0.3 to 0.7 this week, creating unintended concentration"

**Pending order assessment — structured fields:** see [Pending order review](#pending-order-review).

**Portfolio-level observations** (after all position assessments, narrative only):

- Aggregate thesis health: distribution across status categories; direction of movement since prior invocation
- Sector balance shifts: whether recommended actions alter the sector exposure profile
- Thesis dependency warnings: positions sharing catalysts or assumptions whose simultaneous adverse firing would invalidate several at once
- Capital allocation observations: whether the book is capital-efficient given aggregate thesis-quality distribution
- Regime-transition summary (when applicable): flagged breaches addressed by per-position remedies, and any remaining uncured with rationale

---

## Thesis status classification methodology

Every per-position assessment sets `thesis_status` to one of five values, defined by signal characteristics — presence or absence of current-invocation signals bearing on the thesis — not by P/L. A position can be deeply profitable and `at-risk` (the move played out, new information undermines the residual case) or deeply underwater and `on-track` (catalyst hasn't fired, nothing has contradicted).

| Status | Signal criteria |
|--------|----------------|
| **on-track** | Key assumptions from the thesis record remain consistent with current signals. The named catalyst has not yet fired, or the position is moving toward the target for reasons the thesis predicted. No new invocation signal materially undermines any thesis component. |
| **partially-realized** | The named catalyst has partially fired and some thesis components are validated by current signals, but the target has not been reached. The residual case is intact but narrowed — the remaining potential is smaller than at entry, and the evidence supporting the remaining leg should be re-examined. |
| **at-risk** | A current-invocation signal weakens the thesis without invalidating it. Examples: a supporting signal cited in the entry rationale has faded or reversed; a correlated position has moved against the pattern the thesis depended on; a new finding creates ambiguity on a key assumption; prediction-market or flow data has repriced away from the thesis direction. The thesis still has a coherent path, but the margin for further adverse news has narrowed. |
| **stale** | Position age has exceeded `time_expectation_hours` without the catalyst firing or the target being reached, and no current-invocation signal moves the thesis into any of the other four categories. Staleness is the default when nothing has happened — neither confirmation nor contradiction — past the expected resolution window. |
| **invalidated** | A specific falsifiable claim in the thesis has been proven wrong by a current-invocation signal, or an event-based invalidation leg's condition has been met. Invalidation requires a named signal or event, not an inference. Position-level max loss triggers are handled by the engine, not by this status — invalidation is a thesis-level judgment. |

### Borderline cases

- **on-track vs. at-risk.** Default to `at-risk` when a plausible reading of a current signal weakens a cited entry component. The rationale should name the signal and component. Under-classifying to `on-track` when a supporting signal has faded suppresses information the PM needs.
- **at-risk vs. invalidated.** `invalidated` requires a named falsifier — a specific event leg condition met, or a key assumption contradicted by a cited signal. Generic readings ("the tape looks wrong," "momentum has stalled") stay at `at-risk`. Invalidation commits to close; its signal bar is correspondingly higher.
- **at-risk vs. partially-realized.** `partially-realized` requires affirmative evidence that part of the catalyst has fired and part of the thesis has been confirmed. A position up in P/L without the catalyst firing is still `on-track` or `at-risk` — P/L is not a classification input.
- **stale vs. at-risk.** If aged past time expectation and a current signal weakens the thesis, classify `at-risk`. `stale` is reserved for the no-news case.
- **stale vs. invalidated.** Age alone does not invalidate. An overdue catalyst that could still fire stays `stale`; one publicly confirmed not to fire (earnings contradicted the thesis, regulatory event resolved against it) becomes `invalidated`.

### Prior status and cross-invocation continuity

Each thesis record carries a `prior_status` field — the classification from the immediately preceding invocation. The `status_rationale` must explain any transition by naming what changed.

- **Transition requires a cited signal.** `on-track → at-risk` requires a specific finding that weakened the thesis this invocation. `at-risk → on-track` requires a specific finding that restored it. Silent transitions are a status-warrant failure the PM will reject.
- **Repeated classifications still cite fresh signals.** Three consecutive `on-track` classifications are legitimate only if each is anchored to current-invocation evidence, not the entry thesis. Rehashing entry rationale under a repeated `on-track` is the single-invocation symptom of the `sunk_cost_persistence` anti-pattern.
- **Repeated `at-risk` or `stale` without transition is itself a signal.** Across multiple invocations without escalation to `invalidated` or de-escalation to `on-track`, the position is drifting — note the aggregation in `cross_position_observations` or `portfolio_level_observations` and consider whether continued hold remains warranted.

### Calibration target

Analogous to the [analyst's conviction scale calibration](analyst.md#conviction-scale): a well-calibrated strategist exhibits distinct forward-outcome distributions across the five statuses tracked over time.

- `invalidated` positions should close into realized losses. A high recovery share suggests the classification is being applied too loosely.
- `at-risk` positions should exhibit higher forward adverse-outcome rates than `on-track`. Indistinguishable outcomes mean one is being applied reflexively.
- `on-track` should correlate with continued thesis validation. A high share followed by P/L decline without a preceding `at-risk` transition indicates under-classification.
- `stale` should be followed by the catalyst firing (transition to `partially-realized` or `on-track`), invalidation, or a PM-approved close. Long dwell in `stale` without transition means the strategist failed to re-examine.

Tracked across invocations by the feedback loop (see [thesis quality trends](../01-data-layer/internal/portfolio-state.md) category 6); the strategist does not enforce per-invocation.

---

## Action decision logic

For each position, choose one `recommended_action` from `hold`, `reduce`, `close`, `adjust-bracket`, or `add`. The status classification constrains the action space; the action maps to structured parameters and narrative rationale.

### Per-action signal criteria

| Action | Signal criteria |
|--------|----------------|
| **hold** | Status is `on-track` and no new information warrants a parameter change. Holds on `partially-realized`, `at-risk`, and `stale` are permitted but require explicit rationale (see below). Holds on `invalidated` are not permitted. |
| **reduce** | A specific thesis component has weakened without the whole thesis being invalidated (maps to `partially-realized` or component-level `at-risk`); OR the position is a regime-transition / market-movement remedy trim; OR conviction has decayed to a level where the position is over-sized relative to remaining signal. |
| **close** | Status is `invalidated` — an event leg fired or a falsifiable claim was contradicted; OR the target has been reached (resolution `target_reached`); OR conviction has decayed to the point where the full residual case no longer warrants any position; OR portfolio-level risk demands a full exit (resolution `risk_management`). |
| **adjust-bracket** | A current signal changes the appropriate invalidation level (tighten a stop on confirmation, revise an event leg as new catalysts are named) or target level (extend on strengthening, pull in on partial realization). Bracket-widening and time-extension have an elevated burden of proof — see [LLM failure mode avoidance](#llm-failure-mode-avoidance). |
| **add** | A strengthening signal absent at entry has appeared — new convergent evidence that was not part of the original thesis. P/L improvement is not a strengthening signal. A validating signal ("the thesis is playing out as expected") is a hold reason, not an add reason. |

### Full-vs.-partial reasoning for close and reduce

Justify the quantity, not just the action type.

- **Full close.** Required for `invalidated`. Default when the target is reached; partial target-taking is discretionary, and a partial close on `target_reached` requires a reason residual exposure is warranted (target may extend, remainder has an independent secondary thesis).
- **Partial close or reduce.** When a specific thesis component has weakened but others remain intact. Reduction quantity should map to the failed portion — a non-core key-assumption failure warrants a smaller reduction than a core-mechanism failure. A reduce without a quantity-portion mapping fails the PM's action-specific justification criterion.
- **Full close on `at-risk`.** Permitted when weakening is severe enough that the residual case does not justify any remaining exposure. The rationale must explain why partial is insufficient — usually because the weakening bears on the core mechanism.

### Add burden of proof

Adds increase exposure on an existing thesis; the failure mode is treating continued validation as a reason to double down.

- **Strengthening signal required.** The add rationale must name a current-invocation signal not part of the entry thesis — new convergent evidence, a fresh aligning catalyst, an institutional flow signal absent at entry.
- **Validating signals are not strengthening signals.** "The catalyst is firing as expected," "price approaching target," "prediction markets moved in our favor" are validations. If the thesis is playing out as expected, the original sizing was correct — adding implies the original sizing was wrong, which requires a reason the entry underestimated the signal.
- **Adds on non-`on-track` status.** `partially-realized` adds require an explicit argument for why the remaining catalyst warrants more exposure when the original case has already partly resolved. Adds on `at-risk`, `stale`, or `invalidated` are not valid pairings.

### Default stance on bracket-widening and time-leg extension

Both are mechanically `adjust-bracket` but represent the two movements most associated with rationalized continuation: loosening the stop, extending the horizon.

- **Default read: rationalization.** Treat both as rationalization unless the rationale names a concretely new signal that makes the original level wrong.
- **Narrow exception — time-leg extension.** A publicly rescheduled catalyst (earnings delayed to a specific new date, trial date moved) justifies extending to the new timestamp. "Might fire soon" does not qualify. A post-catalyst reaction window justifies a modest extension tied concretely to the reaction window.
- **Narrow exception — stop-widening.** A revised technical level supported by a current cited signal (volatility expanded beyond the original stop's rationale, a specific support/resistance revision) may justify widening. "The stop was too tight" does not qualify.

The adjustment rationale must pair old level with new and cite the signal. Terse or generic rationales fail the PM's action-specific justification criterion.

### Hold rationale requirements by status

`hold` is the most-chosen and most sunk-cost-prone action; rationale requirements scale with status.

- **`on-track` hold.** Default. Rationale cites current signals confirming the thesis. A hold with no cited current signal — rehashing entry rationale — fails the PM's status classification warrant.
- **`partially-realized` hold.** Requires a reason the residual case still justifies full-size exposure. If smaller than the original case, a `reduce` is usually more honest.
- **`at-risk` hold.** Requires a reason the weakening signal does not warrant size reduction, or why asymmetry still favors the residual case. Approved `at-risk` holds should typically pair with complementary bracket tightening (see [portfolio-manager.md existing-position guidance](portfolio-manager.md#aging-theses)). Unguarded holds fail action-status alignment.
- **`stale` hold.** Requires a specific reason the horizon should be extended — usually a publicly rescheduled catalyst or post-catalyst reaction window. "Catalyst expected soon" is not a rationale. The PM default-rejects without a rescheduled-catalyst rationale.
- **`invalidated` hold.** Not permitted. If the thesis is not actually invalidated, the status should be `at-risk`. `hold` + `invalidated` is self-contradictory.

---

## LLM failure mode avoidance

The [portfolio manager](portfolio-manager.md#anti-patterns-the-pm-is-watching-for) polices anti-patterns across proposals from both agents. The strategist surfaces these most frequently — the cadence of hold/add/adjust decisions is where drift accumulates. The strategist's job is to catch the single-invocation manifestation before the PM has to reject it.

Patterns below mirror the PM's rejection criteria, named in the canonical form the PM rationale uses so the feedback loop aggregates on the same strings whether caught at the strategist or rejected at the PM.

### Sunk-cost persistence

*Pattern:* continuing to hold a position whose status has been `at-risk` or `stale` across multiple prior invocations without a new supporting signal. Classification drifts as the strategist extends benefit of the doubt rather than escalating to `invalidated` or recommending close.

*Mirror-image discipline.* When `prior_status` shows two or more consecutive non-`on-track` classifications without transition to `on-track` or `invalidated`, the continued hold requires justification, not the close. Cite a new supporting signal that explains continued patience, or escalate.

### Rationalized continuation

*Pattern:* an `adjust-bracket` widening a stop or extending a time horizon, or an `add` to a losing position, framed as responding to new information but whose rationale restates the entry thesis. The structural shape is an action that loosens the invalidation envelope or increases exposure without citing a current-invocation signal that was not in the original thesis.

*Mirror-image discipline.* Treat bracket-widening, time-leg extension, and adds with a default-reject stance on yourself — require a named current-invocation signal that is genuinely new. If the rationale reduces to "the thesis still applies, the position just needs more room / more time / more size," the correct action is `hold` or `reduce`. See [Default stance on bracket-widening and time-leg extension](#default-stance-on-bracket-widening-and-time-leg-extension) and [Add burden of proof](#add-burden-of-proof).

### Thesis-contradiction suppression

*Pattern:* a status rationale omits a contradiction or uncertainty the synthesizer flagged bearing on the thesis. The strategist reads past it and produces a classification that would differ if addressed.

*Mirror-image discipline.* When the synthesizer flags a contradiction bearing on a cited entry signal, the status rationale must address it — either explain why it does not overturn the thesis (and downgrade status to reflect residual uncertainty) or acknowledge it as moving the thesis to `at-risk` or `invalidated`. Use source-brief retrieval to verify characterization.

### Engine-originated closure as a signal

*Pattern:* an engine-originated envelope appears in the activity log (a position-level max-loss force-close, a sector-trim for concentration breach), and the strategist treats adjacent positions in the same sector or sharing the same catalyst as if nothing happened. The engine action is a real-time regime signal.

*Mirror-image discipline.* When the activity log surfaces a recent engine-originated envelope, explicitly assess whether the closed position's thesis shared components with any currently-held position. Use `cross_position_observations` to name the connection — e.g., "POS-AMD-002's thesis shares the hyperscaler-capex leg that drove POS-AVGO-001's engine-forced close at 10:47; downgrading POS-AMD-002 from `on-track` to `at-risk` to reflect the shared exposure." Silent omission fails the PM's portfolio-coherence criterion.

### Generic-rationale avoidance

*Pattern:* rationales that could be cut-and-pasted across any position — "thesis continues to play out," "still watching the catalyst," "maintaining position for now," "conditions remain favorable." Particularly acute for repeated `hold` on long-held positions.

*Mirror-image discipline.* Every status rationale cites at least one current-invocation source reference (`[SA-TECH-N]`, `[QR-N]`, `[AR-N]`, `[CR-N]`) or a specific portfolio-state component (position age, P/L trajectory, distance to stop). Every action rationale either cites a new signal (non-`hold`) or names the absence of any status-changing signal (`hold`). If a rationale could be written without having read this invocation's synthesizer brief, it is generic.

---

## Pending order review

The strategist evaluates every unfilled order (entry limits, take-profit legs, stop-limit legs) carried from prior invocations. Each gets a structured assessment; the pre-processor passes them to the PM, which makes the final maintain/modify/cancel decision.

### Structured fields

- **Pending order assessment ID:** unique within invocation (e.g., `SA-ORD-1`)
- **Order ID:** the specific order
- **Position ID:** entry orders reference a pending position; bracket legs reference an active position
- **Order type:** `entry_limit`, `entry_stop_limit`, `bracket_target`, `bracket_price_stop`, `bracket_time_stop`, or `bracket_event_stop`
- **Order age hours:** hours elapsed since placement
- **Current distance:** for price-anchored orders, distance from underlying to trigger/limit as a percentage
- **Fill probability assessment:** `likely_soon`, `plausible`, or `unlikely` — qualitative read on underlying movement relative to the order level
- **Recommended action:** `maintain`, `modify`, or `cancel`
- **Modification parameters** (when `modify`): new limit price, trigger price, deadline, or order type
- **Linked position assessment** (optional): the parent position's `SA-N` assessment ID

### Narrative fields

- **Drift rationale:** how conditions have shifted since placement — signals bearing on fill probability or thesis-justification
- **Action rationale:** why maintain / modify / cancel given drift. For `cancel`: whether the parent thesis is intact with a different execution path, or whether the cancellation reflects a thesis status change captured in the per-position assessment

### Treatment of aged orders and fill-probability drift

The principal failure mode is neglect — an order placed a day ago on conditions that no longer apply gets passively extended.

- **Aged orders without a current fill-probability assessment.** When age meaningfully exceeds the parent thesis's time-scale (entries past the entry window's implied decay, bracket legs where the underlying has drifted well away without triggering), the default is `cancel` unless current signals specifically support maintaining.
- **Fill-probability drift.** A `likely_soon` at placement becomes `unlikely` when the underlying has moved. Re-assess against current conditions, not conditions at placement.
- **Bracket legs on positions whose status has changed.** If the parent's status moved to `at-risk` or `invalidated`, the bracket leg's entry-time rationale may no longer apply. A bracket target on an `invalidated` position should be `cancel` (as part of full close), not `maintain`.

### Maintain / modify / cancel criteria

- **maintain.** Conditions unchanged since placement; fill probability still appropriate; parent thesis intact at the order's implied level. Rationale must cite what has not changed.
- **modify.** Thesis intact but parameters no longer reflect conditions — entry limit moves with the underlying, time leg extends for a publicly rescheduled catalyst, target pulls in because partial realization narrowed remaining upside. Same rationale burden as `adjust-bracket`: new parameter paired with old and cited signal (see [Default stance on bracket-widening and time-leg extension](#default-stance-on-bracket-widening-and-time-leg-extension)).
- **cancel.** Order no longer thesis-justified: entry opportunity passed, parent thesis invalidated, parent classification moved beyond the order's rationale, or fill probability decayed below capital-reservation warrant. Rationale names the specific reason.

The PM applies its existing-position modification authority to pending-order assessments (see [portfolio-manager.md scope](portfolio-manager.md#scope-of-pm-authority-for-existing-position-envelopes)): within-action parameter modifications and risk-reducing complementary commands are permitted; action replacement requires rejection, which carries the disagreement to the next invocation.

---

## Abandoned position actions from prior invocation

ADD, ADJUST, CLOSE, and CANCEL commands approved by the PM but abandoned at broker submission (per [state-persistence.md](../05-execution-layer/state-persistence.md) and [broker-adapter.md](../05-execution-layer/broker-adapter.md)) are surfaced in the strategist's guardrail state header's `Abandoned position actions` block — see [state-delivery.md](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header).

Each entry is a prompt to re-evaluate the original intent on current state and signals. A revived ADD/ADJUST/CLOSE produces a new per-position assessment (new `SA-n`); a revived CANCEL produces a new pending-order assessment (new `SA-ORD-n`). [Action decision logic](#action-decision-logic), [Add burden of proof](#add-burden-of-proof), and [Maintain / modify / cancel criteria](#maintain--modify--cancel-criteria) apply unchanged — prior PM approval does not reduce the rationale burden, and the new rationale must ground in current signals.

**CLOSE abandonments warrant priority attention.** An intended risk-reducing exit that did not execute is the most operationally consequential abandonment — the position is still exposed and the reasons for closing may have intensified. Prioritize re-evaluating abandoned CLOSEs and, when the thesis remains invalidated or risk-reducing urgency persists, produce a fresh `close` assessment.

If current signals no longer support the original intent, the entry lapses; the activity log captures the abandonment.

---

## Corporate-action-pending positions

Positions flagged `corporate_action_adjustment_needed` had their bracket cancelled when a corporate action fired on the underlying — split, reverse split, stock dividend, cash dividend, merger, acquisition, or spin-off (see [orders-and-brackets.md § Corporate action handling](../05-execution-layer/orders-and-brackets.md#corporate-action-handling)). Every flagged position must receive an `adjust-bracket` or `close` recommendation — every AlphaMind position requires a complete bracket.

**Default actions by corporate action type:**

- **Splits, reverse splits, stock dividends.** Thesis economically unchanged; share count and cost basis re-scaled by the OMS. Default to `adjust-bracket` with prior parameters re-scaled to the new price basis. Reverse splits warrant scrutiny — they often signal distress — assess thesis survival; if not, `close`.
- **Cash dividends.** Thesis unchanged. Default to `adjust-bracket` with prior parameters unchanged — absolute-price stops remain correct because the underlying moved by the dividend amount. Unusually large special distributions warrant a fresh thesis review; default to `close` when the distribution materially changes capital structure.
- **Cash mergers and acquisitions.** Position effectively liquidated at the deal price. Default to `close`.
- **Stock mergers.** Position converted to the acquirer's shares. The pre-merger thesis on the target does not apply to the acquirer. Default to `close` unless a fresh thesis on the acquirer is articulated (then `adjust-bracket` with new parameters).
- **Spin-offs.** The parent's thesis may survive. Default to `adjust-bracket` at the parent's post-spin price basis. The spun-off child is a new position without a thesis; default to `close` unless a fresh thesis on the child is proposed with brackets.

**Rationale requirements:** name the corporate action type and ratio/amount, state thesis disposition (unchanged / needs re-evaluation / invalidated), and for `adjust-bracket` provide updated parameters tied to current signals. Standard action rationale burden applies. Reverse-split and large-special-distribution cases especially require explicit thesis survival reasoning.

---

## Regime-transition remedy proposals

When the guardrail state header flags [regime-transition breaches](../06-risk-guardrails/regime-adaptation.md#position-handling-when-tightening-creates-breaches) or market-movement breaches, the strategist proposes specific remedies. Position-level thesis context — target proximity, conviction, catalyst timing — makes the strategist the right agent to decide *which* positions to reduce; the PM handles cross-constraint validation and final execution.

### Format: integrated into per-position assessments

Each flagged position receives a normal per-position assessment whose `recommended_action` is the remedy (`reduce`, `close`, or `hold`), with `remedy_flag` set to the breach identifier (e.g., `BREACH-1`) and `remedy_rationale` explaining why this action is right given the thesis state.

The PM evaluates every assessment through the same action-warrant / action-status / action-specific / portfolio-coherence rubric. The [portfolio-level observations](#per-position-assessment--structured-fields) section carries a `regime_transition_summary` listing addressed and uncured breaches with rationale.

### Remedy action selection

| Thesis state | Default remedy |
|--------------|----------------|
| `invalidated` | `close` (the thesis itself calls for close; the breach provides external confirmation) |
| `at-risk` with core component weakening | `close` or `reduce` (close when the breach overage exceeds what partial reduction can cure; reduce when a partial trim can cure the breach and the residual case is intact) |
| `on-track` or `partially-realized` with near-target proximity | `hold` with thesis-based rationale if the position is within hours of target resolution (cure the breach by resolution rather than by forced trim). Requires explicit naming of the target proximity and the expected resolution window |
| `on-track` with meaningful remaining horizon | `reduce` to compliance — trim the minimum quantity required to cure the breach while preserving the strongest-thesis positions. A full close on `on-track` to cure a sector breach is over-response |
| `stale` | `close` or `reduce` (the thesis has not confirmed within its window; the breach is a forcing function to act on the staleness) |

These are defaults, not mechanical rules. Per-position context may support a different choice — e.g., an `on-track` position with an unusually strong thesis and imminent catalyst may warrant `hold-with-rationale` even when a weaker-thesis sibling could be trimmed. The choice is explicit in the remedy rationale.

### Remedy reduction quantity

When the remedy is `reduce`, the quantity must be:

- **Sufficient to cure the breach** when combined with other remedies addressing the same breach. The aggregate should bring the breaching rule back to the 85% zone, not the 95% zone (one tick from re-breach).
- **Validated via the guardrail validation tool.** Call `validate_guardrail` with `action: "CLOSE"` (or partial close) and the proposed quantity to confirm cumulative effect.

### Interaction with per-position thesis status

A remedy trim is an action, not a status. Thesis status for a remedy-flagged position is assigned on the same signal criteria as every other position — the breach is a reason to act, not to reclassify. A `reduce` remedy on an `on-track` position is legitimate: the thesis is intact, the action is breach-driven, and the action rationale names the breach explicitly.

When a breach coincides with independent signals weakening the thesis, status moves independently and the remedy follows the new status's default. A breach on a position already moving to `at-risk` produces an `at-risk` assessment with a `reduce` or `close` remedy whose rationale spans both the thesis weakening and the breach overage.

### Guardrail validation requirements

Remedy `reduce` and `close` actions on breaching positions require validation:

1. **Confirm the remedy cures the breach** — cumulative-impact tracking confirms the aggregate effect on the breaching rule.
2. **Detect secondary breaches.** A close on a short providing directional balance could push net long exposure into its own breach; the tool surfaces this before reaching the PM. Name detected secondary breaches in the remedy rationale.

`hold-with-rationale` remedies do not require validation (no exposure change). The PM's cross-constraint impact summary (see [state-delivery.md](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header)) shows the aggregate effect across the full rule surface.

---

## Halt mode and defensive-posture behavior

The strategist operates in two modes set by the guardrail state header: `normal` and `defensive_posture`. Mode switches are driven upstream by the execution layer's halt-mode logic and the continuous monitor's emergency-invocation trigger (see [state-delivery.md](../06-risk-guardrails/state-delivery.md#halt-mode-header-modifications) and [breach-behavior.md](../06-risk-guardrails/breach-behavior.md#drawdown-halt-mode)). The strategist reads the mode; it does not decide when to switch.

### Defensive-posture mode

Triggered when daily drawdown halt or cumulative drawdown tier 3 is active. The upstream pipeline continues running, so the strategist still receives a full synthesizer brief. The change is behavioral.

**Behavioral shift:**

- **Emphasis pivots to risk reduction.** Actively look for deterioration signals, stop-tightening candidates, and positions whose risk/reward has weakened even modestly.
- **`add` is not permitted.** Action enum restricted to `hold`, `reduce`, `close`, `adjust-bracket` (schema-enforced via conditional on the mode field). A signal-grounded add warranted under `normal` is recorded in portfolio-level observations as deferred until defensive posture lifts.
- **Hold thresholds raise.** A `hold` on `on-track` may warrant complementary bracket tightening, because the environment triggering the halt signals weakened asymmetries. The adjustment rationale names the halt as the new signal.
- **Pending order review emphasis.** Pending entry orders are default-cancel candidates — pre-halt commitments the new market context may have rendered obsolete. Re-assess every pending entry and justify any `maintain` against the halt condition.
- **Portfolio-level observations emphasize capital preservation.** Commentary shifts to "where is the book most exposed to further drawdown, and what is the orderly-reduction priority if the PM needs to cut further."

### Output mode flag and schema variant

The output document's top-level `mode` field is set to `defensive_posture`. The schema enforces:

- `recommended_action` restricted to `hold | reduce | close | adjust-bracket`
- Pending order assessments use the same structure as `normal`
- Portfolio-level observations include a required `defensive_posture_summary` — an explicit orderly-reduction priority list

The schema is a strict superset across modes — `mode` controls which subset of the action enum and required fields apply.

### Emergency invocation handling

Emergency invocations (continuous-monitor-triggered; see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger)) are independent of halt mode — they indicate a sudden regime shift, multi-rule breach, drawdown velocity, or margin-call event warranting immediate reasoning. Emergency invocations can co-occur with halt mode or `normal` mode.

When the guardrail header includes the `** EMERGENCY INVOCATION **` flag, the strategist:

- **Prioritizes breach resolution.** Address flagged regime-transition or market-movement breaches first; place the corresponding per-position assessments first in the output.
- **Re-examines adjacent positions.** The triggering event typically bears on more than the breaching position. Assess whether adjacent positions — same sector, catalyst, or directional exposure — warrant status changes and defensive adjustments.
- **Raises the bar for `hold` and defers discretionary recommendations.** Non-urgent adjustments wait for the next scheduled invocation.

If an emergency invocation fires while halt mode is active, both behaviors apply.

---

## Presentation order and token budget

### Presentation order

Per-position assessments are ordered by status severity, then by action urgency, then by portfolio weight:

1. **`invalidated` first.** These positions should be closed this invocation; surfacing them first ensures the PM's first decisions are the most time-sensitive.
2. **`at-risk` next.** Positions whose thesis has weakened warrant early PM attention and frequently require remedy actions that interact with other positions in the book.
3. **`partially-realized`.** These decisions — full close, partial close, hold through the remaining leg — benefit from being read before defensive-posture decisions on the balance of the book.
4. **`stale`.** The hold-vs-close decisions here are the ones most susceptible to sunk-cost anti-patterns; placing them in a consistent position in the output helps the PM apply consistent review discipline.
5. **`on-track` last.** Highest-quality positions with the fewest required decisions; the PM can scan these as confirmations rather than decisions.

Within a status tier, order by action urgency — `close` before `reduce` before `adjust-bracket` before `hold` before `add` — then by portfolio weight descending (larger positions before smaller). Remedy-flagged assessments (any status with `remedy_flag` populated) are ordered first within their status tier regardless of action type, since their decisions interact across the flagged breach.

Pending order assessments follow the per-position assessments, ordered by recommended action (`cancel` before `modify` before `maintain`) and then by order age descending (oldest orders first, since these are the most likely neglect cases).

The portfolio-level observations section follows last.

### Token budget

The strategist's input bundle is larger than the analyst's because it carries full thesis component detail for every open position. Target ranges scale with portfolio complexity.

| Portfolio profile | Positions in typical range | Target context budget | Target output budget |
|-------------------|---------------------------|----------------------|---------------------|
| Primary ($1,500, no options/shorts) | 1–4 positions | 400–700 tokens | 300–600 tokens |
| Full-system ($100K) | 6–15 positions | 1,500–2,500 tokens | 1,200–2,500 tokens |

**Notes:**

- Context budget covers the guardrail state header (constraint portion), pending orders, and active thesis records at component level from portfolio state. It excludes the shared synthesizer brief.
- Output budget covers structured fields, per-position narratives, pending-order assessments, and portfolio-level observations. Per-position narrative length scales with thesis complexity, not position count.
- These are sizing targets for the state-delivery layer, not behavioral targets. Quality over brevity applies. Drift above the upper bound triggers prompt-iteration review, not mid-invocation truncation.

---

## Pre-submission guardrail validation

The strategist validates proposals that would change portfolio exposure — **add** recommendations and any **close** or **reduce** recommendations that interact with guardrail constraints (e.g., closing a short that would increase net long exposure beyond directional limits).

Same guardrail validation tool as the analyst (see [analyst.md](analyst.md) for the full specification). Workflow: propose, validate, revise if needed. The tool computes delta-adjusted exposure for options, tracks cumulative impact across multiple recommendations, and returns per-rule pass/fail with headroom.

**Hold and adjust-bracket** do not require validation (no exposure change). **Close and reduce** are generally guardrail-relieving, but be aware of cases where closing one position pushes another constraint over the limit (e.g., closing a short providing directional balance). The headroom data in the guardrail state header makes these interactions visible; the validation tool confirms.

**Interaction with analyst proposals:** The strategist and analyst run in parallel with no visibility into each other's proposals. The strategist validates against current portfolio state, not against a hypothetical state including the analyst's proposals. The combined set might collectively breach a limit even though each set is individually compliant. The PM evaluates both sets holistically and can reject or downsize to maintain compliance. See [portfolio-manager.md](portfolio-manager.md).

---

## Source brief retrieval

Same retrieval tool as the analyst — pulls source analysis brief sections by reference ID. Useful when:

- Evaluating whether new information contradicts a specific key assumption in a thesis component
- Investigating whether an entry-rationale signal has strengthened, weakened, or reversed
- Assessing cross-position dynamics via correlation or regime data from `[CR-*]` references

---

## Relationship to other agents

| Agent | Relationship |
|-------|-------------|
| [Analyst](analyst.md) | Runs in parallel — no direct interaction. Both feed the [proposal pre-processor](proposal-pre-processor.md), which detects same-name conflicts (e.g., analyst proposes new NVDA position while strategist closes existing NVDA position) and annotates them for the PM |
| [Proposal pre-processor](proposal-pre-processor.md) | Deterministic processing step that reads structured fields from both outputs, computes cross-proposal annotations (conflicts, cumulative exposure, sector impact), and delivers annotated proposals to the PM |
| [Portfolio manager](portfolio-manager.md) | Ultimate consumer. Receives strategist position assessments alongside analyst proposals, with pre-processor annotations, and makes holistic portfolio decisions |

---

## Dependencies

- [Synthesizer](../03-analysis-layer/synthesizer.md) — primary market context input
- [Portfolio state (raw)](../01-data-layer/internal/portfolio-state.md) — position inventory, P/L, thesis registry, activity log
- [Thesis model](../05-execution-layer/thesis-model.md) — defines thesis structure and status classifications
- [OMS commands](../05-execution-layer/oms-commands.md) — the action parameters in the strategist's output must map to valid CLOSE, ADJUST, ADD, and CANCEL command parameters
- [Risk guardrails / state delivery](../06-risk-guardrails/state-delivery.md) — guardrail headroom informs action recommendations
