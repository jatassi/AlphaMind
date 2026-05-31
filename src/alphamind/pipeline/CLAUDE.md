# pipeline/ — staged pipeline composition

Orchestration. The thin staging layer the scheduler drives: `analysis.py` (run the
analysis-layer agents) and `decision.py` (run the decision chain), with shared wiring in
`_shared.py`. A composition root that assembles domain packages into the two LLM stages.
Design intent (historical): `docs/architecture/component-boundaries.md`.

## Key invariants

- A composition root **above** the domain (enforced by `.importlinter` `composition-root-layering`) — domain packages must not import `pipeline/`.
- Stage glue only: orchestration belongs here, business logic belongs in the layer packages.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
