# Feedback-loop skills — shared reference

Referenced from `/feedback-review`, `/feedback-validate`, and `/feedback-retrospective`.
Holds the contracts those three skills share: the review discipline, the headless CLI
surface they drive, the MetricId catalog pointer, the rollback-evidence-protocol pointer,
and the deferred dashboard-affordance vocabulary. The skills cite this file for the shared
parts and carry only their own job-specific flow.

The canonical design is [`docs/design/feedback-loop.md`](../design/feedback-loop.md)
(metric inventory, cadence, confounder list) and
[`docs/design/command-center.md`](../design/command-center.md) (rendering surface, session
API). Cite those for substance — this reference governs how the skills operate against
them, not what they contain.

## Headless operation

The dashboard rendering surface and the review-session shared canvas
([command-center.md § Review sessions](../design/command-center.md#review-sessions),
View group F) are deferred to
[ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686). The skills run **headlessly**
today: they read analytics through the CLI below and emit findings as markdown the operator
reads. Reference no dashboard tool, review-session endpoint, or validation-tracking tool —
those do not exist in the tree yet. The deferred vocabulary is catalogued at the end of this
file so a skill names a capability as *deferred* rather than calling it.

## Headless CLI surface

The analytics-read CLI is the only feedback-loop command surface that exists today
(`src/alphamind/feedback_loop/digest/cli.py`, ALP-888). It opens the SQLite session, loads
the trailing windows, runs the pure digest/metric cores, and emits JSON to stdout. Subcommands:

| Command | Emits | Key options |
|---|---|---|
| `python -m alphamind.feedback_loop.digest.cli digest` | the `WeeklyDigest` for the target week (headline outcomes, process pulse, trajectory, notable shifts) as JSON | `--week <ISO-DATE>` (default: current week), `--trajectory-weeks N` (default 12) |
| `python -m alphamind.feedback_loop.digest.cli metric <metric_id>` | one `MetricResult` (value, posterior band, sample size, insufficient-sample flag) as JSON | `--window WEEKS` (trailing weeks aggregated, default 1), `--week <ISO-DATE>` |
| `python -m alphamind.feedback_loop.digest.cli metrics-list` | the registered metric ids with `po_type` + `default_window` as JSON | — |

Global options: `--config-dir PATH` (config cascade root, default `config/`) and `--db-path
PATH` (explicit SQLite path; omit to use the configured production path). Exit codes: `0`
success, `1` argument / unknown-metric-id error, `2` infrastructure error. Read `--db-path`
against the production DB read-only per the [environment policy](../../CLAUDE.md).

A metric absent from `metrics-list` (e.g. a counterfactual-replay-gated metric before that
engine lands) is degraded-away, not an error — `metric <id>` on an unregistered id exits 1.
Validation registration/evaluation and retrospective-report persistence have **no CLI**
today; those skills work from the design contracts and this analytics surface, and persist
their records once [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686) lands.

## MetricId catalog

Metric ids are the append-only contract `validations.watched_metric_ids` and the digest
reference by value, minted in
`src/alphamind/feedback_loop/validation/records.py` and re-exported through
`alphamind.feedback_loop.metrics.types`. Enumerate the live catalog with `metrics-list` —
it is the source of truth for which ids exist and their process/outcome classification; do
not hard-code an id a skill cannot find there. Each metric's `po_type`, `default_window`,
and supported conditioning slices come from its descriptor (`feedback_loop.metrics.types`);
the inventory's meaning is [feedback-loop.md § Metric inventory](../design/feedback-loop.md#metric-inventory).

## Review discipline

These hold across all three skills; cite this section rather than restating them. Their
rationale is [feedback-loop.md § Process metrics vs. outcome metrics](../design/feedback-loop.md#process-metrics-vs-outcome-metrics)
and [§ Confounder management](../design/feedback-loop.md#confounder-management).

- **Process vs. outcome weighting.** Weight process metrics (high-frequency, low-noise) for
  fast discipline tweaks; weight outcome metrics (low-frequency, high-noise) for high-stakes
  structural changes. State explicitly when a short-window outcome reading is noise. The
  `po_type` on each `MetricResult` tells you which it is.
- **Confounder conditioning.** Before attributing a shift to a cause, condition on the
  canonical confounders — market/volatility regime (the largest), Claude model version,
  concurrent prompt edits, data-source-quality changes, random variance. An unconditioned
  aggregate is a question to investigate, not a finding.
- **Sample-size honesty.** Read the `insufficient_sample` flag and `posterior_band` on every
  outcome reading; when the band does not separate from "no change," say so. A small sample
  is a hint, not a signal.
- **Goodhart framing.** Phrase every metric as a diagnostic of reasoning quality, not a
  scorecard. Discuss what a number suggests, not what it "should be" — a displayed target
  teaches avoidance of the metric's surface rather than the behavior beneath it.
- **No auto-prescription.** Surface evidence; the operator decides what to change. Offer a
  recommendation only when explicitly asked, framed as a hypothesis with its confounder
  caveats and the validation window it would need.

## Rollback evidence protocol

A `/feedback-validate` EVALUATE verdict determines what evidence is sufficient to roll back
an edit; the derivation (`mandatory_clean_failure` / `optional_pending_retrospective` /
`not_applicable`, and the paired post-rollback registration) is mechanical and defined once
at [feedback-loop.md § Rollback evidence protocol](../design/feedback-loop.md#rollback-evidence-protocol).
`optional_pending_retrospective` outcomes surface in the next `/feedback-retrospective`
report's Suggested follow-ups, where the operator's accept/reject lands as a
`decision_type: follow_up` record. Skills cite the protocol section for the rule; they do not
re-derive its table.

## Deferred dashboard affordances (ALP-686)

The shared-canvas surface a skill would otherwise drive is deferred. Name a capability below
as *deferred to [ALP-686](https://linear.app/alphamind-jatassi/issue/ALP-686)* rather than
calling it; the headless flow substitutes markdown the operator reads. The v1 vocabulary,
session-state shape, and lifecycle endpoints are specified at
[command-center.md § Review sessions](../design/command-center.md#review-sessions).

- **Session lifecycle** — `POST /review-sessions`, `GET /review-sessions/{id}/state`,
  `POST /review-sessions/{id}/control`, `DELETE /review-sessions/{id}`.
- **Canvas control** — `highlight_metric`, `highlight_chart_point`, `navigate_to_view`,
  `annotate`, `pin_for_comparison`, and the `clear_highlights` / `clear_annotations` /
  `clear_pins` verbs.
- **Per-view affordances** — `pin_pre_panel` / `pin_post_panel` (validation evaluation view),
  `scroll_to_section` / `highlight_decision_row` (retrospective view).
- **Validation tracking** — `register_validation`, `get_validation`,
  `list_pending_validations`, `submit_validation_outcome`, `list_outcomes_by_artifact`, and
  the `retrospective_reports` / `retrospective_decisions` persistence — all part of the
  deferred View-F substrate.
