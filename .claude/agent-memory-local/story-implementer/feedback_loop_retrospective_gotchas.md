---
name: feedback-loop-retrospective-gotchas
description: Worktree-base staleness check + SQLAlchemy FK flush-ordering trap + where the feedback_loop loader/repository seams live (ALP-131 wave)
metadata:
  type: project
---

Gotchas from implementing ALP-890 (07c retrospective data layer) in the ALP-131
feedback-loop work tree.

## Worktree base can be stale even when the first ancestry check says "contains"
The dispatch's `git merge-base --is-ancestor origin/<integration> HEAD` printed
"ALREADY CONTAINS" on the very first call, but HEAD was actually BEHIND the
integration branch (merge-base was an old `main` commit `0f7a0a14`). The integration
branch had advanced (story 05's `feedback_loop/dataset.py` with `load_window` /
`WindowDataset` landed) after the worktree branched. **Why:** the first check ran
before/around the fetch settling, or the worktree branched off old main.
**How to apply:** after `git fetch`, ALWAYS re-verify with
`git log --oneline HEAD..origin/<integration>` (commits the branch has that you don't)
and `git rev-parse HEAD` vs `origin/<integration>` BEFORE concluding the base is good.
If a documented dependency file (e.g. `dataset.py` / `load_window`) is missing, suspect
a stale base and rebase onto `origin/<integration>`, don't STOP-and-report a "missing
sibling." A duplicate cherry-pick commit on the worktree (a flag_event_types dupe) was
dropped cleanly by `git rebase` as patch-equivalent.

## SQLAlchemy raw `session.add()` rows do NOT auto-order parent-before-child
Inserting a parent (validations) and child (validation_outcomes, or
retrospective_decisions→retrospective_reports/validations) via the repository
`insert_*` helpers (plain `session.add(row)`, no ORM `relationship()`) then a single
`commit()` raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed` — SQLAlchemy
can't infer the FK dependency without a relationship, so it inserts in mapper/insert
order, not topological order. **Fix:** `session.flush()` after inserting the parent
rows, before inserting children that FK to them. This is a test-fixture trap (seeding
data), not a production-code bug.

## feedback_loop seams for the analytics spine (as-built, ALP-131)
- `load_window` + `WindowDataset` live in `feedback_loop/dataset.py` (NOT a `metrics/`
  module, despite the package docstring). It's async; bridges sync repo helpers via
  `AsyncSession.run_sync`.
- `.importlinter` `feedback-loop-metric-cores-no-sqlalchemy` forbids `state.repository`
  imports ONLY from `feedback_loop.metrics`, `citation.parser`, `citation.chain`.
  Other feedback_loop modules (`dataset`, `retrospective/*`) ARE imperative-shell and
  MAY import `state.repository`.
- `state/repository/{validation,retrospective}_queries.py` already legitimately import
  `feedback_loop.{validation,retrospective}.records` (the frozen record types) — so the
  repo tier↔records coupling is sanctioned; new repo read helpers can live there.
