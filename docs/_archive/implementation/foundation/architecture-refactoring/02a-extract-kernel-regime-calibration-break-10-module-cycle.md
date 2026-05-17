# 02a — Extract `_kernel/regime` + `_kernel/calibration`; break 10-module cycle

## Goal

Eliminate the 10-module import cycle that spans `portfolio_state` ↔ `risk_guardrails` ↔ `distillation` ↔ `persistence` by extracting the four shared enums (`RegimeLabel`, `RegimeTransitionState`, `RiskZone`, `DrawdownTier`) and the calibration vocabulary (`CalibrationState`, `CALIBRATION_STATE_VALUES`, `EXTENDED_HOURS_BOOTSTRAP_RATE`) into a new leaf module `alphamind/_kernel/` with zero outbound first-party dependencies. After this story, every layer imports the shared vocabulary downward from `_kernel/`; no upstream layer imports types from a downstream layer.

The cycle's main wire is `portfolio_state/records/capital.py` — a 50-line re-export shim whose docstring admits <issue id="2d1cd9b2-12f0-49fc-ae3d-6c64a6733638">ALP-344</issue>/<issue id="273434f7-591e-4ac2-bb85-eb601511ed4f">ALP-347</issue> status ("Identity is preserved across all import paths"). This story deletes the shim, removes the `# noqa: E402` chain at `risk_guardrails/regime_adaptation/types.py:62-66`, and removes the function-local lazy imports in `distillation/calibration.py:216,303` — all of which exist solely as cycle workarounds.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L2 — names the four enums + calibration vocab as the root cause, with concrete import edges
* `src/alphamind/risk_guardrails/breach_behavior/types.py:36-40, 47-53` — current home of `DrawdownTier`; imports `RegimeLabel`/`RegimeTransitionState` from `regime_adaptation/types.py`
* `src/alphamind/risk_guardrails/regime_adaptation/types.py:55-66, 74, 82-85` — current home of `RegimeLabel`/`RegimeTransitionState`; the `# noqa: E402` block + `TYPE_CHECKING` workaround
* `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — current home of `RiskZone`
* `src/alphamind/distillation/calibration.py:47-51, 216, 303` — current home of `CALIBRATION_STATE_VALUES`, `CalibrationState`; function-local imports of `persistence.models` are cycle workarounds
* `src/alphamind/portfolio_state/records/capital.py:8-12, 17-49` — the re-export shim (docstring admits temporary status)
* `src/alphamind/portfolio_state/aggregates/drawdown.py:14`, `aggregates/risk_parameters.py:15`, `aggregates/risk_budget.py`, `aggregates/thesis_quality.py` — portfolio_state aggregates that import risk_guardrails enums (the upward edges that close the cycle)
* `src/alphamind/persistence/models.py:25-26` — imports `CALIBRATION_STATE_VALUES` (downward → distillation) and `RegimeTransitionState` (downward → risk_guardrails) — both inversions
* Parent issue <issue id="596c36c6-62ed-462c-975a-dbb92bbdf425">ALP-454</issue> § Pre-resolved decisions (A) — `_kernel/` naming
* `.claude/skills/python-architecture/references/foundations.md` § A2 — import-graph thesis

## Depends on

* 01b (<issue id="67c0550c-487c-44ba-ad9c-3e9f9f807d79">ALP-456</issue>) — creates the `_kernel/__init__.py` package skeleton this story populates

## Scope

In scope: `src/alphamind/_kernel/regime.py` and `src/alphamind/_kernel/calibration.py` (new files); migration of every consumer of the four enums + the calibration vocab; deletion of `portfolio_state/records/capital.py` re-export shim; removal of `# noqa: E402` and `TYPE_CHECKING` workarounds. Tests at `tests/_kernel/test_regime.py` and `tests/_kernel/test_calibration.py`.

### 1\. Create `_kernel/regime.py`

Move four `StrEnum` definitions verbatim from their current homes to `alphamind/_kernel/regime.py`:

* `RegimeLabel` (from `risk_guardrails/regime_adaptation/types.py`)
* `RegimeTransitionState` (from `risk_guardrails/regime_adaptation/types.py`)
* `RiskZone` (from `risk_guardrails/guardrail_evaluation/types.py`)
* `DrawdownTier` (from `risk_guardrails/breach_behavior/types.py`)

`_kernel/regime.py` has zero first-party imports (only `from enum import StrEnum`). Add `__all__` listing the four enums.

### 2\. Create `_kernel/calibration.py`

Move calibration vocabulary verbatim:

