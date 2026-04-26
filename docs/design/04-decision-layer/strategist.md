# Strategist

Operates in a fresh context window. **Mandate:** evaluate every open position against current market conditions and recommend portfolio actions. The strategist connects new information from the synthesizer to existing thesis narratives, classifies thesis health status, identifies cross-position dynamics, and recommends specific actions with rationale. It is the sole agent responsible for thesis status assessment — determining whether each thesis is on-track, at-risk, stale, or invalidated based on primary data.

Runs in parallel with the [analyst](analyst.md). Both agents receive the synthesizer output independently and produce recommendations concurrently. Their outputs converge at the [portfolio manager](portfolio-manager.md), which evaluates both holistically.

---

## Inputs

The strategist's input bundle is delivered at invocation start. Source documents in parentheses are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Synthesizer brief | [synthesizer.md](../03-analysis-layer/synthesizer.md) | Prose synthesis with embedded `[SA-*]`, `[QR-*]`, `[AR-*]`, `[CR-*]` references — the new information against which existing theses are evaluated |
| Full thesis records (component level) | [portfolio-state.md §3a](../01-data-layer/internal/portfolio-state.md), [thesis-model.md](../05-execution-layer/thesis-model.md) | Every active thesis at component level (entry rationale, target rationale, invalidation rationale per leg, key assumptions, `prior_status`). Strategist is the sole consumer that receives components inline because thesis-status classification is per-component |
| Position details | [portfolio-state.md §1a, §2a](../01-data-layer/internal/portfolio-state.md), [position-model.md](../05-execution-layer/position-model.md) | Per-position P/L trajectory, current market value, position age, distance to target, distance to stop, risk/reward ratio at current price |
| Activity log | [portfolio-state.md §5](../01-data-layer/internal/portfolio-state.md) | Intra-invocation changelog (5a), PM decision log sliding window (5b), and per-position modification trail (5c) — provides continuity with prior invocation decisions and surfaces engine-originated actions |
| Pending orders | [portfolio-state.md §4b](../01-data-layer/internal/portfolio-state.md) | Unfilled orders with age, fill-probability context, and the thesis that justified them. Drives [Pending order review](#pending-order-review) |
| Strategist guardrail state header | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) | Formatted text block at the top of the prompt: regime label, sector/directional headroom, position-level constraint proximity, sector exposure breakdown, drawdown state, regime-transition breaches with `BREACH-N` IDs, hard blocks |
| Abandoned openings block | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (abandoned-openings section) | Prior-invocation OPEN abandonments — surfaced as portfolio-awareness context; analyst owns re-evaluation |
| Abandoned position actions block | [state-delivery.md — Strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (abandoned-position-actions section) | Prior-invocation ADD / ADJUST / CLOSE / CANCEL abandonments — surfaced for re-evaluation on current grounds. See [Abandoned position actions from prior invocation](#abandoned-position-actions-from-prior-invocation) |

Tools available during reasoning:

| Tool | Source | Use |
|---|---|---|
| Source-brief retrieval | [decision-layer overview — Information flow](README.md#information-flow) | Pull a section of an analysis brief by reference ID from the synthesizer's retrieval store. See [Source brief retrieval](#source-brief-retrieval) |
| Guardrail validation tool | [state-delivery.md — Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) | Deterministic pre-submission check on exposure-changing proposals. See [Pre-submission guardrail validation](#pre-submission-guardrail-validation) |

The volatility regime label is delivered as the `Regime:` line in the guardrail state header (not as a separate broadcast).

**Token budget:** the input bundle scales with portfolio complexity — target ranges are 400–700 tokens for the primary portfolio (1–4 positions) and 1,500–2,500 tokens for the full-system portfolio (6–15 positions), not counting the shared synthesizer brief. See [Presentation order and token budget](#presentation-order-and-token-budget) for the output budget and the rationale.

---

## Output

The strategist's invocation output is a single document conforming to the [strategist output schema](strategist-output-schema.md) (formal JSON Schema, Draft 2020-12). The schema is the authoritative contract; the field lists in [Output structure](#output-structure) below are the readable reference. Each per-position assessment is a structured record with **structured fields** (machine-parseable, consumed by the [proposal pre-processor](proposal-pre-processor.md)) and **narrative fields** (free-text reasoning, consumed by the [portfolio manager](portfolio-manager.md)). The output also carries pending-order assessments and a portfolio-level observations section.

The output mode is `normal` under standard conditions and `defensive_posture` under halt mode — see [Halt mode and defensive-posture behavior](#halt-mode-and-defensive-posture-behavior) for the behavioral and schema-variant differences.

---

## Responsibilities

- **Thesis status classification:** For each open position, assess thesis health by cross-referencing the thesis narrative and key assumptions against the synthesizer's current market context. Classify each thesis as on-track, partially-realized, at-risk, stale, or invalidated (see [thesis-model.md](../05-execution-layer/thesis-model.md) for status definitions). This is signal-level reasoning — e.g., "`[QR-7]` directly contradicts the supply chain signal that supported this entry, moving thesis from on-track to at-risk."

- **Action recommendation:** For each position, recommend a specific action: hold, reduce, close, adjust-bracket, or add. For non-hold recommendations, include concrete action parameters (quantity, order type, bracket changes, close rationale type).

- **Cross-position reasoning:** Identify portfolio-level dynamics that no single-position assessment would catch: correlation shifts between held positions, thesis dependency overlaps (multiple positions relying on the same catalyst), sector concentration trends, and opportunities to rebalance.

- **Pending order review:** Evaluate unfilled orders from prior invocations — should they be maintained, modified, or cancelled given current conditions?

---

## Output structure

The strategist produces a position assessment for every open position plus any pending orders. The portfolio manager receives the strategist's output as a complete book review alongside the analyst's new trade proposals, with [proposal pre-processor](proposal-pre-processor.md) annotations layered on top.

**Per-position assessment — structured fields:**

- **Assessment ID:** unique identifier within the invocation (e.g., `SA-1`, `SA-2`)
- **Position ID and thesis ID:** which position this assessment covers
- **Underlying:** the root ticker for this position — used by the pre-processor for same-name conflict detection against analyst proposals
- **Sector:** which sector this position belongs to
- **Thesis status:** the strategist's status classification (on-track, partially-realized, at-risk, stale, invalidated) — the authoritative thesis health assessment for this invocation. Signal criteria per status are defined in [Thesis status classification methodology](#thesis-status-classification-methodology)
- **Prior status:** the thesis status from the previous invocation (read from the thesis record), enabling the PM to see status transitions (e.g., on-track → at-risk)
- **Recommended action:** hold, reduce, close, adjust-bracket, or add. Action selection criteria are defined in [Action decision logic](#action-decision-logic)
- **Action parameters** (required for non-hold recommendations):
  - For close: quantity (partial or full), order type (market or limit with price), close rationale type (thesis-invalidated, target-reached, conviction-reduced, risk-management)
  - For reduce: quantity, order type
  - For adjust-bracket: specific changes (new stop level, new target, new time horizon, new/revised event invalidation)
  - For add: additional quantity, entry order parameters, bracket adjustment if needed
- **Exposure impact** (for non-hold recommendations): the estimated change in delta-adjusted exposure for this position's sector, and the net directional impact. For add recommendations: populated by the guardrail validation tool at pre-submission check time. For close/reduce: computed from current position data
- **Guardrail validation result** (for add recommendations only, and for reduce/close recommendations whose exposure impact interacts with a flagged constraint): pass/fail summary from the pre-submission check
- **Remedy flag** (optional): identifier of a regime-transition or market-movement breach flagged in the strategist's guardrail state header that this assessment addresses (e.g., `BREACH-1`). See [Regime-transition remedy proposals](#regime-transition-remedy-proposals)

**Per-position assessment — narrative fields:**

- **Status rationale:** signal-level reasoning with source references (e.g., `[QR-7]`, `[SA-TECH-3]`) explaining the thesis status classification — what signals support or undermine the thesis. When status has changed from the prior invocation, the rationale must explain what drove the transition
- **Action rationale:** signal-level reasoning explaining what new information drives the recommended action. For close with thesis-invalidated: the specific invalidation reason
- **Reduce rationale** (for reduce only): why partial rather than full close, and how the reduction quantity maps to the portion of the thesis that weakened or to the breach overage (for remedy reductions)
- **Add conviction justification** (for add only): what strengthening signal absent at entry warrants increasing the position. A validating signal ("the thesis is playing out") is a hold reason, not an add reason
- **Adjustment rationale** (for adjust-bracket only): why the bracket parameters should change — what new information makes the original level wrong
- **Remedy rationale** (required when remedy flag is present): why this action (trim-to-compliance / close / hold-with-rationale) is the right response to the flagged breach, given the thesis state
- **Cross-position observations** (optional): portfolio-level dynamics relevant to this position — e.g., "this position's correlation with POS-AMD-001 has increased from 0.3 to 0.7 this week, creating unintended concentration"

**Pending order assessment — structured fields:** See [Pending order review](#pending-order-review).

**Portfolio-level observations** (one section, after all position assessments — narrative only):

- Aggregate thesis health: distribution of positions across status categories; direction of movement since the prior invocation
- Sector balance shifts: whether the recommended actions would alter the portfolio's sector exposure profile
- Thesis dependency warnings: multiple positions that share catalysts or assumptions — a single catalyst firing against consensus would invalidate several positions at once
- Capital allocation observations: whether the current book is capital-efficient given the aggregate thesis-quality distribution
- Regime-transition summary (when applicable): which flagged breaches are addressed by the per-position remedies above, and any that remain uncured with rationale

---

## Thesis status classification methodology

Every per-position assessment sets `thesis_status` to one of five values. The scale is defined by signal characteristics — the presence or absence of specific current-invocation signals bearing on the thesis — not by P/L. A position can be deeply profitable and `at-risk` (the move already played out and new information undermines the residual case) or deeply underwater and `on-track` (the catalyst hasn't fired yet and nothing has contradicted the thesis).

| Status | Signal criteria |
|--------|----------------|
| **on-track** | Key assumptions from the thesis record remain consistent with current signals. The named catalyst has not yet fired, or the position is moving toward the target for reasons the thesis predicted. No new invocation signal materially undermines any thesis component. |
| **partially-realized** | The named catalyst has partially fired and some thesis components are validated by current signals, but the target has not been reached. The residual case is intact but narrowed — the remaining potential is smaller than at entry, and the evidence supporting the remaining leg should be re-examined. |
| **at-risk** | A current-invocation signal weakens the thesis without invalidating it. Examples: a supporting signal cited in the entry rationale has faded or reversed; a correlated position has moved against the pattern the thesis depended on; a new finding creates ambiguity on a key assumption; prediction-market or flow data has repriced away from the thesis direction. The thesis still has a coherent path, but the margin for further adverse news has narrowed. |
| **stale** | Position age has exceeded `time_expectation_hours` without the catalyst firing or the target being reached, and no current-invocation signal moves the thesis into any of the other four categories. Staleness is the default when nothing has happened — neither confirmation nor contradiction — past the expected resolution window. |
| **invalidated** | A specific falsifiable claim in the thesis has been proven wrong by a current-invocation signal, or an event-based invalidation leg's condition has been met. Invalidation requires a named signal or event, not an inference. Position-level max loss triggers are handled by the engine, not by this status — invalidation is a thesis-level judgment. |

### Borderline cases

When the evidence lies between two categories, resolve as follows:

- **on-track vs. at-risk.** Default to `at-risk` when a plausible reading of a current signal weakens a cited entry component. The rationale should name the signal and the component. Under-classifying to `on-track` when a supporting signal has faded is the most corrosive failure mode because it suppresses the information the PM needs to manage the position.
- **at-risk vs. invalidated.** Require a named falsifier for `invalidated` — a specific event leg condition met, a specific key assumption contradicted by a specific cited signal. Generic readings ("the tape looks wrong," "momentum has stalled") stay at `at-risk`. Invalidation is a commitment to close; its signal bar is correspondingly higher.
- **at-risk vs. partially-realized.** `partially-realized` requires affirmative evidence that part of the catalyst has fired and part of the thesis has been confirmed. A position that is up in P/L but whose catalyst has not fired is still `on-track` or `at-risk`, not `partially-realized` — P/L is not a classification input.
- **stale vs. at-risk.** If the position has aged past its time expectation and a current signal weakens the thesis, classify `at-risk` (the stronger signal dominates). `stale` is reserved for the no-news case — the catalyst simply didn't fire and nothing else has happened.
- **stale vs. invalidated.** Age alone does not invalidate. An overdue catalyst that could still fire stays `stale`; an overdue catalyst publicly confirmed not to fire (e.g., an earnings release that contradicted the thesis, a regulatory event that resolved against the thesis) becomes `invalidated`.

### Prior status and cross-invocation continuity

Each thesis record carries a `prior_status` field — the classification from the immediately preceding strategist invocation. The strategist reads it and, in `status_rationale`, must explain any transition by naming what changed.

- **Transition requires a cited signal.** `on-track → at-risk` requires a specific finding that weakened the thesis in this invocation. `at-risk → on-track` requires a specific finding that restored it — either the weakening signal was resolved (e.g., the contradicting data was revised), or a new supporting signal has appeared. Silent transitions (different classification with no cited cause) are a status-warrant failure the PM will reject.
- **Repeated classifications should still cite fresh signals.** Three consecutive `on-track` classifications on the same position is legitimate only if each is anchored to current-invocation evidence, not to the entry thesis. Rehashing the entry rationale under a repeated `on-track` classification is the single-invocation symptom of the `sunk_cost_persistence` anti-pattern the PM watches for.
- **Repeated `at-risk` or `stale` without transition is itself a signal.** Across multiple invocations without either escalation to `invalidated` or de-escalation to `on-track`, the position is drifting — the strategist should note the aggregation in `cross_position_observations` or `portfolio_level_observations` and consider whether an action other than continued hold is warranted.

### Calibration target

Analogous to the [analyst's conviction scale calibration](analyst.md#conviction-scale): a well-calibrated strategist should exhibit distinct forward-outcome distributions across the five statuses when its classifications are tracked over time.

- Positions classified `invalidated` should close into realized losses (the close follows the classification). A high share of `invalidated` theses that subsequently recover in price suggests the classification is being applied too loosely — the signal bar needs raising.
- Positions classified `at-risk` should exhibit higher forward adverse-outcome rates than `on-track` positions over the thesis's remaining time horizon. If `at-risk` and `on-track` positions have indistinguishable forward outcomes, the classifications are not tracking signal and one of them is being applied reflexively.
- Positions classified `on-track` should correlate with continued thesis validation — the catalyst firing, the target being approached. A high share of `on-track` classifications followed by P/L decline without a preceding `at-risk` transition indicates under-classification: signals that should have moved the thesis to `at-risk` were suppressed.
- `stale` classifications should be followed either by the catalyst firing (transitioning to `partially-realized` or `on-track`), by invalidation, or by a PM-approved close. A long dwell time in `stale` without transition is the strategist failing to re-examine whether the thesis has quietly moved into `at-risk` or `invalidated`.

These distributions are tracked across invocations by the feedback loop (see [thesis quality trends](../01-data-layer/internal/portfolio-state.md) category 6). The strategist is not expected to enforce them per-invocation — that would reintroduce the numeric-anchor failure mode the analyst's scale avoids — but the feedback loop surfaces drift as a prompt-iteration signal.

---

## Action decision logic

For each position, the strategist chooses one `recommended_action` from `hold`, `reduce`, `close`, `adjust-bracket`, or `add`. The status classification constrains the action space; the action then maps to specific structured parameters and a narrative rationale.

### Per-action signal criteria

| Action | Signal criteria |
|--------|----------------|
| **hold** | Status is `on-track` and no new information warrants a parameter change. Holds on `partially-realized`, `at-risk`, and `stale` are permitted but require explicit rationale (see below). Holds on `invalidated` are not permitted. |
| **reduce** | A specific thesis component has weakened without the whole thesis being invalidated (maps to `partially-realized` or component-level `at-risk`); OR the position is a regime-transition / market-movement remedy trim; OR conviction has decayed to a level where the position is over-sized relative to remaining signal. |
| **close** | Status is `invalidated` — an event leg fired or a falsifiable claim was contradicted; OR the target has been reached (resolution `target_reached`); OR conviction has decayed to the point where the full residual case no longer warrants any position; OR portfolio-level risk demands a full exit (resolution `risk_management`). |
| **adjust-bracket** | A current signal changes the appropriate invalidation level (tighten a stop on confirmation, revise an event leg as new catalysts are named) or target level (extend on strengthening, pull in on partial realization). Bracket-widening and time-extension have an elevated burden of proof — see [LLM failure mode avoidance](#llm-failure-mode-avoidance). |
| **add** | A strengthening signal absent at entry has appeared — new convergent evidence that was not part of the original thesis. P/L improvement is not a strengthening signal. A validating signal ("the thesis is playing out as expected") is a hold reason, not an add reason. |

### Full-vs.-partial reasoning for close and reduce

The strategist must justify the quantity chosen, not just the action type.

- **Full close.** Required when `invalidated` is assigned, because a position sized for the original thesis has no residual case. Also the default when the target is reached, because partial target-taking is a discretionary risk-management choice; if the strategist recommends a partial close on `target_reached`, the reduce rationale must name a specific reason residual exposure is warranted (e.g., the target level may extend, the remaining position has an independent secondary thesis).
- **Partial close or reduce.** Required when a specific thesis component has weakened but others remain intact. The reduction quantity should map to the portion of the thesis that failed — a non-core key-assumption failure warrants a smaller reduction than a core-mechanism failure. A reduce whose quantity does not map to an identifiable portion of the thesis is a generic "take some off the table" and will fail the PM's action-specific justification criterion.
- **Full close on `at-risk`.** Permitted when the weakening is severe enough that the residual case does not justify any remaining exposure even though no specific falsifier has fired. The rationale must explain why partial is not sufficient — usually because the weakening bears on the core mechanism, not a supporting leg.

### Add burden of proof

Adds are the highest-bar action because they increase exposure on an existing thesis, and the failure mode is to treat continued validation as a reason to double down.

- **Strengthening signal required.** The add rationale must name a current-invocation signal that was not part of the entry thesis — a new convergent piece of evidence, a fresh catalyst that aligns with the thesis, an institutional flow signal that was absent at entry.
- **Validating signals are not strengthening signals.** "The catalyst is firing as expected," "price is approaching the target," "prediction markets have moved in our favor" are validations of the original thesis, not new information. A thesis validating itself is a hold reason. If the original thesis is playing out as expected, the original sizing was correct — adding implies the original sizing was wrong, which requires a reason the entry underestimated the signal.
- **Adds on non-`on-track` status.** Adds on `partially-realized` require an explicit argument for why the remaining catalyst warrants more exposure even though the original case has already partly resolved; this will usually fail the PM's action-status alignment criterion. Adds on `at-risk`, `stale`, or `invalidated` are not valid action pairings — the status itself rules out increasing exposure.

### Default stance on bracket-widening and time-leg extension

Both are mechanically identical to `adjust-bracket` but represent the two specific movements most associated with the rationalized-continuation failure pattern: loosening the stop so the position has more room to move against the thesis, and extending the time horizon so the position can stay open past its original resolution window.

- **Default read: rationalization.** The strategist should treat both moves as rationalization unless the rationale names a concretely new signal that makes the original level wrong. The burden of proof sits with the strategist — absent a named new signal, the original invalidation level was the right one.
- **Narrow exception for time-leg extension.** A publicly rescheduled catalyst (earnings delayed to a specific new date, trial date moved) justifies extending to the new timestamp. A vaguely-timed catalyst that "might fire soon" does not qualify. A post-catalyst reaction window — a catalyst has fired but market impact is still unfolding — justifies a modest extension tied concretely to the reaction window, not to "the market needs time to digest."
- **Narrow exception for stop-widening.** A revised technical level supported by a current cited signal (volatility expanded beyond the original stop's rationale, a specific support/resistance revision) may justify widening. A generic "the stop was too tight given how the market is moving" does not qualify.

In all cases, the adjustment rationale must explicitly pair the old level with the new level and cite the signal. Terse or generic adjustment rationales fail the PM's action-specific justification criterion and are the primary engine for the rationalized-continuation anti-pattern.

### Hold rationale requirements by status

A `hold` is the most frequently chosen action; it is also the action most susceptible to the sunk-cost persistence failure mode. Hold rationale requirements scale with status:

- **`on-track` hold.** Default action. Rationale cites current signals that confirm the thesis is playing out as predicted. A hold with no cited current signal — rehashing the entry rationale — will fail the PM's status classification warrant.
- **`partially-realized` hold.** Requires a reason the residual case still justifies full-size exposure. If the residual case is smaller than the original, a `reduce` is usually the more honest action.
- **`at-risk` hold.** Requires a reason the weakening signal does not warrant position-size reduction, or why the position's asymmetry still favors the residual case. When approved, `at-risk` holds should typically be paired with a complementary bracket tightening (see [portfolio-manager.md existing-position guidance](portfolio-manager.md#aging-theses)). Un-guarded holds on `at-risk` positions fail the PM's action-status alignment.
- **`stale` hold.** Requires a specific reason the horizon should be extended — usually a publicly rescheduled catalyst or a post-catalyst reaction window. "Catalyst expected soon" is not a rationale. The PM default-rejects `stale` holds without a rescheduled-catalyst rationale.
- **`invalidated` hold.** Not permitted. If the strategist wants to argue the thesis is not actually invalidated, the status should be `at-risk`, not `invalidated`. A `hold` paired with `invalidated` is self-contradictory and will fail PM action-status alignment.

---

## LLM failure mode avoidance

The [portfolio manager](portfolio-manager.md#anti-patterns-the-pm-is-watching-for) polices a set of anti-patterns that appear across proposals from both agents. The strategist is the agent whose output surfaces these patterns most frequently because it reviews every open position on every invocation — the sheer cadence of hold/add/adjust decisions is where drift accumulates. The strategist's job is to catch the single-invocation manifestation of each pattern before the PM has to reject it.

The patterns below are the mirror image of the PM's rejection criteria. Each is named in the canonical form the PM rationale narratives use, so the feedback loop can aggregate on the same strings whether the pattern was caught at the strategist or rejected at the PM.

### Sunk-cost persistence

*Pattern:* continuing to hold a position whose status has been `at-risk` or `stale` across multiple prior invocations, without any new supporting signal in the current-invocation rationale. The classification drifts because the strategist keeps extending the benefit of the doubt rather than escalating to `invalidated` or recommending a close.

*Mirror-image discipline.* When `prior_status` shows two or more consecutive non-`on-track` classifications without transition to `on-track` or `invalidated`, the strategist should treat the continued hold as the action that requires justification, not the close. The status rationale should explicitly account for the aggregation — either cite a new supporting signal that explains continued patience, or escalate the classification and recommend the corresponding action. A silent third `at-risk` in a row is the pattern.

### Rationalized continuation

*Pattern:* an `adjust-bracket` that widens a stop or extends a time horizon, or an `add` to a losing position, framed as responding to new information but whose rationale restates the entry thesis. The structural shape is an action that loosens the invalidation envelope or increases exposure without citing a current-invocation signal that was not in the original thesis.

*Mirror-image discipline.* The strategist should treat bracket-widening, time-leg extension, and adds with a default-reject stance on itself — before proposing one, require a named current-invocation signal that is genuinely new. If the rationale reduces to "the thesis still applies, the position just needs more room / more time / more size," the correct action is `hold` (no change) or `reduce` (the position was wrongly sized), not the loosening or addition. See [Action decision logic — Default stance on bracket-widening and time-leg extension](#default-stance-on-bracket-widening-and-time-leg-extension) and [Add burden of proof](#add-burden-of-proof).

### Thesis-contradiction suppression

*Pattern:* a status rationale that omits a contradiction or uncertainty the synthesizer flagged bearing on the position's thesis. The strategist reads past the contradiction and produces a status classification that would be different if the contradiction had been addressed.

*Mirror-image discipline.* When the synthesizer flags a contradiction or uncertainty bearing on a cited entry signal for the position, the status rationale must address it — either explain why the contradiction does not overturn the thesis (and downgrade status to reflect the residual uncertainty), or acknowledge the contradiction as moving the thesis to `at-risk` or `invalidated`. Silent omission is the pattern. The source brief retrieval tool is available to verify that the characterization matches the underlying brief; the strategist should use it when the contradiction's impact on the thesis is not clear from the synthesizer's surface-level language.

### Engine-originated closure as a signal

*Pattern:* an engine-originated envelope appears in the activity log (e.g., a position-level max-loss force-close on a semis position, or a sector-trim for concentration breach), and the strategist treats adjacent positions in the same sector or sharing the same catalyst as if nothing had happened. The engine action is a real-time signal about the market regime that the strategist's per-position assessments should reflect.

*Mirror-image discipline.* When the activity log surfaces a recent engine-originated envelope, the strategist should explicitly assess whether the closed position's thesis shared components with any currently-held position. The `cross_position_observations` field is the place to name the connection — e.g., "POS-AMD-002's thesis shares the hyperscaler-capex leg that drove POS-AVGO-001's engine-forced close at 10:47; downgrading POS-AMD-002 from `on-track` to `at-risk` to reflect the shared exposure." Silent omission — producing `on-track` on all adjacent positions — is the pattern and will fail the PM's portfolio-coherence criterion.

### Generic-rationale avoidance

*Pattern:* status or action rationales that could be cut-and-pasted across any position — "thesis continues to play out," "still watching the catalyst," "maintaining position for now," "conditions remain favorable." These are not rationales; they are the absence of one. The failure mode is particularly acute for repeated `hold` recommendations on long-held positions, where the strategist's cognitive load incentivizes boilerplate.

*Mirror-image discipline.* Every status rationale must cite at least one current-invocation source reference (`[SA-TECH-N]`, `[QR-N]`, `[AR-N]`, `[CR-N]`) bearing on the thesis or a specific component of the current portfolio state (position age, P/L trajectory, distance to stop). Every action rationale must either cite a new signal driving the action (for non-`hold` actions) or, for `hold`, cite the absence of any status-changing signal — named specifically, not implied. If a rationale could be written without having read this invocation's synthesizer brief, it is a generic rationale.

---

## Pending order review

The strategist evaluates every unfilled order (entry limits, take-profit legs, stop-limit legs) carried over from prior invocations. Each pending order gets a structured assessment in the strategist's output — the pre-processor passes these through to the PM, which makes the final maintain/modify/cancel decision.

### Structured fields

- **Pending order assessment ID:** unique within the invocation (e.g., `SA-ORD-1`)
- **Order ID:** the specific order being assessed
- **Position ID:** the position this order belongs to (entry orders reference a pending position; bracket legs reference an active position)
- **Order type:** one of `entry_limit`, `entry_stop_limit`, `bracket_target`, `bracket_price_stop`, `bracket_time_stop`, `bracket_event_stop`
- **Order age hours:** hours elapsed since the order was placed
- **Current distance:** for price-anchored orders, distance between current underlying price and the trigger/limit level, expressed as a percentage
- **Fill probability assessment:** one of `likely_soon`, `plausible`, `unlikely`, reflecting the strategist's qualitative read of how the underlying is moving relative to the order level given current signals
- **Recommended action:** `maintain`, `modify`, or `cancel`
- **Modification parameters** (required for `modify`): which fields change and to what — new limit price, new trigger price, new deadline, new order type
- **Linked position assessment** (optional): the `SA-N` assessment ID for the parent position, when the order's disposition should be read alongside the position's status

### Narrative fields

- **Drift rationale:** how conditions have shifted between when the order was placed and the current invocation — what signals have appeared that bear on the order's fill probability or its thesis-justification
- **Action rationale:** why maintain / modify / cancel given the drift. For `cancel`: whether the parent thesis is intact with a different execution path, or whether the order's cancellation reflects a thesis status change that is also captured in the per-position assessment

### Treatment of aged orders and fill-probability drift

The principal failure mode with pending orders is neglect — an order placed a day ago on conditions that no longer apply gets passively extended because no one explicitly re-examined it. The strategist's review is the defense.

- **Aged orders without a current fill-probability assessment are the pattern to avoid.** Every pending order in the strategist's context carries `order_age_hours`. When the age meaningfully exceeds the parent thesis's time-scale (entry orders aged beyond the entry window's implied decay, bracket legs where the underlying has drifted well away from the trigger level without triggering), the default read is `cancel` unless the current invocation's signals specifically support maintaining.
- **Fill-probability drift.** A `likely_soon` at placement becomes `unlikely` when the underlying has moved — the order is now either stale at its original price (the thesis's entry was priced for a different level) or the thesis's urgency has changed. The strategist should re-assess fill probability against current conditions, not against conditions at placement.
- **Bracket legs on positions whose status has changed.** If the parent position's status has moved to `at-risk` or `invalidated`, the bracket legs' rationale at the time of entry may no longer apply. The strategist's assessment of bracket legs should be consistent with the per-position assessment — a bracket target on an `invalidated` position should be `cancel` (as part of the full close), not `maintain`.

### Maintain / modify / cancel criteria

- **maintain.** Conditions have not materially changed since placement; fill probability remains appropriate; parent thesis is intact at the order's implied level. The rationale must cite what has not changed — silent maintains on aged orders are the pattern to avoid.
- **modify.** The thesis is intact but the order's parameters no longer reflect current conditions — the entry limit needs to move with the underlying, the time leg needs to extend for a publicly rescheduled catalyst, the target needs to pull in because partial realization has changed the remaining upside. Modifications have the same rationale burden as `adjust-bracket`: the new parameter must be paired with the old parameter and a cited signal (see [Default stance on bracket-widening and time-leg extension](#default-stance-on-bracket-widening-and-time-leg-extension)).
- **cancel.** The order is no longer thesis-justified: the entry opportunity has passed (the entry-window decay has fully played out at an unfilled level), the parent thesis has invalidated, the parent position's classification has moved to a state where the order's rationale no longer applies, or fill probability has decayed to the point where capital reservation is not warranted. Cancellations are first-class actions, not defaults — the rationale should name the specific reason, not "the order is no longer relevant."

The PM applies its existing-position modification authority to the strategist's pending-order assessments (see [portfolio-manager.md scope](portfolio-manager.md#scope-of-pm-authority-for-existing-position-envelopes)): within-action parameter modifications and risk-reducing complementary commands (e.g., adding a cancel for a pending entry whose parent position has been recommended for close) are permitted; action replacement (maintain → cancel, cancel → modify) requires rejection, which carries the disagreement to the next invocation.

---

## Abandoned position actions from prior invocation

ADD, ADJUST, CLOSE, and CANCEL commands approved by the PM in the prior invocation but abandoned at broker submission (per the Phase 2 write-path policy in [state-persistence.md](../05-execution-layer/state-persistence.md) and [broker-adapter.md](../05-execution-layer/broker-adapter.md)) are surfaced in the strategist's guardrail state header's `Abandoned position actions` block — see [state-delivery.md](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) for format and purpose.

Each entry is a prompt to re-evaluate the original intent on current position state and current market signals, not a retry obligation. If the intent is re-expressed, it flows through the normal assessment pipeline: a revived ADD/ADJUST/CLOSE produces a new per-position assessment (new `SA-n`) and a revived CANCEL produces a new pending-order assessment (new `SA-ORD-n`). The [Action decision logic](#action-decision-logic), [Add burden of proof](#add-burden-of-proof), and [Maintain / modify / cancel criteria](#maintain--modify--cancel-criteria) apply unchanged — prior PM approval does not reduce the rationale burden, and the new rationale must be grounded in current signals, not the prior invocation's.

**CLOSE abandonments warrant priority attention.** An intended risk-reducing exit that did not execute is the most operationally consequential abandonment — the position is still open, still exposed, and the reasons for wanting to close may have intensified. The strategist should prioritize re-evaluating abandoned CLOSEs and, when the thesis remains invalidated or risk-reducing urgency persists, produce a fresh assessment with `close` as the recommended action.

If current signals no longer support the original intent (e.g., the thesis has evolved, the breach cured on its own, a better alternative has emerged), the entry lapses; no "decline" artifact is required. The activity log already captures the abandonment.

---

## Corporate-action-pending positions

Positions flagged `corporate_action_adjustment_needed` had their bracket cancelled when a corporate action fired on the underlying — stock split, reverse split, stock dividend, cash dividend, merger, acquisition, or spin-off (see [orders-and-brackets.md § Corporate action handling](../05-execution-layer/orders-and-brackets.md#corporate-action-handling)). Every flagged position is a first-class assessment item and must receive an `adjust-bracket` or `close` recommendation. Hold without a fresh bracket is not permitted — every AlphaMind position requires a complete bracket, so a flagged position either gets one produced this invocation or is closed.

**Default actions by corporate action type:**

- **Splits, reverse splits, stock dividends.** The thesis is economically unchanged; the position's share count and cost basis have been re-scaled by the OMS. Default to `adjust-bracket` with the prior bracket's parameters re-scaled to the new price basis. Reverse splits warrant additional scrutiny — they often signal distress — and the strategist should assess whether the thesis survives that signal before re-bracketing; if not, `close`.
- **Cash dividends.** The thesis is unchanged. Default to `adjust-bracket` with the prior bracket parameters unchanged — absolute-price stops remain correct because the underlying's price moved by the dividend amount, not by the thesis level. Unusually large special distributions that function as recapitalizations warrant a fresh thesis review; default to `close` when the distribution materially changes the company's capital structure.
- **Cash mergers and acquisitions.** The position has effectively liquidated at the deal price. Default to `close`.
- **Stock mergers.** The position has converted to the acquirer's shares. The pre-merger thesis on the target does not apply to the acquirer. Default to `close` unless the strategist can articulate a fresh thesis on the acquirer grounded in current signals (in which case propose `adjust-bracket` with new parameters appropriate to the acquirer).
- **Spin-offs.** The parent position's thesis may survive. Default to `adjust-bracket` at the parent's post-spin price basis. The spun-off child is a new position without a thesis; default to `close` unless the strategist proposes a fresh thesis on the child with appropriate bracket parameters.

**Rationale requirements:** the rationale names the corporate action type and its ratio or amount, states the thesis disposition (unchanged / needs re-evaluation / invalidated), and for `adjust-bracket` provides the updated bracket parameters tied to current signals. Standard action rationale burden applies (see [Action decision logic](#action-decision-logic)) — corporate-action-triggered re-bracketing is not a reduced-ceremony path. The reverse-split and large-special-distribution cases especially require explicit thesis survival reasoning rather than mechanical re-bracketing.

---

## Regime-transition remedy proposals

When the strategist's guardrail state header flags [regime-transition breaches](../06-risk-guardrails/regime-adaptation.md#position-handling-when-tightening-creates-breaches) or market-movement breaches on existing positions, the strategist is the agent responsible for proposing specific remedies. The strategist's position-level thesis context — target proximity, conviction, catalyst timing — makes it the right agent to decide *which* positions to reduce; the PM handles the cross-constraint validation and final execution decisions.

### Format: integrated into per-position assessments

Remedies are not a separate output section. Each flagged breaching position receives a normal per-position assessment whose `recommended_action` is the remedy (`reduce`, `close`, or `hold`), with the `remedy_flag` field set to the breach identifier from the guardrail state header (e.g., `BREACH-1`) and `remedy_rationale` explaining why this action is the right response given the thesis state.

This keeps the output model uniform — the PM evaluates every assessment through the same action-warrant / action-status / action-specific / portfolio-coherence rubric — while making remedies traceable to the breaches they address. The [portfolio-level observations](#per-position-assessment--structured-fields) section carries a `regime_transition_summary` listing which flagged breaches are addressed and which remain uncured with rationale.

### Remedy action selection

| Thesis state | Default remedy |
|--------------|----------------|
| `invalidated` | `close` (the thesis itself calls for close; the breach provides external confirmation) |
| `at-risk` with core component weakening | `close` or `reduce` (close when the breach overage exceeds what partial reduction can cure; reduce when a partial trim can cure the breach and the residual case is intact) |
| `on-track` or `partially-realized` with near-target proximity | `hold` with thesis-based rationale if the position is within hours of target resolution (cure the breach by resolution rather than by forced trim). Requires explicit naming of the target proximity and the expected resolution window |
| `on-track` with meaningful remaining horizon | `reduce` to compliance — trim the minimum quantity required to cure the breach while preserving the strongest-thesis positions. A full close on `on-track` to cure a sector breach is over-response |
| `stale` | `close` or `reduce` (the thesis has not confirmed within its window; the breach is a forcing function to act on the staleness) |

These are defaults, not mechanical rules. The strategist's per-position context may support a different choice — e.g., an `on-track` position with an unusually strong thesis and an imminent catalyst may warrant a `hold-with-rationale` remedy even when a sibling position with a weaker thesis could be trimmed. The choice is explicit in the remedy rationale.

### Remedy reduction quantity

When the remedy is `reduce`, the quantity must be:

- **Sufficient to cure the breach when combined with other per-position remedies addressing the same breach.** The aggregate of all remedies addressing a single breach should bring the breaching rule back into compliance — ideally to the 85% zone (below the critical threshold), not the 95% zone (one tick from re-breach).
- **Validated via the guardrail validation tool.** The strategist calls `validate_guardrail` with `action: "CLOSE"` (or partial close) and the proposed quantity to confirm the cumulative effect of the remedy set curing the breach. Cumulative-impact tracking across the tool's calls within the invocation makes multi-position remedy validation deterministic.

### Interaction with per-position thesis status

A remedy trim is an action, not a status classification. The thesis status for a remedy-flagged position is assigned on the same signal criteria as every other position — the breach is a reason to act, not a reason to reclassify. A `reduce` remedy on an `on-track` position is legitimate: the thesis is intact, the action is breach-driven not thesis-driven, and the action rationale should name the breach explicitly.

Conversely, if the breach coincides with independent signals weakening the thesis, the status should move independently and the remedy should follow the new status's default. A breach on a position that was already moving to `at-risk` produces an `at-risk` assessment with a `reduce` or `close` remedy whose rationale spans both the thesis weakening and the breach overage.

### Guardrail validation requirements

Remedy `reduce` and `close` actions on breaching positions require validation via the guardrail validation tool, for two reasons:

1. **Confirm the remedy cures the breach.** The tool's cumulative-impact tracking confirms the aggregate effect of the full remedy set on the breaching rule.
2. **Detect secondary breaches.** A close on a short position that was providing directional balance could push net long exposure into its own breach; the tool surfaces this before the remedy reaches the PM. When a secondary breach is detected, the strategist's remedy rationale should name it explicitly so the PM's cross-constraint review doesn't discover it fresh.

`hold-with-rationale` remedies do not require validation (they don't change exposure). The PM's cross-constraint impact summary (see [state-delivery.md](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header)) is how the PM sees the aggregate effect of the strategist's remedy set across the full rule surface.

---

## Halt mode and defensive-posture behavior

The strategist operates in two modes, set by the guardrail state header: `normal` and `defensive_posture`. Mode switches are driven upstream by the execution layer's halt-mode logic and the continuous monitor's emergency-invocation trigger (see [state-delivery.md — halt-mode header modifications](../06-risk-guardrails/state-delivery.md#halt-mode-header-modifications) and [breach-behavior.md — drawdown halt mode](../06-risk-guardrails/breach-behavior.md#drawdown-halt-mode)). The strategist reads the mode from the header; it does not decide when to switch.

### Defensive-posture mode

Triggered when daily drawdown halt or cumulative drawdown tier 3 is active. The upstream pipeline continues running — distillation, research, synthesizer — so the strategist still receives a full synthesizer brief. The change is behavioral, not informational.

**Behavioral shift:**

- **Emphasis pivots from balanced position management to risk reduction.** Under `normal`, the output mix across `hold`, `reduce`, `close`, `adjust-bracket`, and `add` reflects the current state of the book. Under `defensive_posture`, the emphasis pivots: the strategist actively looks for deterioration signals, stop-tightening candidates, and positions whose risk/reward has weakened even modestly, and recommends correspondingly defensive actions.
- **`add` is not permitted.** The action enum is restricted to `hold`, `reduce`, `close`, and `adjust-bracket`. The schema enforces this via a conditional on the mode field; the strategist should not produce an `add` action under defensive posture, and if a proposal that would be an `add` under `normal` is warranted on signal grounds, the strategist records it in the portfolio-level observations with a note that it is deferred until defensive posture lifts.
- **Hold thresholds raise.** Under `normal`, a `hold` on `on-track` with stable signals is the default; under `defensive_posture`, the same position may warrant a complementary bracket tightening (adjust-bracket to pull the stop in) even on `on-track`, because the environment that triggered the halt is itself a signal that existing asymmetries have weakened. The adjustment rationale should name the halt as the specific new signal.
- **Pending order review emphasis.** Under `defensive_posture`, pending entry orders are default-cancel candidates — they represent pre-halt commitments that the new market context may have rendered obsolete. The strategist should re-assess every pending entry and justify any `maintain` recommendation against the halt condition.
- **Portfolio-level observations emphasize capital preservation.** Aggregate thesis-health commentary shifts from "what does the book look like" to "where is the book most exposed to further drawdown, and what is the orderly-reduction priority if the PM needs to cut further." This output is the primary input for the PM's defensive-posture decision-making.

### Output mode flag and schema variant

The output document's top-level `mode` field is set to `defensive_posture` in this mode (analogous to the analyst's `watchlist` mode). The schema enforces:

- `recommended_action` on per-position assessments restricted to `hold | reduce | close | adjust-bracket`
- Pending order assessments permitted with the same structure as `normal` mode
- Portfolio-level observations include a required `defensive_posture_summary` section — an explicit orderly-reduction priority list the PM can act on if further risk reduction is needed

Under `normal` mode these restrictions lift and the `add` action is available. The schema is a strict superset across modes — the mode controls which subset of the action enum and which required fields apply.

### Emergency invocation handling

Emergency invocations (continuous-monitor-triggered, see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger)) are independent of halt mode — they indicate a sudden regime shift, multi-rule breach, drawdown velocity, or margin-call event that warrants immediate agent reasoning rather than waiting for the next scheduled invocation. Emergency invocations can co-occur with halt mode or with `normal` mode.

When the guardrail header includes the `** EMERGENCY INVOCATION **` flag, the strategist:

- **Prioritizes breach resolution.** Any regime-transition or market-movement breach flagged in the guardrail state header is addressed first — remedies for these breaches are the strategist's primary deliverable for the invocation, and the output should place the corresponding per-position assessments first.
- **Re-examines adjacent positions.** The triggering event (regime jump, multi-rule breach, margin call) typically bears on more than the breaching position itself. The strategist should assess whether adjacent positions — same sector, same catalyst, same directional exposure — warrant status changes and defensive adjustments, even if they are not themselves in breach.
- **Raises the bar for `hold` and defers discretionary recommendations.** New-information-driven adjustments to non-breaching positions that could wait for the next scheduled invocation should wait — the strategist's token budget is better spent on the urgent work.

If an emergency invocation fires while halt mode is active, both behaviors apply: the strategist operates under defensive-posture restrictions and prioritizes emergency breach resolution.

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

The strategist's input bundle is larger than the analyst's because it must carry full thesis component detail for every open position (the [strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) contributes the constraint portion; the thesis components come from portfolio state). Scales with portfolio complexity; the target ranges below replace the TBD in the state-delivery spec.

| Portfolio profile | Positions in typical range | Target context budget | Target output budget |
|-------------------|---------------------------|----------------------|---------------------|
| Primary ($1,500, no options/shorts) | 1–4 positions | 400–700 tokens | 300–600 tokens |
| Full-system ($100K) | 6–15 positions | 1,500–2,500 tokens | 1,200–2,500 tokens |

**Notes on the ranges:**

- The context budget covers the guardrail state header (constraint portion), pending orders, and active thesis records at component level from portfolio state. It does not include the synthesizer brief, which is shared across agents and is not a strategist-specific allocation.
- The output budget covers structured fields, per-position narratives, pending-order assessments, and portfolio-level observations. Per-position narrative length scales with thesis complexity, not with position count directly — a simple equity position with an uncomplicated thesis produces a shorter narrative than a multi-leg options strategy with several thesis components.
- These are sizing targets for the state-delivery layer, not behavioral targets for the strategist. The strategist does not have a per-invocation output-length target — the emphasis on quality over brevity applies, following the [feedback guidance on numeric anchors in LLM specs](../remaining-work.md). The ranges exist so the pipeline orchestrator can budget prompt construction; drift of individual invocations above the upper bound triggers a prompt-iteration review, not a mid-invocation truncation.

---

## Pre-submission guardrail validation

The strategist validates proposals that would change portfolio exposure — specifically **add** recommendations and any **close** or **reduce** recommendations that interact with guardrail constraints (e.g., closing a short position that would increase net long exposure beyond directional limits).

The strategist has access to the same guardrail validation tool as the analyst (see [analyst.md](analyst.md) for the full specification). The workflow is identical: propose, validate, revise if needed. The tool computes delta-adjusted exposure for options, tracks cumulative impact across multiple recommendations, and returns per-rule pass/fail with headroom.

**Hold and adjust-bracket recommendations** do not require guardrail validation — they don't change exposure. **Close and reduce recommendations** are generally guardrail-relieving (reducing exposure), but the strategist should be aware of cases where closing one position inadvertently pushes another constraint over the limit (e.g., closing a short that was providing directional balance). The headroom data in the strategist's guardrail state header makes these interactions visible; the validation tool confirms.

**Interaction with analyst proposals:** The strategist and analyst run in parallel with no visibility into each other's proposals. The strategist validates against current portfolio state, not against a hypothetical state that includes the analyst's proposals. This means the combined set of analyst + strategist proposals might collectively breach a limit even though each set is individually compliant. This is an accepted trade-off of the parallel execution model — the portfolio manager evaluates both sets holistically and can reject or downsize proposals to maintain compliance. See [portfolio-manager.md](portfolio-manager.md) for how the PM handles cross-proposal interactions.

---

## Source brief retrieval

The strategist has the same retrieval tool as the analyst — it can pull source analysis brief sections by reference ID from the synthesizer output. This is particularly useful when:

- Evaluating whether new information contradicts a specific key assumption in an existing thesis component
- Investigating whether a signal cited in the thesis entry rationale has strengthened, weakened, or reversed
- Assessing cross-position dynamics by retrieving correlation or regime data from `[CR-*]` references

---

## Relationship to other agents

| Agent | Relationship |
|-------|-------------|
| [Analyst](analyst.md) | Runs in parallel — no direct interaction. Both produce structured output that feeds into the [proposal pre-processor](proposal-pre-processor.md). The pre-processor detects same-name conflicts (e.g., analyst proposes new NVDA position while strategist recommends closing existing NVDA position) and annotates them for the PM |
| [Proposal pre-processor](proposal-pre-processor.md) | Deterministic processing step that reads structured fields from both the analyst and strategist outputs, computes cross-proposal annotations (conflicts, cumulative exposure, sector impact), and delivers annotated proposals to the PM |
| [Portfolio manager](portfolio-manager.md) | The ultimate consumer. Receives the strategist's position assessments alongside the analyst's new trade proposals, with pre-processor annotations, and makes holistic portfolio decisions |

---

## Dependencies

- [Synthesizer](../03-analysis-layer/synthesizer.md) — primary market context input
- [Portfolio state (raw)](../01-data-layer/internal/portfolio-state.md) — position inventory, P/L, thesis registry, activity log
- [Thesis model](../05-execution-layer/thesis-model.md) — defines thesis structure and status classifications
- [OMS commands](../05-execution-layer/oms-commands.md) — the action parameters in the strategist's output must map to valid CLOSE, ADJUST, ADD, and CANCEL command parameters
- [Risk guardrails / state delivery](../06-risk-guardrails/state-delivery.md) — guardrail headroom informs action recommendations
