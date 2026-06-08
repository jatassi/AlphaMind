---
name: feedback-validate
description: Use to register a pre-registered validation for a specific prompt or configuration edit, OR to evaluate a previously-registered validation whose window has elapsed. Triggers on `/feedback-validate` and operator phrases like "I just edited the strategist prompt, let's validate that change", "register a validation for this prompt edit", "did my conviction-criteria edit work?", "check on active validations", "let's evaluate the modification we registered three weeks ago". The skill enforces pre-registration discipline (expected direction, magnitude, window, success/failure criteria captured before post-change data is observed), confounder controls (regime/model-version conditioning), and posterior-band reporting. Confirmation bias is the failure mode this skill exists to defeat. For general performance review use /feedback-review; for open-ended quarterly pattern discovery use /feedback-retrospective.
---

# Feedback validation session

Conduct a focused validation session for a specific prompt or configuration edit. Two modes:

- **REGISTER** — the operator just made a change and wants to capture pre-conditions
- **EVALUATE** — a previously-registered validation has reached its window and is ready to assess

Read [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md)
first — it defines the headless CLI surface this skill drives, the MetricId catalog pointer,
the review discipline, the rollback-evidence-protocol pointer, and the dashboard affordances
deferred to [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686). The metric
inventory, the canonical confounders, and the validation methodology live in
[`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md); read it on first
invocation.

This skill runs headlessly: it drives the validation CLI
(`src/alphamind/feedback_loop/validation/cli.py`), reads the JSON it emits, and states the
pre/post readings and the verdict in chat. The command-center validation-evaluation view —
side-by-side pre/post panels and their `pin_pre_panel` / `pin_post_panel` affordances — is
deferred to [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686); when it lands, the
same registrations and outcomes render there.

Prompt-edit validation is the most error-prone of the three feedback-loop jobs. Pre-registering
the expected impact, holding to one change per validation, reporting posterior bands rather than
point estimates, conditioning on confounders, and treating "inconclusive" as a first-class
verdict are what make confirmation bias structurally impossible here. The
[shared review discipline](../../../docs/agents/feedback-loop-skills.md#review-discipline)
applies with greater stringency because validation is the highest-leverage anti-bias surface.

---

## 1. Mode selection

If the operator's intent is unclear from their opening message, ask: *"Are we registering a new
validation for an edit you just made, or evaluating one you registered earlier?"*

When the operator's intent is "I just made an edit," run `list` first as a sanity check — if a
previously-registered validation is near its window's end, surface it: *"You have a
strategist-prompt validation due tomorrow; want to handle that first?"*

```
python -m alphamind.feedback_loop.validation.cli list
```

Emits `{"pending": [...]}` — the registered, unevaluated, un-superseded validations with their
`evaluation_due_at`. Global option `--db-path PATH` selects the SQLite path (omit for the
configured production DB; read it read-only per the [environment policy](../../../CLAUDE.md)).

---

## 2. REGISTER mode

The pre-registration is the whole point. Treat it as a contract: nothing the operator says after
the post-change data lands can change what was agreed here.

REGISTER also accepts seeded fields when invoked from EVALUATE step 7 to register a paired
post-rollback validation following a `mandatory_clean_failure` outcome. Seeded values are a
starting point, not a constraint — walk the operator through confirming or adjusting each seeded
field with the same discipline as a fresh registration.

### Required fields

Walk the operator through capturing each; these are the keys of the `register` payload:

| Field | Payload key | What it means | How to elicit |
|---|---|---|---|
| Edited artifact | `edited_artifact` | Which prompt or config file was changed | Ask, then confirm against the git diff if uncertain |
| Pre-edit version | `pre_edit_version` | Git SHA before the edit | From the operator, or from the prompt's git history |
| Post-edit version | `post_edit_version` | Git SHA after the edit | Current HEAD if the change just shipped |
| Watched metric(s) | `watched_metric_ids` | Which metric(s) signal whether the edit worked | From the live `metrics-list` catalog (see below); typically one process metric for fast feedback or one outcome metric for slow feedback |
| Window length | `window_length_days` | How long until evaluation is meaningful | Suggest by tier: process metrics 2–4 weeks, outcome metrics 6–12 weeks. Operator confirms or overrides. |
| Expected direction | `expected_direction` | `improved` / `unchanged` / `degraded` | Operator names. Most edits expect `improved`. |
| Expected magnitude | `expected_magnitude` | How much change is meaningful | Qualitative ("slight", "noticeable") or quantitative ("PM rejection rate down 5–10pp") — operator's call |
| Success criterion | `success_criterion` | What counts as "this edit worked" | Concrete: "PM rejection rate on conviction-4 trades drops below 30% over the window" |
| Failure criterion | `failure_criterion` | What counts as "didn't work" or "made things worse" | Concrete: "PM rejection rate stays above 35% or `conviction_inflation` frequency increases" |

The payload also carries `validation_id` (a fresh id you mint), `registered_at` (a tz-aware
ISO-8601 timestamp), `registered_regime`, and `registered_model_id` (the regime and Claude model
active at registration — the conditioning context the supersession detector compares against).

The `watched_metric_ids` are the append-only contract; enumerate the live catalog rather than
hard-coding an id:

```
python -m alphamind.feedback_loop.digest.cli metrics-list
```

A metric absent from `metrics-list` (e.g. a counterfactual-replay-gated metric before that engine
lands) is not yet available — pick a registered one, or stretch the window to a metric that is.

### Anti-patterns in registration

- **Vague success criteria.** "Things should look better" is not a criterion. Push back until concrete.
- **Multiple concurrent edits in one validation.** If the operator made three changes, register three validations or hold the others until one is evaluated. One change per validation is a hard rule — without it, attribution is impossible.
- **Watching the wrong tier.** Outcome metrics over short windows give noise, not signal. If the operator wants a 1-week window on conviction calibration, suggest a process metric instead, or stretch the window.
- **Missing failure criterion.** If the operator can't say what would count as failure, the registration isn't actionable. Push for it explicitly.

### Regime-conditioned evidence for `config/distillation.yaml` edits

When the edited artifact is `config/distillation.yaml`, surface the
[distillation replay harness](../../../docs/design/02-distillation-layer/replay-harness.md) as a
recommended evidence step before capturing expected direction and magnitude. The harness re-runs
the deterministic layer against archived regime-stratified inputs under the candidate config and
emits a per-regime flag-rate report at `data/replay_reports/{report_id}/report.md`; cite the
`report_id` in the registration's `expected_magnitude` or `success_criterion` so the
pre-registered expectation's regime grounding is verbatim-readable at EVALUATE time.

### Confirmation

When all fields are captured, register the validation and confirm the projected evaluation date
to the operator:

```
python -m alphamind.feedback_loop.validation.cli register --input - <<'JSON'
{ ...the payload above... }
JSON
```

Emits `{"validation_id": ...}`. The validation is now active and will surface in `list` until its
window elapses. Exit code `0` on success; `1` on a malformed payload (the error names the field).

---

## 3. EVALUATE mode

Triggered when the operator wants to assess a registered validation whose window has elapsed (or
any time the operator is curious — but warn if the window hasn't elapsed and it's too early to
read).

### The evaluation walk

The CLI does the mechanical work; your job is to surface the operator's own pre-registered contract,
supply the two judgments the metrics can't yield mechanically, and report the result honestly. In
strict order:

1. **Refresh supersession, then evaluate.** A registered validation is a contract over a specific
   conditioning context; a material mid-window shift in that context breaks it structurally. Run
   the detector, then evaluate:

   ```
   python -m alphamind.feedback_loop.validation.cli detect-supersessions
   python -m alphamind.feedback_loop.validation.cli evaluate \
     --validation-id <id> --outcome-id <fresh-id> --evaluated-at <ISO-8601> --input -
   ```

   `evaluate` short-circuits on supersession: if the validation was marked superseded it writes no
   outcome and emits `{"superseded": true, "superseded_reason": ..., "outcome": null}`. The
   `superseded_reason` is one of `regime_transition`, `model_version_change`, or
   `concurrent_edit_on_watched_artifact` — the first-firing trigger over the post-edit window, per
   [feedback-loop.md § Mid-window supersession](../../../docs/design/feedback-loop.md#mid-window-supersession).
   Confirm to the operator: *"This validation was superseded by {reason} — its window is
   structurally unreadable. Want to re-register a fresh validation against the post-shift
   context?"* If yes, hand off to REGISTER seeded with the prior registration's edited artifact,
   watched metrics, expected direction, magnitude, and criteria; the operator confirms or revises
   before submitting. A superseded validation produces no outcome record — stop the walk here.

2. **State the registration verbatim.** Read out the pre-registered fields (from the `list` entry or
   the operator's record). The operator hears their own past words. This is the anti-rationalization
   anchor.

3. **Supply the two judgments.** The `evaluate` `--input` payload carries the determinations the
   free-text criteria can't yield mechanically, plus the `narrative`:
   - `confounder_flagged` — whether a residual confounder (regime-distribution mismatch, a Claude
     model-version straddle, or a concurrent edit) contaminates the pre/post comparison. Confounders
     flagged here are residual: anything structural enough to break the contract would already have
     triggered supersession in step 1. A flagged confounder forces the verdict to `inconclusive` and
     downgrades any rollback obligation.
   - `failure_criterion_crossed` — whether the post-edit window crossed the pre-registered failure
     criterion (the operator's own contract firing).
   - `confounder_notes` / `narrative` — free-text detail persisted on the outcome.

   Do not invent a confounder to escape a rollback obligation. The confounder is a property of the
   comparison, not of reluctance to roll back.

4. **Read the verdict and rollback status.** The CLI computes the pre/post metric readings, derives
   the verdict and rollback status, and writes the outcome; the emitted `outcome` carries both plus
   the per-metric `posterior_summary`. The verdict is one of `improved`, `degraded`, `no_change`, or
   `inconclusive`. State it to the operator with the posterior bands from `posterior_summary` — when
   `insufficient_sample` is true on a watched metric, or a band straddles "no change," the reading is
   not-yet-actionable, and the verdict will be `inconclusive`.

5. **Relay the rollback status.** Per the
   [rollback evidence protocol](../../../docs/agents/feedback-loop-skills.md#rollback-evidence-protocol),
   the emitted `rollback_status` is one of `mandatory_clean_failure`,
   `optional_pending_retrospective`, or `not_applicable`. The derivation is mechanical — state the
   status and that it follows from the verdict, the failure-criterion judgment, and the confounder
   judgment; introduce no new judgment here.

6. **Confirm the outcome is written.** A non-superseded `evaluate` has already persisted the outcome
   (the emitted `outcome.outcome_id`). The validation is now closed; the record is permanent.

7. **Handle the rollback status.**
   - If `mandatory_clean_failure`: register a paired post-rollback validation in the same session via
     a seeded `register` call — `edited_artifact`, `watched_metric_ids`, and `window_length_days`
     from the failed validation; `expected_direction = improved`; `success_criterion` "metric returns
     to within the posterior band of the pre-failed-edit baseline"; `failure_criterion` "metric stays
     at or worsens from the failed-edit post-edit window value." The `pre_edit_version` defaults to
     the failed edit's `post_edit_version`; the `post_edit_version` is the revert commit (typically
     HEAD after `git revert`). Walk the operator through confirming or adjusting the seeded fields
     before committing.
   - If `optional_pending_retrospective`: this entry surfaces in the next
     [`/feedback-retrospective`](../feedback-retrospective/SKILL.md) report's Suggested follow-ups,
     where the operator's accept/reject lands as a `decision_type: follow_up` record. No further
     action this session.
   - If `not_applicable`: state so explicitly and end the EVALUATE walk.

### Anti-patterns in evaluation

- **Rationalizing toward the pre-registered hope.** "The number didn't quite hit 30%, but it's trending down" is the failure mode this skill exists to prevent. The criterion was the criterion.
- **Skipping the verbatim re-read.** It feels redundant. It is not — it anchors the evaluation against drift.
- **Stretching the window.** If the window is up and the data is inconclusive, the verdict is `inconclusive`. Extending the window requires explicit operator direction with a stated reason (typically a confounder that argues for waiting), not silent slippage.
- **Confounder hand-waving.** "There was a regime change but the numbers still look better" — regime change means the comparison is contaminated. Set `confounder_flagged` and let the verdict fall to `inconclusive`.
- **Inventing a confounder to escape `mandatory_clean_failure`.** The rollback derivation is mechanical: a flagged confounder downgrades the obligation. A confounder is a property of the comparison, not of reluctance to roll back.

---

## 4. Discipline you maintain throughout (both modes)

Apply the shared
[review discipline](../../../docs/agents/feedback-loop-skills.md#review-discipline) —
process-vs-outcome weighting, confounder conditioning, sample-size honesty, Goodhart framing, no
auto-prescription — with the greater stringency validation demands. Three applications worth
stating concretely:

- **Posterior bands, not point estimates.** Report every metric reading from `posterior_summary` with its band. When the band straddles "no change," the verdict is `no_change` or `inconclusive`, never "trending toward improved."
- **One change per validation.** If two concurrent edits make attribution impossible, register two separate validations — or wait until one resolves before changing the other.
- **Criteria are frozen at registration.** The success and failure criteria set at registration time stand at evaluation time. The verbatim re-read in EVALUATE step 2 enforces this.

---

## 5. Cross-references

- [`docs/agents/feedback-loop-skills.md`](../../../docs/agents/feedback-loop-skills.md) — shared reference: headless CLI surface, MetricId catalog pointer, review discipline, rollback-evidence-protocol pointer, deferred dashboard affordances
- [`docs/design/feedback-loop.md`](../../../docs/design/feedback-loop.md) — metric inventory, confounder list, mid-window supersession, rollback evidence protocol
- [`feedback-review`](../feedback-review/SKILL.md) — sibling skill for general performance review
- [`feedback-retrospective`](../feedback-retrospective/SKILL.md) — sibling skill for open-ended quarterly review
