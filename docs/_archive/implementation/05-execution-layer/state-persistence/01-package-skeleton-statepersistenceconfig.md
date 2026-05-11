# 01 — Package skeleton + StatePersistenceConfig

## Goal

Land the `src/alphamind/execution/state_persistence/` package layout — `__init__.py` re-exports, the per-component subdirectory shape (`tables/`, `repository/`, `write_paths/`, `invocation_context/`), and a frozen Pydantic `StatePersistenceConfig` exposing the per-feature configuration knobs (PM-decision sliding window length, retention placeholders, snapshot-isolation timeout). After this story the empty package becomes a discoverable feature surface that downstream stories add into without each one re-deciding layout, and a configuration handle becomes available for stories 03 onward to load.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Logical entities — entity inventory the per-component subdirectories will host
* `docs/design/05-execution-layer/state-persistence.md` § Read paths — the sliding-window N parameter for `recent_pm_decision_log` (typically 2–3) lives in this config
* `src/alphamind/execution/state_persistence/__init__.py` — currently empty; this story populates it
* `src/alphamind/risk_guardrails/breach_behavior/__init__.py` — sibling-feature package layout to mirror (per-component subdirectory depth, frozen-config Pydantic shape)
* `src/alphamind/portfolio_state/__init__.py` — adjacent typed-records layout that this layer round-trips
* `config/main.yaml` — top-level config file the new `state_persistence:` section will live in
* `src/alphamind/config/main_loader.py` (or sibling) — config-loader pattern other features hook into

## Depends on

(none — this is the foundational story for the work tree)

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. Per-component subdirectory layout

Create empty `__init__.py` files seeding the layout:

```
src/alphamind/execution/state_persistence/
  __init__.py                       # re-exports StatePersistenceConfig + load function
  config.py                         # StatePersistenceConfig + loader
  tables/
    __init__.py                     # SQLAlchemy models go here (later stories)
  repository/
    __init__.py                     # SqlPortfolioStateRepository goes here (story 06)
  write_paths/
    __init__.py                     # Phase 1 + Phase 2 helpers go here (stories 07–08)
  invocation_context/
    __init__.py                     # InvocationContext lives here (story 02b)
```

The depth-per-component pattern matches `src/alphamind/risk_guardrails/breach_behavior/` and the project memory `feedback_scaffold_per_component_depth`.

### 2\. `StatePersistenceConfig`

A frozen Pydantic `BaseModel` at `config.py`:

```python
class StatePersistenceConfig(BaseModel):
    model_config = {"frozen": True, "strict": True}

    pm_decision_log_sliding_window_invocations: int = Field(ge=1)
    snapshot_read_timeout_seconds: float = Field(gt=0.0)
    pip_freeze_snapshot_root: str  # filesystem root for process_lifetimes/<id>/pip_freeze.txt
    invocation_provenance_root: str  # filesystem root for invocations/<id>/{resolved_config,data_calibration_state}.json
```

Default values come from `config/main.yaml` under a new top-level `state_persistence:` section. The PM-decision sliding window default is `2` per the design doc's "typically 2–3" guidance — pick a definitional default that downstream stories consume.

### 3\. Config loader

Function `load_state_persistence_config(main_config_yaml: dict) -> StatePersistenceConfig` at `config.py`. Reads the `state_persistence:` block from the loaded `main.yaml` dict and returns the validated config. Raises a clear error when the section is missing.

### 4\. `config/main.yaml` populated section

Add a `state_persistence:` block to `config/main.yaml` with the four keys above. Use filesystem path values consistent with the existing `paths:` block (e.g., `paths.database`'s `%USERPROFILE%`-style on Windows, `~/AlphaMind/...` on POSIX).

### Out of scope

* No SQLAlchemy models — those land in stories 02b through 05.
* No repository implementation — story 06.
* No InvocationContext — story 02b.
* No `process_lifetimes` table — story 02b.

## Acceptance criteria

- [ ] `src/alphamind/execution/state_persistence/__init__.py` re-exports `StatePersistenceConfig` and `load_state_persistence_config`.
- [ ] `src/alphamind/execution/state_persistence/{tables,repository,write_paths,invocation_context}/__init__.py` exist and are importable as empty subpackages.
- [ ] `StatePersistenceConfig` is a frozen Pydantic `BaseModel` with the four named fields and rejects mutation.
- [ ] `load_state_persistence_config` returns a valid `StatePersistenceConfig` from a populated `main.yaml` dict.
- [ ] `load_state_persistence_config` raises a `ValueError` (or descendant) with a clear message when `state_persistence:` is missing from `main.yaml`.
- [ ] `config/main.yaml` carries a populated `state_persistence:` section with all four keys.
- [ ] `tests/execution/state_persistence/test_config.py` exists with at least four tests: load-happy-path, missing-section-raises, frozen-mutation-raises, default-window-size-matches-design-doc.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run the test suite (`uv run pytest tests/execution/state_persistence/ -n auto`) and confirm all four tests pass. Lint clean. Spot-check the `config/main.yaml` block visually for path-expansion conformance with the existing `paths.database` style.