* `CalibrationState` enum (`Literal["calibrated", "bootstrap", "unavailable"]` or `StrEnum`)
* `CALIBRATION_STATE_VALUES` tuple of three strings
* `EXTENDED_HOURS_BOOTSTRAP_RATE` constant if it has no behavioral dependencies (move) or leave in `distillation/calibration.py` (don't move)

`_kernel/calibration.py` has zero first-party imports. Add `__all__`.

### 3\. Migrate consumers

Every consumer of the four enums + calibration vocab retargets to `_kernel`:

* `risk_guardrails/breach_behavior/types.py` — drop `DrawdownTier` definition; `from alphamind._kernel.regime import DrawdownTier` (or rely on re-export — see step 5)
* `risk_guardrails/regime_adaptation/types.py` — drop `RegimeLabel`/`RegimeTransitionState` definitions; import from `_kernel.regime`; **delete** the `# noqa: E402` block at lines 62-66 and the `TYPE_CHECKING` workaround at 74, 82-85
* `risk_guardrails/guardrail_evaluation/types.py` — drop `RiskZone` definition; import from `_kernel.regime`
* `distillation/calibration.py` — drop `CALIBRATION_STATE_VALUES`/`CalibrationState` definitions; import from `_kernel.calibration`; **delete** function-local imports at lines 216, 303 (they were cycle workarounds and become unnecessary)
* `persistence/models.py:25-26` — retarget imports to `_kernel.calibration` and `_kernel.regime`
* `portfolio_state/aggregates/{drawdown,risk_parameters,risk_budget,thesis_quality}.py` — retarget enum imports to `_kernel.regime`

### 4\. Delete `portfolio_state/records/capital.py` re-export shim

The shim re-exported `DrawdownTier`, `RiskZone`, `RegimeLabel`, `RegimeTransitionState` from `risk_guardrails` to `portfolio_state.records.capital`. After step 3, every consumer of these enums imports from `_kernel.regime`. Verify zero hits for `from alphamind.portfolio_state.records.capital import (DrawdownTier|RiskZone|RegimeLabel|RegimeTransitionState)` then delete the file.

Other contents of `records/capital.py` (e.g., `ActiveRiskParameterSet`, `RiskBudgetConsumption`, etc.) move to a new `portfolio_state/records/risk_parameters.py` or stay in `aggregates/` — pick whichever home minimizes consumer churn.

### 5\. Keep or remove per-subdivision re-exports (judgment call)

`risk_guardrails/breach_behavior/__init__.py:14-60` currently re-exports `DrawdownTier`, `RiskZone`, etc. via star-imports from `types.py`. Keep these re-exports pointing at `_kernel.regime` (so external consumers don't have to retarget all at once), or remove them and update every external consumer. Default to keeping for now and add a deprecation comment; the orchestrator can decide based on consumer count.

### Out of scope

The 12-module decision↔execution cycle is broken by story 02b in parallel. Pydantic→frozen-dataclass conversion of risk_guardrails records is story 10e. Money/Decimal migration is story 05b/06a. This story is purely the structural extraction of shared enums + calibration vocab.

## Acceptance criteria

- [ ] `src/alphamind/_kernel/regime.py` exists with `RegimeLabel`, `RegimeTransitionState`, `RiskZone`, `DrawdownTier` defined and `__all__` populated.
- [ ] `src/alphamind/_kernel/calibration.py` exists with `CalibrationState`, `CALIBRATION_STATE_VALUES` defined and `__all__` populated.
- [ ] `_kernel/regime.py` and `_kernel/calibration.py` import zero first-party (`alphamind.*`) modules.
- [ ] `risk_guardrails/regime_adaptation/types.py` has no `# noqa: E402` directives; no `TYPE_CHECKING` workaround for `RegimeLabel`/`RegimeTransitionState`/`ActiveRiskParameterSet`.
- [ ] `distillation/calibration.py:216, 303` has no function-local imports of `alphamind.persistence.models`.
- [ ] `portfolio_state/records/capital.py` is deleted.
- [ ] Running `uv run python .claude/skills/python-architecture/scripts/analyze_imports.py src/alphamind` reports the 10-module cycle (`portfolio_state.aggregates.drawdown → ... → distillation.output → portfolio_state.aggregates.drawdown`) is no longer present in the `cycles` field.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run pytest -n auto` all pass.
- [ ] `uv run lint-imports` passes (story 01a's contracts continue to hold).

## Verification

Run `python .claude/skills/python-architecture/scripts/analyze_imports.py src/alphamind | jq '.cycles | length'` — value drops from 4 to ≤3, and the specific 10-module cycle does not appear. Spot-check: `python -c "from alphamind._kernel.regime import RegimeLabel, DrawdownTier; print(RegimeLabel.__module__, DrawdownTier.__module__)"` prints `alphamind._kernel.regime` for both. Spot-check: `python -c "from alphamind.portfolio_state.aggregates.drawdown import DrawdownState"` succeeds without triggering circular import. All `tests/portfolio_state/`, `tests/risk_guardrails/`, `tests/distillation/`, `tests/persistence/` test modules pass under `uv run pytest -n auto`.