---
status: in_progress
completed_date:
commit_id:
---

# 02 — Package skeleton & configuration

## Goal

Stand up the Python package layout for state-delivery under `src/alphamind/risk_guardrails/state_delivery/` and land the per-feature configuration knobs the rendering layer reads. Empty packages with import-only smoke test, plus a parseable YAML stub. All subsequent stories drop modules into this skeleton.

## Reading

- `docs/design/06-risk-guardrails/state-delivery.md` — the full feature spec; the three audience headers, halt-mode header modifications, emergency invocation header, and the validation tool live here
- `docs/design/06-risk-guardrails/README.md` — placement of state-delivery within the four-tier guardrail enforcement model
- `docs/design/configuration-management.md` § Composition model — where state-delivery's runtime knobs sit relative to profile / regime / mode / overlay (most are inherited; state-delivery owns very little operator-tunable behavior)
- `docs/implementation/01-data-layer/portfolio-state/02-package-skeleton-and-config.md` — sibling pattern for `src/alphamind/<feature>/` skeleton with a per-feature `config/<feature>.yaml`
- `docs/implementation/02-distillation-layer/replay-harness/02-package-skeleton-and-cli-stub.md` — sibling pattern for a flat per-component module layout under a top-level package
- `src/alphamind/risk_guardrails/state_delivery/__init__.py` — the (currently empty) directory this story populates
- `src/alphamind/portfolio_state/__init__.py` — sibling reference for the `<feature>Config` Pydantic + YAML loader pattern

## Depends on

None within this work tree (story 01 is README-only and runs in parallel).

## Scope

In scope:

- Top-level package at `src/alphamind/risk_guardrails/state_delivery/` with submodules organized by audience and concern (per-component depth per `feedback_scaffold_per_component_depth.md`). Empty `__init__.py` files; subsequent stories populate the modules:

  ```
  src/alphamind/risk_guardrails/state_delivery/
      __init__.py
      config.py                # this story — StateDeliveryConfig + loader
      primitives.py            # 03 — shared rendering helpers
      analyst.py               # 04a — analyst header renderer
      strategist.py            # 04b — strategist header renderer
      portfolio_manager.py     # 04c — PM header renderer
      halt_mode.py             # 05 — halt-mode header modifications
      emergency.py             # 06 — emergency invocation header
      validation_tool.py       # 07 — guardrail validation tool
  ```

- Test mirror at `tests/risk_guardrails/state_delivery/` with `__init__.py` only; subsequent stories populate per-module test files. The directory `tests/risk_guardrails/__init__.py` may already exist via sibling work (rules-and-limits / guardrail-evaluation work trees scaffold the same parent); this story creates it if missing without breaking the sibling's expectation.

- `config/state_delivery.yaml` with the keys the rendering layer consumes:

  ```yaml
  state_delivery:
    recent_engine_actions:
      lookback_invocations: 1               # PM "Recent engine-originated actions" — invocations of activity-log scope to render
    correlation_state:
      min_position_count: 3                 # PM Correlation state block — omit below this position count
    dependency_risk_flag:
      min_position_count: 3                 # PM Dependency risk flag block — omit below this position count
    abandoned_window:
      lookback_invocations: 1               # Analyst / strategist abandoned-* sections — only the prior invocation
  ```

  Field rationale:
  - `recent_engine_actions.lookback_invocations: 1` — the design's PM header "Recent engine-originated actions (since last invocation)" wording; future operator-tunable for crisis-mode review windows.
  - `correlation_state.min_position_count: 3` — directly from the design's `[omitted if < 3 concurrent positions]` guard. Surfaced here so the threshold isn't a magic constant in `portfolio_manager.py`.
  - `dependency_risk_flag.min_position_count: 3` — same shape; the dependency-risk-flag block inherits the same gating rationale.
  - `abandoned_window.lookback_invocations: 1` — the design's "Scoped to the prior invocation only — older abandonments are stale" rule. Surfaced so the renderer doesn't hardcode the window.

  All four fields are intentionally minimal — most of state-delivery's behavior is determined by upstream config (profile feature flags, regime multipliers, mode behavioral contracts) and the per-rule registry from [`config/guardrails.yaml`](../../foundation/configuration/03f-guardrails-yaml-model.md). State-delivery does not own rule values, regime multipliers, halt-mode action vocabularies, or token budgets — those live upstream and the renderer reads the resolved snapshot.

