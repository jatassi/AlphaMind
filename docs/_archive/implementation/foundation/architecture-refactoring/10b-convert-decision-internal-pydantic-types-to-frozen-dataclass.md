# 10b — Convert decision internal Pydantic types to frozen dataclass

## Goal

Convert the 15 actual decision-subdivision P5 violations (`ValidationError`, `ValidationWarning`, `ValidationResult`, `*Result`, `HarnessSuccess`) from Pydantic to `@dataclass(frozen=True, slots=True)` with `__post_init__` invariants. The audit's per-subdivision triage classified the other ~69 of 84 L9 hits as warranted boundary types (analyst/strategist/PM output schemas fed to `output_format` json_schema mode); those stay Pydantic.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L5; decision-subdivision triage names the 15 actual violations
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decision (D)
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/validation.py:69,77,85` — 3 violation sites each × 3 agents = 9
* `src/alphamind/decision/portfolio_manager/{runner.py:123, harness.py:241}` — `PMResult`, `HarnessSuccess`
* `src/alphamind/decision/strategist/{runner,harness}.py` — `StrategistResult`, `HarnessSuccess`
* `src/alphamind/decision/analyst/{runner,harness}.py` — `AnalystResult`, `HarnessSuccess`
* Story 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — `commands/` kernel exists; decision boundary types use those for the LLM-tool surface

## Depends on

* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — `commands/` exists; boundary types reference kernel types cleanly

## Scope

In scope: convert 15 internal types in `decision/{analyst,strategist,portfolio_manager}/{validation,runner,harness}.py` from Pydantic to frozen dataclass. Tests update accordingly.

### Conversion list

* `{analyst,strategist,portfolio_manager}/validation.py:69,77,85`: `ValidationError`, `ValidationWarning`, `ValidationResult` (3 classes × 3 agents = 9)
* `portfolio_manager/runner.py:123` `PMResult`
* `portfolio_manager/harness.py:241` `HarnessSuccess` (PM)
* `strategist/runner.py` `StrategistResult`
* `strategist/harness.py` `HarnessSuccess` (strategist)
* `analyst/runner.py` `AnalystResult`
* `analyst/harness.py` `HarnessSuccess` (analyst)

### Keep boundary Pydantic

* `analyst/models.py` `AnalystOutput` (fed to `output_format=json_schema`)
* `strategist/models.py` `StrategistOutput`
* `portfolio_manager/models.py` `PMEnvelope`, `PMCompletionRecord`, `PMAnalystEnvelope`, `PMStrategistEnvelope`
* `proposal_pre_processor/models.py` `BUNDLE_OUTPUT_SCHEMA` and related

### Coordination

* Decision agents' `HarnessSuccess` may be unified post-06d (if `_harness_core` exposes a single `HarnessSuccess` shape, the per-agent variants disappear). Either way, the type is frozen dataclass.
* `arbitrary_types_allowed=True` on `GuardrailValidationResult` (`analyst/models.py:356,380,442`, `strategist/models.py:274,298,545`) — this is a separate concern (audit's H6); leave for now or address opportunistically.

## Acceptance criteria

- [ ] 15 named types are `@dataclass(frozen=True, slots=True)`.
- [ ] Boundary Pydantic types (AnalystOutput, StrategistOutput, PMEnvelope, etc.) remain Pydantic.
- [ ] `mypy --strict` passes; no `BaseModel.model_dump`/`model_validate` calls on the converted types remain in consumers.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "class ValidationError\|class ValidationWarning\|class ValidationResult\|class PMResult\|class StrategistResult\|class AnalystResult\|class HarnessSuccess" src/alphamind/decision/` shows `@dataclass(frozen=True, slots=True)` decorators on all hits, no `class X(BaseModel)`. End-to-end decision pipeline test (`scripts/verify_decision_pipeline.py` if runnable in tests) passes unmodified.