---
name: feedback-validate
description: Use to register a pre-registered validation for a specific prompt or configuration edit, OR to evaluate a previously-registered validation whose window has elapsed. Triggers on `/feedback-validate` and operator phrases like "I just edited the strategist prompt, let's validate that change", "register a validation for this prompt edit", "did my conviction-criteria edit work?", "check on active validations", "let's evaluate the modification we registered three weeks ago". The skill enforces pre-registration discipline (expected direction, magnitude, window, success/failure criteria captured before post-change data is observed), confounder controls (regime/model-version conditioning), and posterior-band reporting. Confirmation bias is the failure mode this skill exists to defeat. For general performance review use /feedback-review; for open-ended quarterly pattern discovery use /feedback-retrospective.
---

# Feedback validation session

Conduct a focused validation session for a specific prompt or configuration edit. Two modes:

- **REGISTER** — operator just made a change and wants to capture pre-conditions
- **EVALUATE** — a previously-registered validation has reached its window and is ready to assess

Both modes use the [command center dashboard](../../../docs/design/command-center.md) as a shared canvas via the same review-session surface as `/feedback-review`.

The discipline this skill enforces: pre-registration of expected impact, one change per validation, posterior bands rather than point estimates, confounder conditioning, "inconclusive" as a first-class verdict. Prompt-edit validation is the most error-prone of the three feedback-loop jobs; this skill exists to make confirmation bias structurally impossible.

The full design context — including the metric inventory and the canonical confounders — is in [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md). Read it on first invocation.

This skill assumes the AlphaMind dashboard exposes review-session tools and validation-tracking tools (registration, lookup, outcome recording) in addition to those used by `/feedback-review`, plus the Chrome browser automation tools for opening the dashboard URL.

---

## 1. Mode selection

If the operator's intent is unclear from their opening message, ask: *"Are we registering a new validation for an edit you just made, or evaluating one you registered earlier?"*

When the operator's intent is "I just made an edit," call `list_pending_validations()` (lists validations entities per [state-persistence.md § Validations](../../../docs/design/05-execution-layer/state-persistence.md)) first as a sanity check — if a previously-registered validation is near its evaluation window's end, surface it: *"You have a strategist-prompt validation due tomorrow; want to handle that first?"*

---

## 2. REGISTER mode

The pre-registration is the whole point. Treat it as a contract: nothing the operator says after the post-change data lands can change what was agreed here.

### Required fields

Walk the operator through capturing each:

| Field | What it means | How to elicit |
|---|---|---|
| Edited artifact | Which prompt or config file was changed | Ask, then confirm against the git diff if uncertain |
| Pre-edit version | Git SHA of the pre-edit version | From the operator, or derived from the prompt's git history |
| Post-edit version | Git SHA of the post-edit version | Current HEAD if the change just shipped |
| Watched metric(s) | Which metric(s) will signal whether the edit worked | From the [metric inventory](../../../docs/design/feedback-loop.md); typically one process metric for fast feedback or one outcome metric for slow feedback |
| Window length | How long until evaluation is meaningful | Suggest based on metric tier: process metrics need 2–4 weeks; outcome metrics need 6–12 weeks. Operator confirms or overrides. |
| Expected direction | improved / unchanged / degraded | Operator names. Most edits expect "improved." |
| Expected magnitude | How much change is meaningful | Qualitative ("slight", "noticeable") or quantitative ("PM rejection rate down 5–10pp") — operator's call |
| Success criterion | What counts as "this edit worked" | Concrete: "PM rejection rate on conviction-4 trades drops below 30% over the window" |
| Failure criterion | What counts as "this edit didn't work" or "made things worse" | Concrete: "PM rejection rate stays above 35% or anti-pattern frequency for `conviction_inflation` increases" |

### Anti-patterns in registration

- **Vague success criteria.** "Things should look better" is not a criterion. Push back until concrete.
- **Multiple concurrent edits in one validation.** If the operator made three changes, register three validations or hold the others until one is evaluated. One change per validation is a hard rule — without it, attribution is impossible.
- **Watching the wrong tier.** Outcome metrics over short windows give noise, not signal. If the operator wants a 1-week window on conviction calibration, suggest a process metric instead, or stretch the window.
- **Missing failure criterion.** If the operator can't say what would count as failure, the registration isn't actionable. Push for it explicitly.

### Confirmation

When all fields are captured, call `register_validation(...)` (writes a validation entity per [state-persistence.md § Validations](../../../docs/design/05-execution-layer/state-persistence.md)) and confirm the projected evaluation date to the operator. The validation is now active and will surface in `list_pending_validations()` when the window elapses.

---

## 3. EVALUATE mode

Triggered when the operator wants to check a registered validation whose window has elapsed (or any time the operator is curious — but warn if the window hasn't elapsed and it's too early to evaluate).

### Setup

Call `get_validation(validation_id)` (reads the validation entity per [state-persistence.md § Validations](../../../docs/design/05-execution-layer/state-persistence.md)) to load the registration. Call `list_pending_validations()` (lists validations entities per [state-persistence.md § Validations](../../../docs/design/05-execution-layer/state-persistence.md)) if the operator hasn't named a specific one — surface the candidates and let them pick.

Open a review session via `create_review_session()` (backed by `POST /review-sessions`) and `mcp__claude-in-chrome__tabs_create_mcp(url=dashboard_url)` as in `/feedback-review`. The dashboard navigates to a dedicated validation view that shows pre/post side by side for the registered metric.

