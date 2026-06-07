# feedback_loop/ — month-over-month system improvement

The headless analytics spine (Shape B). Pairs the system's reasoning artifacts
(analyst conviction, strategist status, PM verdicts, synthesizer citations) with the
outcomes they produced (thesis resolutions, realized P/L) and computes deterministic,
conditionable metrics over that pairing. Design intent (point-in-time, not code-truth):
`docs/design/feedback-loop.md`.

## Module map

| Subpackage | Concerns | Filled by (ALP-131 story) |
|---|---|---|
| `metrics/` | Deterministic metric-computation library + WindowDataset loader | 05, 06a–06e |
| `citation/` | Reference-ID / citation-chain analysis | 06d |
| `digest/` | Weekly-digest generation | 07a, 08a |
| `validation/` | Confounder-managed validation-discipline logic | 07b, 08b |
| `retrospective/` | Retrospective (periodic LLM-driven review) data layer | 07c |

The operator-tunable sample-size thresholds live in `config/feedback.yaml`
(`alphamind.config.models.feedback.FeedbackLoopConfig`); windows and posterior bands are
definitional, and notable-shift thresholds live in `config/digest.yaml`.

## Key invariants

- **Read-only over trading state.** The feedback loop measures and proposes; it never
  mutates live trading-state records and must not import `alphamind.execution` (enforced
  by the `feedback_loop-no-execution` `.importlinter` contract). The thesis-resolution
  authoring path that closes the ALP-834 gap lives in `analysis/thesis_resolution/` + a
  `run_invocation` step — not here.
- **Functional core / imperative shell.** Metric `compute()` cores are pure; DB I/O lives
  in the loader shell.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
