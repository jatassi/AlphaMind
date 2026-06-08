# feedback_loop/ — month-over-month system improvement

Analysis/operational. Turns accumulated history into improvement: `discovery/` (find
candidate signals/patterns), `analytics/` (metric inventory across layers),
`retrospective/` (post-hoc review), `validation/` (confounder-managed checks). Substrate
for the counterfactual replay role and the citation-chain flagship. Design intent
(historical): `docs/design/feedback-loop.md`.

## Key invariants

- Read-and-analyze only — the feedback loop measures and proposes; it does not mutate live trading state.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
