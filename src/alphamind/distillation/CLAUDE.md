# distillation/ — deterministic raw-data → LLM-consumable signals

Distillation layer. Pure, deterministic transforms turning raw collected data into
normalized indicators, anomalies, and composites for the analysis layer. Subpackages by
category (`q1/`, `q3/`, `q6/`, `q7/`, `qualitative/`) plus `replay_harness/` (offline
re-run of distillation over historical inputs). Design intent (historical):
`docs/design/02-distillation-layer/`.

## Key invariants

- **Compute cores are pure** — no SQLAlchemy in the compute path (enforced by `.importlinter` contracts `distillation-no-sqlalchemy` / `distillation-compute-no-sqlalchemy`). DB reads belong in `loaders.py`; `compute.py` stays pure.
- Distillation sits **above** `persistence.models` (enforced) and must not import the orchestration layer.
- Deterministic: same inputs → same outputs (this is what makes the replay harness sound).

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- Some `q*/assemble.py` still thread Sessions in; the compute/load split (punch-list #9) is mid-migration, so the no-sqlalchemy contract carries `ignore_imports` entries that retire as each split lands.
