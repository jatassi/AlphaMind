# 03a — Repository active_risk_parameters_provider helper

## Goal

Ship `make_active_risk_parameters_provider` under `src/alphamind/execution/guardrail_enforcement/repository_provider.py` — a typed adapter that turns a `Phase1EnforcementResult` into the `Callable[[], Awaitable[ActiveRiskParameterSet]]` shape the `SqlPortfolioStateRepository` expects for its `active_risk_parameters_provider` slot. Migrate `src/alphamind/scripts/verify_state_persistence.py` to construct its provider via this helper against a representative regime/drawdown setup. Once shipped, [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)'s eventual decision-layer pipeline composition will use this same helper to wire production code; for now the migration scope is the verify script.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement layer.
* `src/alphamind/execution/state_persistence/repository/sql_repository.py` § `_active_risk_parameters_provider` — the slot this story populates.
* `src/alphamind/execution/state_persistence/repository/__init__.py` § `build_sql_repository` factory — the factory that takes the provider as input.
* `src/alphamind/scripts/verify_state_persistence.py` — current ad-hoc provider construction; migrate to the new helper.
* `src/alphamind/portfolio_state/assembler.py` § Step 2 — `repository.get_active_risk_parameters()` consumer (no edits in this story; understanding the consumer keeps the helper's behavior consistent).
* `src/alphamind/execution/guardrail_enforcement/orchestrator.py` (story 02) — for `Phase1EnforcementResult`.

## Depends on

* [ALP-395](https://linear.app/alphamind-jatassi/issue/ALP-395/02-compose-phase-1-enforcement-orchestrator) (this work tree, story 02) — for `Phase1EnforcementResult` and `compose_phase_1_enforcement`.

## Scope

In scope, all under `src/alphamind/execution/guardrail_enforcement/` and `src/alphamind/scripts/verify_state_persistence.py`. Tests at `tests/execution/guardrail_enforcement/`.

### 1\. `make_active_risk_parameters_provider` helper

`src/alphamind/execution/guardrail_enforcement/repository_provider.py` (new file).

```python
def make_active_risk_parameters_provider(
    result: Phase1EnforcementResult,
) -> Callable[[], Awaitable[ActiveRiskParameterSet]]:
    """Build the provider callable expected by ``SqlPortfolioStateRepository``.

    The returned callable, when awaited, yields ``result.active_risk_parameters``.
    Stateless — same input ``result`` produces a callable that always returns
    the same parameter set.
    """
```

The implementation is a one-line closure; the value is the typed boundary, not the logic.

### 2\. `verify_state_persistence.py` migration

Update both call sites in `src/alphamind/scripts/verify_state_persistence.py` (the two factory invocations near lines 1273 and 1351) to construct the `active_risk_parameters_provider` via:

1. Build a synthetic `RegimeAdaptationOutput` and `DrawdownState` matching the test fixture's intent.
2. Call `compose_phase_1_enforcement(...)` with `progressive_tiers` loaded from `config/breach_behavior.yaml`.
3. Pass the result through `make_active_risk_parameters_provider(result)` to obtain the callable.

The `prior_active_risk_parameters_provider` callable continues to use the existing pattern (it serves a different purpose — historical lookup by prior invocation ID) and is out of scope here.

### 3\. Public surface

In `src/alphamind/execution/guardrail_enforcement/__init__.py`:

```python
from alphamind.execution.guardrail_enforcement.repository_provider import (
    make_active_risk_parameters_provider,
)
```

### Out of scope

* Replacing the provider pattern with direct orchestrator invocation in the assembler — the provider injection is load-bearing for testability and [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)'s eventual production wiring.
* Wiring `make_active_risk_parameters_provider` into [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)'s decision-layer pipeline composition — that is [ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)'s responsibility; this story ships the helper it will consume.
* Persistence of the composed parameter set to a new DB table — the existing assembler flow is unchanged.
* Modifying the `prior_active_risk_parameters_provider` slot or its semantics.
* Modifying `verify_oms_commands.py` and decision-layer fixtures — that is story 03b.

## Acceptance criteria

- [ ] `make_active_risk_parameters_provider` is importable from `alphamind.execution.guardrail_enforcement`.
- [ ] The returned callable is awaitable and yields `result.active_risk_parameters` (object equality acceptable; no copy).
- [ ] Calling the callable multiple times returns the same parameter set.
- [ ] The signature matches the type expected by `SqlPortfolioStateRepository.__init__`'s `active_risk_parameters_provider` parameter (verified by mypy / type-checker — assigning the helper's output to a variable typed as the repository's expected callable type type-checks cleanly).
- [ ] `verify_state_persistence.py` no longer constructs `ActiveRiskParameterSet(...)` inline for the `active_risk_parameters_provider` slot; it constructs a `Phase1EnforcementResult` via `compose_phase_1_enforcement(...)` and passes through `make_active_risk_parameters_provider`.
- [ ] `uv run python scripts/verify_state_persistence.py` runs against a freshly-migrated DB and produces `RESULT: PASS` (regression check that the migration didn't break the existing verify flow).
- [ ] `tests/execution/guardrail_enforcement/test_repository_provider.py` exists and passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/guardrail_enforcement/test_repository_provider.py -n auto -v` — all new tests pass.
* Run `uv run python scripts/verify_state_persistence.py` against a freshly-migrated DB — `RESULT: PASS`.
* Run `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.