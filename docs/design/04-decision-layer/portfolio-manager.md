# Portfolio manager

Operates in a fresh context window. **Mandate:** critically evaluate the analyst's and strategist's recommendations from a portfolio management perspective. The skeptical manager who pressure-tests every thesis before committing capital and ensures portfolio coherence across new entries and position changes.

**Key design principle:** the PM focuses on **judgment** — thesis quality, portfolio coherence, strategic reasoning. Mechanical cross-referencing (same-name conflict detection, cumulative exposure computation, sector shift calculation) is handled by the [proposal pre-processor](proposal-pre-processor.md) before the PM's context window opens. Guardrail feasibility is handled by upstream validation (analyst/strategist pre-submission checks). The PM receives proposals that are guardrail-compliant and cross-annotated, and applies investment judgment.

Risk guardrails (max position size, max daily drawdown, correlation limits) are hard-coded programmatic constraints — not LLM-mediated. See [risk guardrails](../06-risk-guardrails/README.md). Proposals arrive guardrail-validated; the PM validates its own modifications; the execution layer provides a final authoritative check with synchronous feedback.

---

## Inputs

Delivered at invocation start. Source documents are authoritative for each input's content.

| Input | Source | Description |
|---|---|---|
| Proposal pre-processor bundle | [proposal-pre-processor.md](proposal-pre-processor.md), schema in [proposal-pre-processor-bundle-schema.md](proposal-pre-processor-bundle-schema.md) | The annotated bundle: analyst proposals + strategist assessments + cross-proposal annotations (combined-set impact, conflict cross-references, conviction distribution, book health summary) |
| Synthesizer brief | [synthesizer.md](../03-analysis-layer/synthesizer.md) | Prose synthesis with embedded `[SA-*]`, `[QR-*]`, `[AR-*]`, `[CR-*]` references — the same brief the analyst and strategist read, providing the PM with first-hand market context for evaluating their narratives |
| Portfolio state | [portfolio-state.md](../01-data-layer/internal/portfolio-state.md) | Position inventory (§1a) with sector and directional exposure (§1b); position-level and portfolio-level P/L plus drawdown tracking (§2a–§2c); active thesis records at the summary level (§3a) — full component-level retrieval is via the tool below; recent thesis resolutions (§3c); capital and capacity (§4a–§4d); activity log (§5a intra-invocation changelog, §5b PM decision log, §5c position modification trail) — used by the evaluation framework for `prior_status` history and engine-originated-action awareness; thesis quality trends (§6) |
| PM guardrail state header | [state-delivery.md — Portfolio manager guardrail state header](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header) | Formatted text block at the top of the prompt: full constraint visibility, cross-constraint impact summary (showing combined-set effects), correlation state, recent engine-originated actions prominence cue, regime overrides, hard blocks |

Tools available during reasoning:

