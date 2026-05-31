# config/ — YAML configuration loading, models, validation

Foundation. Loads and validates the `config/*.yaml` tree (composition model, run-type
overlays, snapshot persistence). Subpackages: `models/` (typed config schemas),
`validation/` (layered validators), `control_handlers/` (runtime config-change handlers).
Design intent (historical): `docs/design/configuration-management.md`.

## Key invariants

- Config is validated on load — a malformed or out-of-range value should fail fast, not propagate.
- Secrets are referenced by env-var name, never stored inline in YAML.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- (none recorded yet)