### The evaluation walk

In strict order, do not deviate:

1. **Check for supersession.** If `get_validation` returned a non-null `superseded_at`, the EVALUATE walk does not run. The validation evaluation view shows the supersession banner (reason + triggering shift + re-register affordance) per [command-center.md § Validation evaluation view](../../../docs/design/command-center.md#validation-evaluation-view). Confirm to the operator: "This validation was superseded by {reason} on {timestamp} — its window is structurally unreadable. Want to re-register a fresh validation against the post-shift context?" If yes, hand off to REGISTER seeded with the prior registration's edited artifact, watched metrics, expected direction, expected magnitude, and success/failure criteria; the operator confirms or revises before submitting. Either way, do not submit a validation outcome — superseded validations do not produce outcome records per [state-persistence.md § Validations](../../../docs/design/05-execution-layer/state-persistence.md).
2. **State the registration verbatim.** Read out the pre-registered fields. The operator hears their own past words. This is the anti-rationalization anchor.
3. **Show pre-edit window data.** Highlight the metric over the pre-edit window. State the value with a posterior band.
4. **Show post-edit window data.** Highlight the same metric over the post-edit window. State the value with a posterior band.
5. **Compute the comparison.** Conditioned on regime (the active regime distribution in each window must be similar enough to compare; if not, flag), conditioned on Claude model version (any model update straddling the windows breaks attribution; if so, flag), and conditioned on whether other concurrent prompt edits landed in the window (if so, flag). Confounders flagged here are residual — anything structural enough to break the contract would have triggered supersession in step 1.
6. **Surface confounder issues.** If any confounder is materially different across the windows, the right verdict is `inconclusive`. Say so.
7. **State the verdict.** One of:
   - `improved` — post-window meets the success criterion with confidence
   - `degraded` — post-window meets the failure criterion with confidence
   - `no_change` — post-window distinguishable from neither success nor failure; falls in the middle
   - `inconclusive` — sample size insufficient or confounders prevent attribution
8. **Capture the outcome.** Call `submit_validation_outcome(validation_id, verdict, posterior_summary, narrative)` (writes a validation outcome entity per [state-persistence.md § Validation outcomes](../../../docs/design/05-execution-layer/state-persistence.md)). The validation is now closed; the record is permanent.

### Anti-patterns in evaluation

- **Rationalizing toward the pre-registered hope.** "The number didn't quite hit 30%, but it's trending down" is the failure mode this skill exists to prevent. The criterion was the criterion.
- **Skipping the verbatim re-read.** It feels redundant. It is not — it anchors the evaluation against drift.
- **Stretching the window.** If the window is up and the data is inconclusive, the verdict is `inconclusive`. Extending the window requires explicit operator direction with a stated reason (typically a confounder that argues for waiting), not silent slippage.
- **Confounder hand-waving.** "There was a regime change but the numbers still look better" — regime change means the comparison is contaminated. Flag and resolve before evaluating.

---

## 4. Discipline you maintain throughout (both modes)

These echo `/feedback-review` but apply with greater stringency since validation is the highest-leverage anti-bias surface.

**Posterior bands, not point estimates.** Every metric reading reported with explicit uncertainty. When the band straddles "no change," the verdict is `no_change` or `inconclusive`, not "trending toward improved."

**One change per validation.** If two concurrent edits make attribution impossible, register two separate validations (or wait until one resolves before changing the other).

**Confounder conditioning is mandatory.** Regime, Claude model version, other concurrent prompt edits. The dashboard surfaces these via the [provenance fields](../../../docs/design/05-execution-layer/state-persistence.md) on each invocation; use them.

**Inconclusive is a first-class verdict.** Better to say "we don't know" than to fake confidence. Inconclusive validations can be re-registered with a longer window or different conditions.

**Criteria are frozen at registration.** The success and failure criteria set at registration time stand at evaluation time. The skill enforces this by reading the registration verbatim before showing data.

---

## 5. Using the dashboard affordances

Same affordances as `/feedback-review` (highlight, navigate, annotate, pin, batched calls). One session-mode addition: the validation evaluation view shows pre/post panels side by side and exposes view-specific affordances `pin_pre_panel(metric_id)` and `pin_post_panel(metric_id)` (per [command-center.md § Validation evaluation view](../../../docs/design/command-center.md#validation-evaluation-view)) — pin the relevant pre and post panels so they stay visible across navigation when the operator wants to drill into a related metric.

Chrome tools beyond opening the tab: routine state reads via `get_session_state`; visual fallback via `mcp__claude-in-chrome__read_page` only when the structured state misses something specific (a chart's actual rendering, a pre/post panel's exact label).

---

## 6. Cross-references

- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — full metric inventory, confounder list, validation methodology
- [`docs/design/command-center.md § Review sessions`](../../../docs/design/command-center.md#review-sessions) — session API surface
- [`docs/design/05-execution-layer/state-persistence.md`](../../../docs/design/05-execution-layer/state-persistence.md) — provenance fields (regime, model version, prompt versions) the conditioning relies on
- [`feedback-review`](../feedback-review/SKILL.md) — sibling skill for general performance review
- [`feedback-retrospective`](../feedback-retrospective/SKILL.md) — sibling skill for open-ended quarterly review
