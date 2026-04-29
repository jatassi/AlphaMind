---
status: in_progress
completed_date:
commit_id:
---

# 02 — Package skeleton & configuration

## Goal

Stand up the Python package layout under `src/alphamind/risk_guardrails/breach_behavior/` and land the per-feature operator-tunable knobs the breach-behavior primitives consume. Empty modules with import-only smoke tests, plus a parseable YAML stub and a Pydantic config model. All subsequent stories drop modules into this skeleton.

## Reading

- `docs/design/06-risk-guardrails/breach-behavior.md` — the full feature spec; the four-zone escalation model, per-rule breach-response classification, forced-reduction policy, drawdown halt mode, margin cascade handling, emergency invocation triggers, hard rejection semantics, and engine-originated traceability live here.
- `docs/design/06-risk-guardrails/README.md` — placement of breach behavior within the four-tier guardrail enforcement model and its relationships with the continuous monitor, state delivery, regime adaptation, and rules-and-limits.
- `docs/design/configuration-management.md` § Composition model — where breach-behavior's runtime knobs sit relative to profile / regime / mode / overlay (most are inherited from `guardrails.yaml`; breach-behavior owns very little operator-tunable behavior).
- `docs/implementation/06-risk-guardrails/state-delivery/02-package-skeleton-and-config.md` — sibling story precedent; mirror its structure for `<feature>Config` Pydantic + YAML loader, `tests/risk_guardrails/<feature>/` test mirror, and per-component flat-depth file layout.
- `docs/implementation/06-risk-guardrails/guardrail-evaluation/01-scaffold-canonical-types.md` — sibling precedent for a `risk_guardrails/<feature>/` package with multiple per-concern module files.
- `src/alphamind/risk_guardrails/breach_behavior/__init__.py` — currently empty; this story populates it.
- `src/alphamind/risk_guardrails/state_delivery/__init__.py` and `src/alphamind/portfolio_state/__init__.py` — sibling references for the `<feature>Config` Pydantic + YAML loader pattern (flatten-then-validate; `model_config = ConfigDict(frozen=True)`; positive-int Field constraints; `load_<feature>_config(path: pathlib.Path) -> <Feature>Config`).
- `config/guardrails.yaml` — already shipped via the configuration-management work tree; carries the per-rule `escalation_zones`, `breach_response`, `monitor_between_invocations`, `progressive_tiers` (cumulative drawdown only), and the `emergency_invocation` block (cooldown + trigger list). Breach-behavior reads these via the resolver / `RuleRegistry` rather than parsing the file directly.

## Depends on

None within this work tree (story 01 is README-only and runs in parallel).

## Scope

In scope:

- Top-level package at `src/alphamind/risk_guardrails/breach_behavior/` with submodules organized by concern (per-component depth per `feedback_scaffold_per_component_depth.md`). Empty `__init__.py` files; subsequent stories populate the modules:

  ```
  src/alphamind/risk_guardrails/breach_behavior/
      __init__.py
      config.py                # this story — BreachBehaviorConfig + loader
      types.py                 # 03 — canonical typed records and enums
      zones.py                 # 04a — zone classifier
      drawdown_tiers.py        # 04b — cumulative drawdown tier classifier + progressive overrides
      hard_rejection.py        # 04c — hard rejection payload assembler
      position_selection.py    # 04d — forced-reduction position-selection primitives
      halt_state.py            # 05a — halt state computation
      secondary_breach.py      # 05b — secondary breach check
      emergency_triggers.py    # 05c — emergency invocation trigger evaluation
      engine_envelope.py       # 06 — engine-originated envelope assembler
      cascade.py               # 07 — margin call cascade orchestration
  ```

  Module files are added as their owning stories land; this story creates only `config.py` and the empty `__init__.py`.

- Test mirror at `tests/risk_guardrails/breach_behavior/` with `__init__.py` only; subsequent stories populate per-module test files. The directory `tests/risk_guardrails/__init__.py` may already exist via sibling work (rules-and-limits, guardrail-evaluation, state-delivery work trees scaffold the same parent); this story creates it if missing without breaking siblings' expectations.