| Tool | Source | Use |
|---|---|---|
| Source-brief retrieval | [Retrieval tools — Source-brief retrieval](#source-brief-retrieval) | Pull a section of an analysis brief by reference ID from the synthesizer's retrieval store. Used to verify analyst/strategist narrative claims against the underlying source signal |
| Thesis-component retrieval | [Retrieval tools — Thesis-component retrieval](#thesis-component-retrieval) | `get_thesis_components(position_id)` — pulls full component-level thesis record for one held position. Required because thesis records are delivered at summary level by default for the PM |
| Guardrail validation tool | [state-delivery.md — Guardrail validation tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) | Deterministic check on PM modifications (sizing adjustments) before commands are finalized. See [Pre-submission guardrail check on PM modifications](#pm-originated-envelopes-pm_analyst-and-pm_strategist) |
| Envelope submission tool | [submit-envelope-tool-schema.md](submit-envelope-tool-schema.md) | `submit_envelope(envelope)` — submits one finalized envelope to the engine and returns a per-command result list with synchronous accept/reject payloads. See [Synchronous command feedback](#synchronous-command-feedback) |

The volatility regime label is delivered as the `Regime:` line in the guardrail state header.

---

## Output

The PM's output is one or more **command envelopes** validating against [pm-envelope-schema.md](pm-envelope-schema.md) (Draft 2020-12). Embedded OMS commands validate against [oms-command-schema.md](../05-execution-layer/oms-command-schema.md). Engine-originated envelopes are a sibling contract in [engine-envelope-schema.md](../05-execution-layer/engine-envelope-schema.md) — produced by the continuous monitor.

The PM emits one envelope per source proposal (one `pm_analyst` per analyst recommendation, one `pm_strategist` per strategist position or pending-order assessment). A single invocation typically produces several. Approved envelopes carry the resulting OMS commands (OPEN/CLOSE/ADJUST/ADD/CANCEL); rejected envelopes carry an empty `commands` array and a populated rationale — rejections are first-class records the feedback loop reads.

---

## Responsibilities

- Evaluate thesis quality for new trade recommendations: Is the reasoning sound? Are there counterarguments the analyst missed? (Reads analyst narrative fields)
- Evaluate strategist position assessments: Does the recommended action align with current thesis status and market context? (Reads strategist narrative fields)
- Resolve flagged conflicts: The pre-processor identifies same-name conflicts; the PM makes the judgment call (e.g., close existing AND enter new, or close and skip, or hold and skip)
- Assess portfolio-level risk using pre-processor annotations: cumulative exposure impact, sector shift preview, and combined-set guardrail breaches inform approve/reject decisions
- Adjust position sizing (with guardrail validation on modifications)
- Reject recommendations that don't meet quality thresholds
- Determine execution priority: the pre-processor provides entry-window-based ordering; the PM can override on thesis quality or strategic grounds
- Execute approved trades through the [execution layer](../05-execution-layer/README.md)

---

## Envelope structure

Every OMS command is wrapped in a **command envelope** providing full traceability regardless of origin. This separates "what to execute" from "why and how it was decided."

The most common envelope type is **PM-originated** — the PM evaluates a proposal, produces an envelope with its evaluation and modifications, and the resulting OMS command(s). Envelopes can also be **engine-originated** — the continuous monitor detects a guardrail breach from market movement and generates a protective CLOSE command with a guardrail trigger record. Both share the same base structure, ensuring the activity log, feedback loop, and thesis resolution system operate uniformly.

The engine extracts OMS commands from envelopes. The activity log stores the full envelopes, providing traceability from every execution action back to its origin and reasoning.

### Base envelope structure (all provenance types)

- **Envelope ID:** unique identifier for this decision
- **Invocation ID:** which pipeline invocation this decision belongs to (for engine-originated envelopes generated between invocations, this is `null` and a **trigger timestamp** is recorded instead)
- **Source provenance:** one of `pm_analyst`, `pm_strategist`, or `engine_guardrail` — identifies the origin of the command. See provenance-specific fields below
- **Commands:** the resulting OMS command(s). Contains OPEN/CLOSE/ADJUST/ADD/CANCEL commands for approved proposals. Empty for rejections and holds (PM-originated only)

### PM-originated envelopes (`pm_analyst` and `pm_strategist`)

The primary envelope type — the PM's evaluation of proposals from the analyst or strategist.

- **Source recommendation ID** and **recommendation type** (new_entry for analyst proposals, position_assessment for strategist proposals). For strategist proposals, also includes the position ID
- **Evaluation:** the PM's verdict (approve, approve_with_modification, reject) and a structured quality assessment whose criterion set depends on source type. For new entries (`pm_analyst`): falsifiability, sizing proportionality, portfolio coherence, timing plausibility, counterargument consideration. For position actions (`pm_strategist`): status classification warrant, action-status alignment, action-specific justification, portfolio coherence. All pass/fail. See [Evaluation framework](#evaluation-framework) for per-criterion detail, verdict aggregation, and failure patterns. Every envelope carries a concerns list (may be empty) and a rationale narrative
- **Modifications:** zero or more structured records of what the PM changed. Each records: field changed, original proposed value, approved value, adjustment category (risk_reduction, conviction_disagreement, capital_constraint, portfolio_balance, guardrail_rejection_response), and a per-modification rationale. Empty for pass-through approvals and rejections

**Rejections as first-class records:** Rejected proposals produce envelopes with no commands. The feedback loop can analyze "what proposals does the PM reject and why?" without needing an OMS command to attach metadata to.

### Engine-originated envelopes (`engine_guardrail`)

Generated by the continuous monitor when a guardrail breach is detected between invocations (or within an invocation's collect phase) from market movement, regime changes, or margin events — not from a new command failing validation. These envelopes carry a **guardrail trigger record** instead of a PM evaluation:

- **Trigger timestamp:** when the breach was detected
- **Rule breached:** which guardrail rule was violated (e.g., daily drawdown limit, position-level max loss, margin call)
- **Breach details:** current value, limit value, overage amount, and regime at breach time
- **Position selection logic:** why this position was selected for reduction — e.g., "smallest position in breaching sector," "highest unrealized loss," "position triggering position-level max loss." Deterministic and configurable — see [breach-behavior.md](../06-risk-guardrails/breach-behavior.md)
- **Close rationale type:** always `risk_management` with sub-type `engine_guardrail` — distinguishes engine-originated from PM-originated risk-management closures in the feedback loop

**Constraints:** The engine issues CLOSE commands (full or partial) only via this path. Constructive actions (OPEN, ADD, ADJUST) require PM judgment.

**Thesis resolution:** When an engine-originated CLOSE resolves a thesis, the resolution carries provenance `engine_guardrail` with the full trigger record. The feedback loop segments these from PM-originated closures to answer: "how often does the engine force-close positions, and are those closures premature or beneficial?"

**PM visibility:** Engine-originated envelopes appear in the activity log and the strategist's and PM's next-invocation input bundles — via [portfolio state §5](../01-data-layer/internal/portfolio-state.md) and the [PM guardrail state header's `Recent engine-originated actions` block](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header). The PM sees: "engine force-closed POS-AVGO-001 at 10:47 because semi sector exposure breached the elevated-regime limit (8.5% vs. 8.0%)." Full awareness of between-invocation actions to factor into subsequent decisions.

**Envelope processing order:** The PM produces all envelopes before any commands submit to the engine. This allows consideration of cross-proposal interactions (e.g., an analyst new entry + a strategist close on the same name). Commands then submit sequentially. See "Synchronous command feedback" below.

**Pre-submission guardrail check on PM modifications:** When the PM modifies a proposal's parameters (e.g., position size from 10 to 15 contracts), the modified parameters are validated before the command is finalized. The PM uses the same guardrail validation tool as the analyst and strategist (see [analyst.md](analyst.md)). If a modification would breach a limit, the PM revises or accepts the original sizing.

---

## Evaluation framework

The PM applies two linked frameworks: **thesis quality** for new-entry proposals from the [analyst](analyst.md), and **position-action quality** for existing-position recommendations from the [strategist](strategist.md). Both produce pass/fail flags that populate the envelope's `evaluation` field and, with the cure-vs-reject principle, determine the verdict (`approve`, `approve_with_modification`, `reject`).

**Structural, not ideological.** Criteria check whether required reasoning elements are present, internally consistent, and supported by cited inputs — not whether the PM agrees with the call. Agreement on direction and sizing is a separate concern expressed through the verdict and modification records. A proposal can pass every thesis-quality criterion and still be rejected on portfolio-level grounds; conversely, a proposal whose direction the PM favors must still fail if its reasoning is structurally incomplete.

**Retrieval for evaluation.** When a criterion turns on whether a narrative correctly characterizes a signal, the PM uses [source-brief retrieval](#retrieval-tools) to check the cited analysis-brief reference. When a criterion turns on whether a strategist's status rationale cites a thesis component correctly, the PM uses [thesis-component retrieval](#retrieval-tools). This is the primary mechanism for catching narratives that mischaracterize the synthesizer, source briefs, or the underlying thesis.

### Thesis quality evaluation (new-entry proposals)

For each analyst proposal, the PM emits pass/fail on five criteria. Each criterion is evaluated against specific structured and narrative fields in the [analyst output schema](analyst-output-schema.md).

**1. Falsifiability** — whether the thesis could be proven wrong by an event observable inside the time horizon, and whether the hard invalidation leg(s) tie to the causal chain rather than to generic price movement.

*Reads:* `thesis_narrative`, `invalidation_legs`, `invalidation_rationale`, `time_expectation_hours`.

*Fails when:* the invalidation rationale is generic ("N% stop loss", "price goes against us") rather than tied to the specific signal or mechanism the thesis depends on; the hard leg's trigger is not reachable within the time horizon; the thesis is written in unfalsifiable prose ("could benefit from", "may extend") such that no observable outcome would clearly disprove it; a critical event-based invalidation is missing — specifically, a catalyst that could fire against the thesis before the price leg would trigger.

*Passes when:* at least one hard leg is tied to a named signal or mechanism in the narrative; invalidation events are physically reachable within the horizon; the narrative commits to what would make it wrong.

**2. Sizing proportionality** — whether the proposed size is proportionate to the claimed conviction and the instrument's risk profile.

*Reads:* `conviction_level`, `position_size.pct_of_portfolio`, `position_size.premium_at_risk` (capital at risk — the USD magnitude of the position's worst-case loss; present for defined-risk options/strategies, covering both debit positions and net-credit strategies), `position_size_rationale`, `instrument.asset_type`.

*Fails when:* sizing falls outside the conviction level's [advisory band](analyst.md#conviction-scale) without a concrete rationale for the deviation; sizing sits at a band extreme without addressing the risk-profile nuance the conviction scale requires (e.g., top-of-band on open-ended equity risk without a corresponding argument about tight invalidation or low correlation); the stated rationale contradicts the structured fields (claims defined-risk sizing on an equity position); the thesis narrative describes evidence whose quality, convergence, and catalyst hardness do not support the declared conviction — making the sizing inflated at the root rather than at the mapping.

*Passes when:* sizing falls within the advisory band with an intelligible rationale, or sits outside the band with a concrete, instrument-appropriate justification.

**3. Portfolio coherence** — whether the trade fits the current book: correlation, concentration, catalyst overlap, and interaction with concurrent proposals.

*Reads:* the [proposal pre-processor's](proposal-pre-processor.md) cross-constraint impact summary, sector exposure shift preview, conflict records, and conviction distribution; correlation state from the [PM guardrail state header](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header); position-level data from portfolio state; the thesis narratives of held positions (summary by default, components via [thesis-component retrieval](#retrieval-tools)) and of other in-flight analyst/strategist recommendations.

*Fails when:* the trade's catalyst is already carried by equivalent held positions such that approval materially concentrates catalyst risk (e.g., another hyperscaler-capex-driven long when equivalent positions already dominate the sector); approval would push weighted-average pairwise correlation into a warning or critical zone without offsetting action; a pre-processor same-underlying conflict (`entry_vs_close`, `entry_direction_conflict`) is present without a coherent resolution elsewhere in the PM's envelope set; the proposal contradicts a held thesis whose invalidation has not been separately recommended; the sector shift preview implies a concentration inconsistent with the PM's read of the current market environment.

*Passes when:* the projected post-approval state does not materially concentrate catalyst, correlation, or sector risk; any conflict records have coherent resolutions elsewhere in the envelope set; catalyst overlap is incidental rather than load-bearing.

**4. Timing plausibility** — whether the time expectation is consistent with the catalyst's timing, and whether the entry window and time-based invalidation legs fit the thesis.

*Reads:* `time_expectation_hours`, `target_rationale`, `entry_window`, invalidation legs of type `time`, and the synthesizer brief for catalyst timing.

*Fails when:* the time expectation does not bracket the catalyst with plausible cushion (e.g., a 4-hour horizon for a 36-hour catalyst with no entry-window urgency); an `entry_window` deadline post-dates the catalyst timestamp when `decay_type` is `binary` (the setup's edge resolves before entry can occur); a time-based invalidation leg's deadline fires before the catalyst can resolve the thesis, guaranteeing a stop-out by time regardless of whether the thesis was right; the decay-type categorization is inconsistent with the catalyst's actual character (claimed `binary` for a gradual information edge, or vice versa); `time_expectation_hours` exceeds the system's 4–72 hour horizon without a setup-specific justification.

*Passes when:* the time expectation brackets the catalyst; entry window and time-based legs are mutually consistent and consistent with the catalyst's direction.

**5. Counterargument consideration** — whether the analyst has engaged with the strongest thesis-specific bear case rather than logging a generic risk disclaimer.

*Reads:* `counterarguments_acknowledged`, `thesis_narrative`, and the synthesizer brief for contradictions or uncertainties bearing on the thesis.

*Fails when:* the counterargument field contains only generic risk language ("market could sell off", "an earnings miss is possible") rather than a thesis-specific bear case; a contradiction flagged by the synthesizer bearing on this thesis is absent from both the narrative and the counterargument field; the stated reason to proceed despite the counterargument is non-responsive — it repeats the bull case instead of explaining why the bear case is subordinate or contingent; the counterargument, if it fires, would invalidate the causal chain and the analyst has not provided a thesis-consistent reason to proceed at the stated conviction.

*Passes when:* a specific, thesis-relevant bear case is named; the stated reason to proceed addresses why it is subordinate, outweighed, or contingent on something the analyst does not expect.

**Criterion independence.** The criteria are evaluated independently. Failing one does not automatically fail another. Joint failure patterns (e.g., a weak counterargument paired with a high conviction label paired with top-of-band sizing) are genuine patterns rather than single criterion failures and are surfaced as such in the rationale narrative — see [anti-patterns](#anti-patterns-the-pm-is-watching-for) below.

### Position-action evaluation (strategist recommendations)

For each strategist position assessment, the PM evaluates the recommended action against a parallel but distinct set of criteria. The emphasis shifts from "is this a well-constructed new bet" to "is this action warranted by new information, and does it fit the claimed status".

**1. Status classification warrant** — whether the [thesis status](../05-execution-layer/thesis-model.md#thesis-status-classifications) the strategist assigned (on-track / partially-realized / at-risk / stale / invalidated) is supported by specific new signals from this invocation's synthesizer, not by rehashing entry context.

*Reads:* `thesis_status`, `prior_status`, `status_rationale`, the synthesizer brief (and source briefs via [retrieval](#retrieval-tools) when verifying a cited finding), and the underlying thesis record at component level via [thesis-component retrieval](#retrieval-tools) when verifying a cited component against the strategist's characterization.

*Fails when:* the status rationale cites no new signal (e.g., "still at-risk" with no reference to fresh findings); an `invalidated` classification is not tied to a concrete invalidation event (a triggered event leg, a specific thesis-breaking finding); a status transition (e.g., on-track → at-risk) is claimed without a rationale for what changed; the rationale cites a signal that does not appear in the synthesizer brief or in a retrievable source brief; the classification contradicts the cited evidence (signals strengthened, status downgraded).

*Passes when:* the rationale ties the classification to a specific current finding with a traceable reference; transitions are explained by what changed since the prior status.

**2. Action-status alignment** — whether the recommended action fits the assigned status.

*Reads:* `thesis_status`, `recommended_action`, `action_rationale`.

*Fails when:* an `invalidated` status is paired with any action other than `close` (the position's own status says it should be closed; holding or reducing is self-contradictory); an `on-track` status is paired with `add` without a separately identified strengthening signal in `add_conviction_justification` (P/L alone and "thesis is playing out" are not strengthening signals); a `stale` status is paired with `hold` without a concrete reason the horizon should be extended; an `at-risk` status is paired with an unguarded hold (no stop tightening, no partial reduce) when the at-risk signal implies asymmetric downside.

*Passes when:* the action reflects the status's implied disposition, or a deviation is explained by a specific factor (e.g., within-hours proximity to target justifies holding a stale thesis through the remaining window).

**3. Action-specific justification** — whether the per-action narrative field justifies the specific parameters.

*Reads:* `reduce_rationale` (for `reduce`), `add_conviction_justification` (for `add`), `adjustment_rationale` (for `adjust-bracket`); the corresponding action parameters (quantity, new stop level, new target, new deadline).

*Fails when:* a `reduce` does not explain why partial rather than full (or vice versa for the quantity chosen); an `add` cites "thesis playing out as expected" rather than a new convergent signal absent at entry — a validating signal is a hold reason, not an add reason; an `adjust-bracket` widens a stop or extends the time horizon without a concrete reason the original level was wrong (the default read on stop-widening and horizon-extension is that the strategist is rationalizing rather than responding to new information, and the burden of proof sits with the strategist to name the new signal); the specific parameter — a partial-close fraction, a new stop level, a new deadline — is unjustified relative to the cited signal.

*Passes when:* the narrative names the specific signal and explains why it maps to the chosen parameter.

**4. Portfolio coherence** — same definition and reads as new-entry criterion 3, applied to the net book state after all strategist and analyst recommendations execute.

*Strategist-specific failure modes:* closing a position that provides directional balance, concentration offset, or an explicit hedge without recognizing the secondary effect on exposure; adding to a position already near a correlation or concentration boundary; proposing a combination of trims that would cure one sector breach while creating a directional-exposure breach visible in the cross-constraint summary. The [strategist's regime-transition remedy proposals](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header) are evaluated under this criterion — the strategist may propose remedies that look reasonable at the position level but conflict at the portfolio level, and the cross-constraint impact summary is how the PM sees the conflict.

### Verdict aggregation

The pass/fail flags feed a verdict via a **cure-vs-reject principle**: a failure curable by a sizing, parameter, or priority adjustment within PM authority becomes `approve_with_modification`; a failure requiring the originating agent to re-draft the thesis or status classification becomes `reject`. The PM does not rewrite theses or re-classify status — those require the originating agent at the next invocation.

**Default mapping per failure mode (new-entry proposals):**

| Failed criterion | Typical verdict | Rationale |
|---|---|---|
| Falsifiability | reject | An unfalsifiable thesis cannot be cured by sizing or priority changes; the analyst must redraft. |
| Sizing proportionality | approve_with_modification | The PM resizes (within the advisory band, or outside it with documented rationale); the thesis itself is intact. The new size is validated against the [guardrail tool](../06-risk-guardrails/state-delivery.md#guardrail-validation-tool) per the [pre-submission check](#pm-originated-envelopes-pm_analyst-and-pm_strategist) above. |
| Portfolio coherence | depends on failure mode | Same-catalyst stacking or correlation concentration: modification (downsizing) if the thesis is otherwise strong; rejection if stacking is structural. Unresolved same-underlying or direction conflict: reject one side after determining which thesis dominates the net book. Unresolved thesis contradiction against a held position: reject. |
| Timing plausibility | typically reject | A time-expectation/catalyst mismatch is a structural error. Exception: a misconfigured `entry_window` deadline or a time-leg deadline the PM can correct without changing the setup → modification. |
| Counterargument consideration | reject | A missing or non-responsive counterargument means the analyst did not complete the reasoning the thesis requires. The PM does not manufacture the missing argument. |

**Default mapping per failure mode (strategist recommendations):**

| Failed criterion | Typical verdict | Rationale |
|---|---|---|
| Status classification warrant | reject | An unjustified status classification undermines every downstream element of the assessment. Strategist must re-classify at the next invocation. |
| Action-status alignment | reject | The action contradicts the position's own status. The PM does not substitute a different action — the strategist must revise. |
| Action-specific justification | depends on failure mode | A parameter-level issue the PM can correct without changing the action (e.g., converting an unjustified partial close to a full close when the thesis is classified `invalidated`) → modification. A missing add-conviction justification or a bracket-widening rationale that amounts to rationalization → reject. |
| Portfolio coherence | typically modification | The PM can reorder, resize, or skip an otherwise-valid action to preserve book coherence; the action itself is not structurally flawed. |

**Sizing-proportionality vs. conviction disagreement.** When the PM disagrees with the analyst's conviction-to-size mapping but the criterion passes (sizing within band, narrative supports the conviction), the PM may still modify. Captured via the `conviction_disagreement` adjustment category rather than a criterion failure. The feedback loop tracks whether overrides improve or worsen outcomes over time.

**Pass-through approvals.** When all criteria pass and the PM has no modifications, the envelope records `verdict = approve` with empty modifications and concerns. The expected case for well-formed proposals — the framework flags structural problems, not disagreement for its own sake.

**Concerns list vs. rationale narrative.** The concerns list is populated from failed criteria (one entry per failure, naming the criterion) plus any additional PM concerns not captured by a criterion. The rationale narrative is the PM's prose explanation — names the failure pattern where applicable, explains cross-criterion interactions, and documents reasoning in a form the feedback loop can read.

### Anti-patterns the PM is watching for

Several failure modes manifest across multiple criteria. When the PM detects one, the rationale narrative names the pattern rather than listing criterion failures in isolation — the pattern name is what the feedback loop aggregates on.

**Conviction inflation.** A proposal whose narrative describes thin signal convergence (two signals, at least one flagged contradiction, a soft catalyst) carrying a conviction 4 or 5 label and sizing at the top of the advisory band. Sizing-proportionality fails at the root (the conviction itself is inflated, not just the mapping); counterargument often fails alongside (contradiction under-addressed). Tracked across invocations via the [pre-processor's conviction distribution annotation](proposal-pre-processor.md); the PM's rejections for this pattern are the primary defense against calibration drift.

**Sunk-cost persistence.** A strategist `hold` on a position whose status has been `at-risk` or `stale` across multiple prior invocations without a new supporting signal. Status-warrant and action-specific criteria catch the single-invocation manifestation; the PM's visibility into `prior_status` and the [activity log via portfolio state §5](../01-data-layer/internal/portfolio-state.md) (PM decision log §5b sliding window) lets it recognize the aggregation that distinguishes sunk-cost from legitimate catalyst-proximity holding.

**Rationalized continuation.** A strategist `add` or `adjust-bracket` on a losing position, framed as responding to new information but whose rationale restates the entry thesis. Action-specific catches this — the default read on bracket-widening and horizon-extension is that the burden of proof sits with the strategist to name a concretely new signal. The mirror-image of the [ROLL command rationale](../design-decisions.md) the system excludes: the same cognitive shortcut the OMS vocabulary prevents structurally, the evaluation framework catches behaviorally.

**Thesis-contradiction suppression.** A new-entry narrative or strategist status rationale that omits a contradiction or uncertainty the synthesizer flagged bearing on the cited signal. Counterargument-consideration (new entries) and status-warrant (strategist actions) catch this; source-brief retrieval verifies the underlying characterization when something smells wrong.

**Engine-originated closure as a signal.** When an engine-originated envelope appears in the activity log (e.g., a force-close on a position-level max-loss breach), any strategist recommendation treating the closed position's sector or thesis as unchanged is suspect. The PM's [guardrail state header surfaces recent engine-originated actions](../06-risk-guardrails/state-delivery.md#portfolio-manager-guardrail-state-header) as a prominence cue; the activity log via [portfolio state §5](../01-data-layer/internal/portfolio-state.md) carries the full record. A coherence-criterion failure when cross-position observations ignore the engine action.

---

## Existing position management guidance

The evaluation framework covers per-recommendation decisions on strategist proposals. Three recurring scenarios warrant explicit guidance — they surface repeatedly, intersect multiple criteria, and benefit from a consistent default procedure: **aging theses**, **partial invalidation**, and the **time-limit approach**. This section also makes the PM's modification authority explicit, which the [envelope model](#pm-originated-envelopes-pm_analyst-and-pm_strategist) implies but does not state directly.

### Scope of PM authority for existing-position envelopes

The PM modifies strategist recommendations within bounded authority — preventing drift into the strategist's lane (position-level thesis expertise, status classification) or the analyst's lane (new-entry thesis construction).

**Permitted modifications:**
- **Parameter adjustments within the recommended action.** Quantity of a reduce, stop level or target or deadline of an adjust-bracket, limit price of a close, order type on any exposure-changing action. The action type stays the same. The pre-submission guardrail check applies — modified parameters must pass the validation tool.
- **Risk-reducing complementary commands.** Adding an `adjust-bracket` command to a `hold` or `reduce` recommendation to tighten a stop or pull in a time leg. Recorded as a modification with adjustment category `risk_reduction` and a rationale explaining the added protection. The strategist's original action remains primary; the complementary command layers defensive context.

**Outside PM authority (use rejection instead):**
- **Action replacement.** Changing the strategist's action type — `hold` → `close`, `reduce` → `close`, `close` → `add` — is not a parameter modification. The semantic difference between actions (bracket cancellation on full close, `close_rationale_type` requirements, thesis-resolution implications) is structural. The PM's mechanism for disagreement is rejection, which carries to the next invocation for strategist re-evaluation against fresh synthesizer data.
- **Adding constructive commands.** `OPEN` and `ADD` commands require full thesis construction — the analyst's domain. A gap is noted for the feedback loop, not bolted onto a strategist-originated envelope.

The rejection path preserves the separation between strategist position-level expertise and PM portfolio-level authority. The [continuous monitor's protective-close authority](../05-execution-layer/oms-commands.md#command-origins) is a separate emergency-risk-management mechanism.

### Aging theses

A position whose `time_expectation_hours` has elapsed without the catalyst firing or reaching target.

**Signals:**
- Position age exceeds `time_expectation_hours` on the thesis record
- Strategist classifies `thesis_status` as `stale` — or continues `on-track` past the time expectation (evaluation framework flags as status-warrant failure)
- `prior_status` shows consecutive invocations at `at-risk` or `stale` without transition — sunk-cost persistence anti-pattern applies

**Default PM response:**
- **Strategist recommends `close` on `stale`:** approve.
- **Strategist recommends `hold` on `stale`:** approve only if `action_rationale` names a reason the horizon should be extended (typically a publicly rescheduled catalyst with a new timestamp). Otherwise reject.
- **Strategist recommends `hold` on a position past time expectation but classifies as `on-track`:** reject. The classification-warrant criterion fails regardless of the hold.
- **When approving any hold on an aging thesis:** modify by adding a complementary `adjust-bracket` command to tighten the stop or pull in the time leg. Time decay and drift risk accumulate as a position ages past expected resolution; an unguarded hold is higher-risk than at entry.

### Partial invalidation

The strategist's `status_rationale` identifies specific [thesis components](../05-execution-layer/thesis-model.md#thesis-structure) as invalidated while others remain intact. Partial invalidation is a first-class state — the thesis model enforces component-level structure.

**Signals:**
- Strategist classifies `thesis_status` as `partially-realized` or `at-risk` with component-level specificity
- `status_rationale` cites a signal contradicting a specific component while others hold
- A multi-leg strategy position where one leg's rationale is undermined but the other's is not

**Default PM response:**
- **Strategist recommends `reduce` tied to the invalidated component:** approve. Check that reduction quantity maps to the failed portion — a non-core key-assumption invalidation warrants a smaller reduction than a core-mechanism invalidation. If quantity looks disproportionate, modify rather than reject.
- **Strategist recommends `hold` on partial invalidation:** default to reject. A position sized for the full thesis held against a partially invalidated thesis is over-sized by definition. Approve only when `status_rationale` demonstrates the invalidated component was non-core (supporting rather than driving the causal chain).
- **Strategist recommends `close` on partial invalidation:** evaluate whether the residual thesis could stand alone. If so, reject (close discards optionality the residual preserves). If the invalidated component was load-bearing, approve.
- **Complementary bracket tightening.** Partial invalidation narrows the plausible path to target. When approving a `reduce` without an accompanying `adjust-bracket`, consider adding bracket tightening as a risk-reducing complementary command.

### Time-limit approach

**The system is thesis-based, not time-based** (see [design-decisions.md](../design-decisions.md#why-thesis-based-position-management-over-time-based)). `time_expectation_hours` is a soft target feeding strategist classification triggers (past-expectation tends to produce `stale`) but not a mechanical close trigger. Time-based invalidation legs are hard limits mechanically enforced by the execution layer.

**PM stance on time:**
- `time_expectation_hours` expiring is a signal for the strategist to re-evaluate, not for the PM to force a close. The path: past-expectation → strategist reclassifies → PM evaluates the new classification and action. Bypassing this conflicts with [Scope of PM authority](#scope-of-pm-authority-for-existing-position-envelopes).
- Extending a time-based invalidation leg via `adjust-bracket` is the most common manifestation of [rationalized continuation](#anti-patterns-the-pm-is-watching-for). The default read on any time-leg push-out: rationalization unless concrete new information is cited.
- **Narrow exception — rescheduled catalyst.** A publicly rescheduled catalyst (earnings delayed to a specific new date, FOMC postponed, trial date moved) justifies extending to match the new timestamp. The rationale must cite the reschedule specifically.
- **Narrow exception — post-catalyst reaction window.** A fired catalyst whose market impact has not fully played out may warrant a modest extension tied to the expected reaction window. The rationale must name the reaction window concretely.

**Interaction with regime changes:** when a regime tightens mid-position, time-based legs typically need no modification — the regime-transition remedy process handles size and exposure breaches. Time-leg adjustments are per-position judgment calls, bundled into regime responses only when the regime change specifically affects catalyst timing (e.g., a crisis regime may delay corporate actions).

---

## Synchronous command feedback

When the PM submits OMS commands, the engine validates each command and returns a result **synchronously within the same invocation**. The PM receives either a success acknowledgment or rejection payload for each command, and retains agency to respond.

**Why synchronous:** The analyst and strategist pre-validate against guardrail state, but portfolio state can shift between their validation and PM submission — market movement, fills resolving during the invocation's collect phase, or intra-invocation regime changes. PM modifications add another source of drift. Synchronous feedback closes the loop: the PM knows immediately whether each command succeeded.

**On command rejection:** The rejection payload includes which guardrail(s) blocked the command, current limit values, and a suggested modification (see [oms-commands.md](../05-execution-layer/oms-commands.md)). The PM can:
- Reissue with reduced size or different parameters
- Skip and move to the next command
- Re-evaluate subsequent commands in light of the rejection (e.g., tighter-than-expected capital may make a lower-priority trade infeasible)

**Sequential processing with feedback:** Commands submit one at a time in the PM's chosen priority order. Each command's guardrail validation accounts for the cumulative impact of prior successful commands in the same invocation. If command 2 is rejected, command 3 is validated against state including command 1's impact (but not command 2's, since it was rejected).

**Interaction with envelope model:** The envelope is produced during evaluation (before submission). If a command is rejected and the PM reissues with modified parameters, the envelope is updated with an additional modification record: field changed, original value, revised value, adjustment category `guardrail_rejection_response`, and the specific guardrail that triggered the revision.

**Transport:** The PM submits via the `submit_envelope` tool — input is one finalized envelope, response is one `submission_result` per embedded command (`command_ordinal`, `status`, `command_id`, plus `acknowledgment` on accept or `rejection_payload` on reject). The PM updates the envelope with a `phase: post_rejection` modification and re-invokes `submit_envelope` with the revised envelope. Tool I/O contract: [submit-envelope-tool-schema.md](submit-envelope-tool-schema.md).

---

## Retrieval tools

The PM has two on-demand retrieval tools for verifying narrative claims against underlying records.

### Source-brief retrieval

Same tool as the analyst and strategist — pulls a section of an analysis brief by reference ID. Use when:

- Evaluating whether the analyst's thesis correctly interpreted a signal
- Evaluating whether the strategist's position assessment aligns with the cited source signal
- Investigating counterarguments by checking what the source brief actually said vs. how it was characterized

Reference IDs follow the synthesizer's typed format (`SA-TECH-N`, `QR-N`, `AR-N`, `CR-N` — see [decision-layer overview](README.md)).

### Thesis-component retrieval

Pulls the full component-level thesis record for one held position from the [thesis registry](../01-data-layer/internal/portfolio-state.md#3-thesis-registry). PM context delivers theses at summary level by default — full component-level detail is loaded for the strategist, not the PM (see [portfolio-state.md §3a](../01-data-layer/internal/portfolio-state.md)). Use when:

- Verifying that the strategist's `status_rationale` cites the thesis component it claims to
- Resolving a partial-invalidation evaluation where component-level reasoning matters (see [Partial invalidation](#partial-invalidation))
- Spot-checking a strategist `add_conviction_justification` against the original entry rationale to confirm the "strengthening signal" was genuinely absent at entry

Tool signature: `get_thesis_components(position_id)` returns the full thesis record per [thesis-model.md](../05-execution-layer/thesis-model.md) — summary plus all components (entry rationale, target rationale, invalidation rationale per leg) with key assumptions and linked orders/legs.
