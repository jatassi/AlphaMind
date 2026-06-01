# 08 — View F — Quality and feedback (placeholder, deferred)

## Status

**Placeholder.** Deferred from [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) v1 per the parent's § Pre-resolved (A). Do not dispatch — re-draft into a proper work tree (under this parent or as a separate Linear parent) once the blockers below resolve.

## What this work tree owns

View group F from [command-center.md](<docs/design/command-center.md>) and the rendering surface for [feedback-loop.md](<docs/design/feedback-loop.md>):

* **Weekly digest** — single scrollable page snapshotted weekly, per [command-center.md § Weekly digest](<docs/design/command-center.md>) and [feedback-loop.md § Dashboard and digest curation](<docs/design/feedback-loop.md>).
* **Validation evaluation view** — side-by-side pre/post comparison for `/feedback-validate` EVALUATE mode, per [command-center.md § Validation evaluation view](<docs/design/command-center.md>).
* **Monthly view** — trajectory counterpart to the weekly digest with calibration band, PM accuracy band, citation-chain panel, anti-pattern detector accuracy, per [command-center.md § Monthly view](<docs/design/command-center.md>).
* **Retrospective view** — renders `retrospective_reports` markdown + joining `retrospective_decisions`, walked through during `/feedback-retrospective` Phase 4, per [command-center.md § Retrospective view](<docs/design/command-center.md>).
* **Ad-hoc query surface** — hybrid form-to-SQL surface with entity catalog, conditioning filters, saved queries, CSV export, per [command-center.md § Ad-hoc query surface](<docs/design/command-center.md>).
* **Review sessions affordances** — `highlight_metric`, `highlight_chart_point`, `navigate_to_view`, `annotate`, `pin_for_comparison`, `clear_*` verbs + per-view affordances (`pin_pre_panel`, `pin_post_panel`, `scroll_to_section`, `highlight_decision_row`), per [command-center.md § Review sessions](<docs/design/command-center.md>).

## New SQL tables this tree adds

* `agent_calls` — per-LLM-call telemetry; consumed by ad-hoc query + per-invocation detail prompt viewer.
* `validations` + `validation_outcomes` — `/feedback-validate` REGISTER + EVALUATE persistence per [state-persistence.md](<docs/design/05-execution-layer/state-persistence.md>).
* `retrospective_reports` + `retrospective_decisions` — `/feedback-retrospective` Phase 3 + 4 persistence.
* `weekly_digest_snapshots` — serialized weekly digest contents at the snapshot boundary.
* `saved_queries` — ad-hoc query persistence per [state-persistence.md § Saved queries](<docs/design/05-execution-layer/state-persistence.md>), originally deferred from [ALP-119](<https://linear.app/alphamind-jatassi/issue/ALP-119>).

All under a new `command_center/feedback/` subpackage that plugs into the FastAPI app + auth + persistence layer + frontend foundation + SSE multiplexer that [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) v1 already shipped.

## Why deferred

Two upstream work trees must land first:

1. **Feedback loop** ([ALP-131](<https://linear.app/alphamind-jatassi/issue/ALP-131>), `_requirements pending_`) — the design substrate for digest curation, validation registration, retrospective generation, citation-chain metrics, anti-pattern detector accuracy. F-group views render its deliverables; drafting them before feedback-loop drafts produces speculative stories.
2. **Counterfactual replay engine** ([ALP-129](<https://linear.app/alphamind-jatassi/issue/ALP-129>), `_stories drafted_`) — the `counterfactual_replays` table the monthly view's PM accuracy panels and the ad-hoc query's replays entity depend on. Stories are drafted but no implementation has landed.

And [ALP-128](https://linear.app/alphamind-jatassi/issue/ALP-128/command-center) v1 itself must land first (the FastAPI app + auth + persistence layer + frontend foundation + SSE multiplexer + view-D config editor framework all get reused).

## Depends on

* [ALP-131](<https://linear.app/alphamind-jatassi/issue/ALP-131>) (Feedback loop) — work tree must be drafted + substantially landed.
* [ALP-129](<https://linear.app/alphamind-jatassi/issue/ALP-129>) (Counterfactual replay engine) — `counterfactual_replays` table must be populated by a running engine.
* [ALP-685](<https://linear.app/alphamind-jatassi/issue/ALP-685>) (07 verify + RUNBOOK + NSSM, this work tree) — command center v1 must be stood up before F-group extends it.

## Action when unblocked

Run `/draft-user-stories Command center — view F follow-on` (or similar) to re-draft this placeholder into a proper work tree. Likely \~10-12 stories: weekly digest, validation evaluation view, monthly view (calibration band + PM accuracy band + citation-chain + anti-pattern detector), retrospective view, ad-hoc query (form + SQL + saved + export), review-session backend + downstream SSE, plus 1-2 stories for the new F-group tables (`agent_calls` likely warrants its own substrate story given it requires a logging callsite in the SDK harness, mirroring how `01a` shipped PROFILE_SWITCHED in this tree).

## Acceptance criteria

- [ ] Do not implement against this placeholder. Re-draft into a proper work tree first.

## Verification

n/a — placeholder.