- Pydantic v2 `StateDeliveryConfig` model in `src/alphamind/risk_guardrails/state_delivery/config.py` with:
  - `recent_engine_actions_lookback_invocations: Annotated[int, Field(gt=0)]`
  - `correlation_state_min_position_count: Annotated[int, Field(gt=0)]`
  - `dependency_risk_flag_min_position_count: Annotated[int, Field(gt=0)]`
  - `abandoned_window_lookback_invocations: Annotated[int, Field(gt=0)]`
  - `model_config = {"frozen": True}`
  - A `load_state_delivery_config(path: pathlib.Path) -> StateDeliveryConfig` helper that parses the YAML and validates against the model. Mirror the flatten-then-validate shape from `src/alphamind/portfolio_state/__init__.py`.

- `tests/risk_guardrails/state_delivery/test_config.py`:
  - `config/state_delivery.yaml` parses into a `StateDeliveryConfig` with the documented values.
  - Negative or zero values for any positive field fail parse with a field-path-bearing error.
  - Missing required keys fail parse with a field-path-bearing error.

- `tests/risk_guardrails/state_delivery/test_imports.py`:
  - Imports `alphamind.risk_guardrails.state_delivery` and each submodule that exists at this story's commit (`config`); subsequent stories add their own import smoke tests when they land their modules.

Out of scope:
- Any rendering helper, audience renderer, halt-mode wrapper, emergency block, or validation-tool implementation (stories 03–07).
- Wiring `StateDeliveryConfig` into the broader cascaded composition under `main.yaml` — the resolver-level composition story lives in the configuration foundation work tree and is not re-litigated here.
- Defining typed pre-render value objects ("HeaderRecord" structures) — the rendering primitives in story 03 take the existing `portfolio_state.records.*` and `portfolio_state.consumers.*` types directly and emit text. No parallel typed-record layer.

## Notes

`src/alphamind/risk_guardrails/state_delivery/` is the canonical placement; the `risk_guardrails/` parent package already exists with empty `__init__.py` files for `state_delivery/`, `rules_and_limits/`, `guardrail_evaluation/`, `regime_adaptation/`, `breach_behavior/`, and `scenario_tests/`. This story adds modules under one of those existing siblings; no new top-level package is introduced.

The `state_delivery/` layout uses per-file flat depth (`analyst.py`, `strategist.py`, `portfolio_manager.py`) rather than subdirectories — each audience renderer is a single-file concern, so a per-component subdirectory would be over-structuring. The `consumers/` flat-file pattern in `src/alphamind/portfolio_state/consumers/` is the precedent. If any single audience renderer outgrows one file (e.g., the PM renderer accreting helper modules), the file can be promoted to a subdirectory in a follow-up without changing import paths.

Per `feedback_simplify_before_building.md`, the four `state_delivery.yaml` knobs are the minimum that decouple operator-tunable behavior from the renderer. Resist the urge to add more knobs preemptively — additional behavior (e.g., per-zone color tags, abbreviation rules, alternate sector ordering) is hardcoded for format stability and lifted into config only when an operator explicitly requests it.

Per `feedback_avoid_numeric_anchors.md`, the YAML's numeric defaults are operator-tunable infrastructure thresholds, not LLM behavior targets. Subsequent stories that read them must not introduce hardcoded magic numbers; they must import from `StateDeliveryConfig` to access them.

The `tests/risk_guardrails/__init__.py` and `tests/risk_guardrails/state_delivery/__init__.py` files already exist on `main` (the test-tree scaffolding was landed alongside the empty `src/alphamind/risk_guardrails/state_delivery/` package). This story leaves both files in place; the acceptance criteria check existence rather than creation. Add but do not modify or remove an existing file.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/state_delivery/__init__.py` exists.
- [ ] `src/alphamind/risk_guardrails/state_delivery/config.py` exists with the documented `StateDeliveryConfig` Pydantic v2 model and the documented `load_state_delivery_config` helper.
- [ ] `tests/risk_guardrails/__init__.py` exists.
- [ ] `tests/risk_guardrails/state_delivery/__init__.py` exists.
- [ ] `config/state_delivery.yaml` exists with the four documented top-level keys under `state_delivery:`.
- [ ] `load_state_delivery_config(Path("config/state_delivery.yaml"))` parses into a `StateDeliveryConfig` whose four field values match the documented defaults.
- [ ] Zero or negative values for any of the four positive fields fail parse with a field-path-bearing error.
- [ ] Removing any of the four required YAML keys fails parse with a field-path-bearing error.
- [ ] `tests/risk_guardrails/state_delivery/test_imports.py` imports `alphamind.risk_guardrails.state_delivery` and `alphamind.risk_guardrails.state_delivery.config` successfully.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
