---
name: feedback-review
description: Use to conduct an interactive AlphaMind feedback review session with the operator over the AlphaMind command center dashboard — weekly process-metric review, monthly outcome-metric review, or ad-hoc deep-dive into specific agents, metrics, or behaviors. Triggers on `/feedback-review` and operator phrases like "review this week", "let's review last month's performance", "walk me through the dashboard", or any operator request to look at AlphaMind agent calibration trends, PM rejection patterns, anti-pattern frequencies, citation chain metrics, conviction or status calibration, or system trajectory month-over-month. The session uses the dashboard as a shared canvas: you highlight metrics, navigate views, annotate chart points, and pin items for comparison via dashboard control tools while the operator drives the dashboard themselves and responds verbally. Not for live operational health (the command center handles that directly), not for evaluating a specific prompt edit's pre-registered impact (a sibling skill handles that).
---

# Feedback review session

Conduct an interactive review session in which you and the operator walk through AlphaMind's recent performance over the [command center dashboard](../../../docs/design/command-center.md), using the dashboard as a shared canvas. You highlight, navigate, annotate, and pin via tool calls; the operator drives the dashboard themselves and responds verbally. Your job is to surface what's worth attention, apply discipline so weak signals aren't overread, and help the operator decide what (if anything) needs adjustment.

The full design context — including the metric inventory across all layers — is in [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md). Read it on first invocation. The structure of a review depends on knowing what's available to look at and what discipline applies.

This skill assumes two things about the environment:

- The AlphaMind dashboard exposes review-session tools (session create/end, state read, control write) corresponding to the API surface in [command-center.md § Review sessions](../../../docs/design/command-center.md#review-sessions).
- Chrome browser automation tools (`mcp__claude-in-chrome__*`) are available, used for opening the dashboard URL on session start and as a visual fallback when the structured state API does not capture something you need to see.

---

## 1. Session lifecycle

The mechanics around the dashboard. Brief, then move on to the substance.

1. **Open.**
   - Call `create_review_session()` (backed by `POST /review-sessions`) to obtain `session_id` and `dashboard_url`.
   - Open the URL directly in the operator's Chrome via `mcp__claude-in-chrome__tabs_create_mcp(url=dashboard_url)`.
   - Confirm the tab opened; if it failed (chrome MCP unavailable, permission denied), fall back to handing the operator the URL.
2. **Each turn.** Call `get_session_state(session_id)` (backed by `GET /review-sessions/{id}/state`) at the start of the turn. The returned state tells you the operator's current view, what they have selected, and recent navigation. Inject this into your reasoning — it is your equivalent of seeing what the operator is looking at right now.
3. **End.** When the operator signals end (explicit "we're done", or a natural close), call `end_review_session(session_id)` (backed by `DELETE /review-sessions/{id}`). The session is transient by design; once ended it is gone. Do not auto-close the operator's tab — leave that to them in case they want to keep looking.

The operator drives the dashboard at their own pace. You do not react to every interaction — you read state at turn boundaries and respond to what the operator says.

### When to reach for chrome tools beyond opening the tab

The structured state API (`get_session_state`) is your default surface for understanding what the operator sees. It is cheap, deterministic, and does not prompt for permission per call. Chrome tools have a place but a narrow one:

- **`mcp__claude-in-chrome__read_page`** — use when the structured state does not capture what you need: a chart's rendered details, the actual text of an annotation, a layout anomaly the operator is asking about. Costs a permission prompt; do not use routinely.
- **`mcp__claude-in-chrome__computer` (screenshot)** — use when verbal description is failing and you need to see what the operator sees. Reserve for genuine grounding gaps, not as a default.
- **`mcp__claude-in-chrome__navigate` / JavaScript injection / direct DOM clicks** — do *not* use these to manipulate the dashboard. The semantic control API (`highlight_metric`, `navigate_to_view`, `annotate`, `pin_for_comparison`) is the right primitive — it persists in session state, survives layout changes, and stays consistent with the operator's view of what is happening. Chrome-level manipulation would bypass that surface and create state Claude wrote that the session does not know about.

---

## 2. The flow of a review

Three rough cadences. If the operator's intent is unclear from their opening message, ask before opening the dashboard: "this week's process pulse, last month's outcome calibration, or something specific you want to drill into?"

### Weekly review (default)

Starting view: the **Weekly digest**. It already curates the right surface for a weekly cadence. Walk top-to-bottom, surfacing what's worth attention:

- **Notable shifts first.** Anything the dashboard flagged as having moved this week — anti-pattern spike, regime change, sector underperformance, validation reaching its window. Highest-leverage because it's a *change*. Highlight the flagged items and ask the operator if they had already noticed.
- **Process pulse second.** This week's PM rejection rate, anti-pattern frequencies by name, modification rate, guardrail rejection count — each with week-over-week delta. Process metrics move enough week-over-week to be diagnostic. Flag the deltas that look meaningful and discuss what they suggest.
- **Trajectory third.** Sparklines over 8–12 weeks for P/L, win rate, cost per resolved thesis, conviction calibration with posterior bands, status calibration with posterior bands. Ask whether the trajectory matches the operator's expectations.
- **Validation status if active.** Active prompt edits in their validation windows, with pre-registered expected impact. If any are nearing the window's end, flag them — the actual go/no-go decision belongs in the validation skill, but surfacing readiness is fair game here.
- **Headline outcomes last.** P/L, win rate, drawdown — noisy at the weekly horizon. Discuss briefly; do not over-weight.

### Monthly review

Starting view: the **Monthly view**. Outcome-tier metrics dominate. Conditioning slices (regime, sector, conviction band, prompt version, model version) are the primary affordances.

Lead with:

- **Citation chain metrics.** Per-source signal survival rate, synthesizer recall, decision-layer citation rate per source. These directly surface "is this analysis-layer agent producing predictive content?" and "is the synthesizer dropping signal?" — the highest-leverage diagnostics for analysis-layer prompt revisions.
- **Conviction calibration** for the analyst, **status calibration** for the strategist. Both with posterior bands. Both need months of data — flag insufficient sample as such.
- **Anti-pattern detector accuracy.** For each canonical anti-pattern (`sunk_cost_persistence`, `rationalized_continuation`, `thesis_contradiction_suppression`, `engine_originated_closure_signal`, `conviction_inflation`), check the forward-outcome distribution of tagged positions. A detector firing on noise is a signal that the PM prompt's pattern definition needs revisiting.
- **PM rejection accuracy** from the counterfactual replay engine — when PM rejected a proposal, what would the trade have done? Same for modifications.

### Ad-hoc deep-dive

The operator brings a specific question — "why did energy underperform this month?", "is the strategist holding too many at-risk positions?", "is the synthesizer down-weighting tech-semis findings?". Read the question, navigate to the relevant view, walk through the data with them. The discipline below still applies.

---

## 3. Discipline you maintain throughout

These are not optional. The whole point of the review is to make good decisions about what to change; failing on these makes the review actively misleading.

**Process vs. outcome distinction.** Process metrics (rejection rates, anti-pattern frequencies, criterion pass/fail) move fast and are low-noise — week-over-week deltas are real signal. Outcome metrics (calibration curves, P/L, validation rates) move slowly and are noisy — short-window deltas are mostly noise. Weight them differently in the conversation. When discussing an outcome metric over a short window, explicitly say it is noisy.

**Confounder conditioning.** Every behavior shift has multiple plausible causes:

- Market or volatility regime shift — the single biggest confounder
- Anthropic model update — Claude can drift in behavior between releases without our prompts changing
- Other concurrent prompt edits
- Data source quality changes
- Random variance

Before claiming "X changed because Y," check the conditioning. The dashboard surfaces regime-conditioned and prompt-version-conditioned views — use them. If the data isn't conditioned and the unconditioned aggregate looks suspicious, the right response is "this might be regime-driven; let me check the regime-conditioned view" — not "X improved."

**Sample-size honesty.** Outcome metrics need real resolved-thesis volume. If a metric's posterior band is wide enough that the change isn't distinguishable from noise, say so. The dashboard surfaces this with "needs N more observations" copy when below threshold; echo that explicitly. Do not pretend a 3-trade sample tells you anything about conviction calibration.

**Goodhart awareness.** Phrase metrics as *diagnostics*, not *scorecards*. The strategist's anti-pattern frequency is a diagnostic of strategist reasoning quality; making it the optimization target teaches the strategist to avoid the *language* of the pattern rather than the underlying behavior. Talk about what a metric *suggests*, not what its number "should be."

**No auto-prescription.** You surface evidence; the operator decides what to change. If the operator explicitly asks "what would you do here?", you can offer a recommendation — but with the relevant caveats (the change is a hypothesis, the validation window matters, the confounder controls). Do not push changes the operator hasn't invited.

---

## 4. Using the dashboard affordances

The dashboard is your shared canvas, not a passive backdrop. Use the affordances actively to ground the conversation.

**`highlight_metric` and `highlight_chart_point`** — Use whenever you discuss a specific metric or data point. The operator should never have to mentally search the dashboard for what you are talking about. Highlight first, talk second.

**`navigate_to_view`** — Use when transitioning topics that live on different views. Do not make the operator click through; navigate them. State what you are navigating to and why before the navigation happens, not after.

**`annotate`** — Use when leaving a note that captures *why* a chart point or metric is notable. Annotations persist for the session and can be referenced later ("remember the annotation we left on the synthesizer recall chart?").

**`pin_for_comparison`** — Use when comparing two or more items that live on different views or in different time windows. Pinning keeps them visible across navigation.

**`clear_highlights`, `clear_annotations`, `clear_pins`** — Use sparingly. Highlights and annotations from earlier in the session form a useful trail; clear only when they are actively cluttering or the operator asks for a clean slate.

**Batch where possible.** The control endpoint accepts batched actions in a single call. When highlighting three related metrics, send them as one batched call rather than three sequential ones.

---

## 5. Anti-patterns specific to running a review

The discipline above is the *what*. The following are the *how* — failure modes specific to running a review session as the conducting agent.

**Talking about a metric without highlighting it.** Forces the operator to mentally search the page. Highlight first, talk second.

**Reading short-window outcome metrics as signal.** "Win rate dropped from 62% to 58% this week" over 12 trades is variance, not change. Treat it as such or do not bring it up.

**Unconditioned aggregate as evidence.** "Conviction-4 trades performed worse this month" without checking whether the regime shifted, the analyst prompt changed, or sample size is sufficient. The dashboard's conditioning slices exist exactly for this.

**Auto-prescription.** "You should tighten the strategist's at-risk criteria." Surface the evidence; let the operator decide.

**Pretending to know.** If the data is not there to support a claim, say so. Especially for citation chain metrics on agents with little resolved-thesis volume yet, "we do not have enough resolved theses citing this source to score signal survival rate yet" is the correct answer.

**Goodhart-style framing.** "Anti-pattern frequency for `sunk_cost_persistence` should be lower." The frequency is a diagnostic; aiming to lower it directly teaches avoidance of the language rather than the behavior. Discuss what the frequency suggests, not what it should be.

**Walls of text.** The operator is reading the dashboard, not your prose. Keep verbal commentary tight; the dashboard carries most of the visual weight. One sentence per metric you highlight is usually enough.

**Silent navigation.** State what you are navigating to and why *before* the navigation, not after.

---

## 6. Cross-references

- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — full metric inventory across data, distillation, analysis, decision, and execution layers; the canonical source for what is measurable
- [`docs/design/command-center.md § Review sessions`](../../../docs/design/command-center.md#review-sessions) — session API surface (lifecycle, control endpoint, state shape, affordance vocabulary)
- [`docs/design/05-execution-layer/state-persistence.md`](../../../docs/design/05-execution-layer/state-persistence.md) — the underlying data substrate (activity log, agent calls, counterfactual replays, thesis records, provenance fields)
- [`docs/design/05-execution-layer/counterfactual-replay-engine.md`](../../../docs/design/05-execution-layer/counterfactual-replay-engine.md) — engine producing the data behind PM rejection accuracy and modification effectiveness
- [`docs/design/04-decision-layer/portfolio-manager.md`](../../../docs/design/04-decision-layer/portfolio-manager.md) — defines the canonical anti-pattern strings and the PM evaluation criteria the review surfaces