- `config/breach_behavior.yaml` with the keys breach-behavior primitives consume:

  ```yaml
  breach_behavior:
    forced_reduction:
      short_trim_target_pct_of_limit: 95          # see breach-behavior.md § Position selection logic — short trims close to 95% of the applicable limit
      total_short_immediate_threshold_pct_of_limit: 110  # breach-behavior.md § Per-rule breach response — total short > 110% of limit triggers immediate reduction
    drawdown_velocity:
      window_minutes: 30                          # breach-behavior.md § Emergency invocation trigger — daily drawdown crossing the threshold within this window fires
      threshold_pct_of_daily_limit: 60            # ... at 60% of the active daily-drawdown limit
    multi_rule_breach:
      simultaneous_deferred_rules_count: 3        # breach-behavior.md § Emergency invocation trigger — 3+ deferred rules simultaneously breach
    cascade:
      max_steps: 8                                # defensive cap on cascade chain length; cascade orchestration aborts and surfaces if exceeded
    delta_buffer:
      secondary_check_buffer_factor: 1.0          # multiplier applied to guardrail-evaluation's buffered delta when checking secondary breaches; default 1.0 reuses the library's buffer unchanged
  ```

  Field rationale:
  - `forced_reduction.short_trim_target_pct_of_limit: 95` — the design's "trim to 95% of the applicable limit" rule for short-exposure breaches and single-short breaches. Surfaced here so the trim target isn't a magic constant in `position_selection.py`.
  - `forced_reduction.total_short_immediate_threshold_pct_of_limit: 110` — the design's `> 110% of limit triggers immediate partial reduction` rule. Surfaced so the deferral/immediate-action boundary is operator-tunable if calibration shifts.
  - `drawdown_velocity.window_minutes: 30` and `drawdown_velocity.threshold_pct_of_daily_limit: 60` — the design's "daily drawdown crosses 60% of limit within 30 minutes of last check" trigger. Surfaced as two related knobs so the orchestrator can re-tune cadence vs. threshold independently.
  - `multi_rule_breach.simultaneous_deferred_rules_count: 3` — the design's "3+ deferred rules simultaneously breach between invocations" trigger. Surfaced so the threshold doesn't hardcode in `emergency_triggers.py`.
  - `cascade.max_steps: 8` — defensive cap on cascade chain length; protects against pathological cascade loops in test/error cases. The design does not specify a cap; 8 is a starting value that comfortably covers the largest documented cascade (margin call → forced reduction → secondary breach → final close — 4 steps) plus headroom.
  - `delta_buffer.secondary_check_buffer_factor: 1.0` — multiplier reusing guardrail-evaluation's buffered delta unchanged; reserved for tightening the buffer in cascade re-evaluation if calibration shows the standard buffer is insufficient. Default 1.0 is a no-op.

  All five sections are intentionally minimal — most of breach-behavior's semantics live in `guardrails.yaml` (per-rule zones, breach-response classification, progressive tiers, emergency-invocation cooldown + triggers list). Breach-behavior does not own rule values, regime multipliers, escalation zone thresholds, or progressive-tier configurations — those live in the rule registry and the resolver consumes them.

- Pydantic v2 `BreachBehaviorConfig` model in `src/alphamind/risk_guardrails/breach_behavior/config.py` with:
  - `forced_reduction_short_trim_target_pct_of_limit: Annotated[float, Field(gt=0.0, le=100.0)]`
  - `forced_reduction_total_short_immediate_threshold_pct_of_limit: Annotated[float, Field(gt=100.0)]` — must exceed 100% (the limit itself); the threshold delineates "small overage deferred" from "significant overage triggers immediate reduction".
  - `drawdown_velocity_window_minutes: Annotated[int, Field(gt=0)]`
  - `drawdown_velocity_threshold_pct_of_daily_limit: Annotated[float, Field(gt=0.0, le=100.0)]`
  - `multi_rule_breach_simultaneous_deferred_rules_count: Annotated[int, Field(ge=2)]` — must be at least 2; a single deferred-rule breach is not a multi-rule trigger.
  - `cascade_max_steps: Annotated[int, Field(gt=0)]`
  - `delta_buffer_secondary_check_buffer_factor: Annotated[float, Field(gt=0.0)]`
  - `model_config = ConfigDict(frozen=True)`
  - A `load_breach_behavior_config(path: pathlib.Path) -> BreachBehaviorConfig` helper that parses the YAML and validates against the model. Mirror the flatten-then-validate shape from `src/alphamind/risk_guardrails/state_delivery/config.py` once that lands, or from `src/alphamind/portfolio_state/__init__.py`'s `load_portfolio_state_config` if state-delivery's config helper has not yet shipped.

- `tests/risk_guardrails/breach_behavior/test_config.py`:
  - `config/breach_behavior.yaml` parses into a `BreachBehaviorConfig` with the documented values.
  - Negative or zero values for any positive field fail parse with a field-path-bearing error.
  - `forced_reduction_total_short_immediate_threshold_pct_of_limit` set to exactly 100.0 fails parse (the constraint is strict `>` 100.0).
  - `multi_rule_breach_simultaneous_deferred_rules_count` set to 1 fails parse (the constraint is `>= 2`).
  - Missing required keys fail parse with a field-path-bearing error.

- `tests/risk_guardrails/breach_behavior/test_imports.py`:
  - Imports `alphamind.risk_guardrails.breach_behavior` and each submodule that exists at this story's commit (`config`); subsequent stories add their own import smoke tests when they land their modules.

