# Story-implementer memory

- [WindowDataset can't be sliced per-week in-memory](windowdataset_no_inmemory_week_slicing.md) — agent_calls + ThesisOutcome drop the per-record timestamp; per-week trajectory needs a dataset/loader change, not a pure generator.
- [feedback-loop retrospective gotchas](feedback_loop_retrospective_gotchas.md) — worktree-base staleness re-check after fetch; SQLAlchemy raw-add FK flush-ordering trap; feedback_loop loader/repository seam locations + import-linter scope
