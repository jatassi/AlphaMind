# scheduler/ — pipeline composition root + APScheduler driver

Orchestration. The deliberative pipeline's entry point (`python -m alphamind.scheduler`).
APScheduler fires the Tier B cadence (~3 weekday runs — pre-open / mid-day / pre-close —
plus a Sunday run). Key files: `driver.py`, `orchestrator.py`, `invocation.py`,
`phase1_inputs.py`, `phase2_dispatch.py`, `run_context.py`, `supervisor.py`,
`emergency.py`, `fresh_start.py`; `debug_e2e/` holds the offline end-to-end harness.
Design intent (historical): `docs/design/pipeline-control-and-events-schema.md`,
`docs/architecture/component-boundaries.md`.

## Key invariants

- `scheduler/` and `pipeline/` are composition roots **above** the domain (enforced by `.importlinter` `composition-root-layering`); domain packages must not import upward into them.
- **Production scheduler code must not import `debug_e2e`** (enforced by `debug-e2e-forbidden-in-production`).
- Each trigger gets a distinct minute, so no two scheduled runs share an `as_of` (prevents distillation UNIQUE-constraint collisions).

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- `run_context.py` references `debug_e2e.settings` only under `TYPE_CHECKING`; `.importlinter` excludes type-checking imports so that seam stays a type-time-only edge.
