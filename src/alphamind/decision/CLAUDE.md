# decision/ — the LLM decision chain (analyst → strategist → PM)

Decision layer. Sequential reasoning over the synthesizer snapshot: `analyst/`
(asymmetric new-entry theses) → `strategist/` (re-evaluates every open position) →
`proposal_pre_processor/` (deterministic merge + cross-proposal annotations) →
`portfolio_manager/` (critically accepts / modifies / rejects, emits the submit
envelope). Shared types in `_shared/`. Canonical command/envelope models live in the
sibling `commands/` package. Design intent (historical):
`docs/design/04-decision-layer/`.

## Key invariants

- **`decision/` must NOT import `execution/`** (enforced by `.importlinter` `decision-not-execution`). The PM emits an envelope; execution consumes it.
- The chain is strictly sequential — each stage sees the prior stage's structured output.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
