# portfolio_state/ — portfolio read-model (positions, P/L, theses)

Data layer (internal). The queryable view of portfolio state the decision and risk layers
read: `records/`, `aggregates/`, `computations/`, `views/`, `consumers/`, `events/`,
`protocols/`. Sourced from execution-layer writes. Design intent (historical):
`docs/design/01-data-layer/internal/portfolio-state.md`.

## Key invariants

- **`portfolio_state/` must NOT import `risk_guardrails/`** (enforced by `.importlinter` `portfolio_state-not-risk_guardrails`).
- Read-model: it surfaces state, it does not author trades — execution is the single writer.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