Out of scope:
- Any classifier, payload assembler, position selector, halt-state computation, secondary-breach check, emergency-trigger evaluator, engine-envelope assembler, or cascade orchestration logic (stories 03–07).
- Wiring `BreachBehaviorConfig` into the broader cascaded composition under `main.yaml` — the resolver-level composition story lives in the configuration-management foundation work tree and is not re-litigated here.
- Defining canonical typed records (`HaltState`, `EmergencyContext`, `EngineEnvelope`, `HardRejectionPayload`, `RegimeTransitionBreach`, `EngineGuardrailTriggerRecord`, `SecondaryBreachCheckResult`, etc.) — story 03 owns those.
- Rendering or formatting any text — breach-behavior emits typed records; rendering is state-delivery's domain.

## Notes

`src/alphamind/risk_guardrails/breach_behavior/` is the canonical placement; the `risk_guardrails/` parent package already exists with empty `__init__.py` files for `state_delivery/`, `rules_and_limits/`, `guardrail_evaluation/`, `regime_adaptation/`, `breach_behavior/`, and `scenario_tests/`. This story populates one of those existing siblings; no new top-level package is introduced.

The `breach_behavior/` layout uses per-file flat depth (`zones.py`, `position_selection.py`, etc.) rather than subdirectories — each primitive is a single-file concern, and per-component subdirectories would over-structure. The `state_delivery/` flat-file pattern is the precedent. If any single primitive grows past one file (e.g., position-selection accreting per-breach-type helper modules), the file can be promoted to a subdirectory in a follow-up without changing import paths.

Per `feedback_simplify_before_building.md`, the five `breach_behavior.yaml` knobs are the minimum that decouple operator-tunable behavior from the primitives. Resist the urge to add more knobs preemptively — additional behavior (e.g., per-breach-type position-selection tiebreakers, per-trigger cooldown overrides, alternate cascade depth caps per breach class) is hardcoded for stability and lifted into config only when an operator explicitly requests it.

Per `feedback_avoid_numeric_anchors.md`, the YAML's numeric defaults are operator-tunable infrastructure thresholds, not LLM behavior targets. Subsequent stories that read them must not introduce hardcoded magic numbers; they must import from `BreachBehaviorConfig` to access them. The per-rule escalation zones, progressive-tier triggers, and emergency-invocation cooldown live in `guardrails.yaml` (already shipped); breach-behavior reads them via the rule registry / resolver, not by re-declaring them here.

The `tests/risk_guardrails/__init__.py` and `tests/risk_guardrails/breach_behavior/__init__.py` files may already exist on `main` (the test-tree scaffolding may have been landed alongside the empty `src/alphamind/risk_guardrails/breach_behavior/` package). This story leaves both files in place if present; the acceptance criteria check existence rather than creation. Add but do not modify or remove existing files.

Per `feedback_no_inventing_component_names.md`, every Python identifier in this story (and subsequent ones in this work tree) maps to a named concept in `breach-behavior.md`, `state-delivery.md`, `engine-envelope-schema.md`, or the `guardrails.yaml` schema. The five YAML-knob sections (`forced_reduction`, `drawdown_velocity`, `multi_rule_breach`, `cascade`, `delta_buffer`) name design-doc constructs verbatim; the field names paraphrase the design's per-clause language without inventing new terminology.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/breach_behavior/__init__.py` exists.
- [ ] `src/alphamind/risk_guardrails/breach_behavior/config.py` exists with the documented `BreachBehaviorConfig` Pydantic v2 model and the documented `load_breach_behavior_config` helper.
- [ ] `tests/risk_guardrails/__init__.py` exists.
- [ ] `tests/risk_guardrails/breach_behavior/__init__.py` exists.
- [ ] `config/breach_behavior.yaml` exists with the five documented top-level keys under `breach_behavior:` (`forced_reduction`, `drawdown_velocity`, `multi_rule_breach`, `cascade`, `delta_buffer`) and all seven leaf field defaults match the documented values.
- [ ] `load_breach_behavior_config(Path("config/breach_behavior.yaml"))` parses into a `BreachBehaviorConfig` whose seven field values match the documented defaults.
- [ ] Zero or negative values for any positive field fail parse with a field-path-bearing error.
- [ ] `forced_reduction_total_short_immediate_threshold_pct_of_limit` set to exactly 100.0 fails parse.
- [ ] `multi_rule_breach_simultaneous_deferred_rules_count` set to 1 fails parse.
- [ ] Removing any of the seven required YAML keys fails parse with a field-path-bearing error.
- [ ] `tests/risk_guardrails/breach_behavior/test_imports.py` imports `alphamind.risk_guardrails.breach_behavior` and `alphamind.risk_guardrails.breach_behavior.config` successfully.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
