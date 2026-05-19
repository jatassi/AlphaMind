# 02b — PM-side coordinated edit (canonical imports + sector via resolver)

## Goal

Replace the PM work tree's transitional minimal OMS command models with imports from this work tree's canonical home, and update PM validation to derive sector via the existing `sector_resolver` callable instead of the (now-removed) `OMSPositionSize.sector` inline field. Refresh PM verify fixtures against the canonical OMS shape. After this story, no module under `src/alphamind/decision/portfolio_manager/` redefines OMS command variants — all flow from `alphamind.execution.oms.command_models`.

## Reading

* `src/alphamind/decision/portfolio_manager/oms_command_models.py` — the transitional minimal models that are deleted (or shimmed) by this story; note the docstring already names this file as transitional
* `src/alphamind/decision/portfolio_manager/models.py` — current `from alphamind.decision.portfolio_manager.oms_command_models import (...)` block; replaced by `from alphamind.execution.oms.command_models import (...)` (or via `alphamind.execution.oms`)
* `src/alphamind/decision/portfolio_manager/validation.py` § `_check_embedded_command_sector` (line ~381) — the function that reads `command.position_size.sector`; story 02b switches to `sector_resolver(command.instrument.underlying)`. Note the call already accepts `sector_resolver` as a parameter to the surrounding `validate_pm_envelope`; thread it down.
* `src/alphamind/execution/oms/command_models.py` (story 01a) — canonical models the PM imports
* `tests/decision/portfolio_manager/test_validation.py` — test surface that uses the transitional types; update assertions and fixtures. Line 123's reference to `tactical_exit` becomes `conviction_reduced` per parent decision (E)
* `src/alphamind/scripts/verify_pm.py` — the live-SDK verify script that regenerates PM fixtures; story 02b runs this once after canonical models land
* `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json` — the four PM scenario fixtures that get refreshed against the canonical OMS shape
* Parent issue <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> § Pre-resolved configuration decisions (B) and (E) — sector handling and `tactical_exit` correction

## Depends on

* <issue id="11c3d121-2d77-4218-9355-90a234d5ff7b">ALP-370</issue> (01a) — canonical OMS command models must exist before PM can import from them

## Scope

In scope, under `src/alphamind/decision/portfolio_manager/`, `tests/decision/portfolio_manager/`, and `tests/fixtures/decision/pm/`. No edits under `src/alphamind/execution/`.

### 1\. Replace transitional `oms_command_models.py`

Either:

* **Option A (preferred — clean delete):** Delete `src/alphamind/decision/portfolio_manager/oms_command_models.py` entirely. Update every importer in PM to source from `alphamind.execution.oms.command_models`.
* **Option B (back-compat shim):** Keep the file as a thin re-export module: `from alphamind.execution.oms.command_models import OpenCommand, CloseCommand, ...; __all__ = [...]`. No type definitions in this file.

Choose Option A unless removing the file breaks an external consumer not visible at story time. Update PM `__init__.py` re-exports so external callers continue to see `OMSCommand`, `OpenCommand`, etc. via `alphamind.decision.portfolio_manager`.

### 2\. Update `models.py`

* Change the import block at lines ~34–43 from `from alphamind.decision.portfolio_manager.oms_command_models import (...)` to `from alphamind.execution.oms.command_models import (...)` (or via the canonical `__init__`).
* Verify `PMEnvelope.commands: list[OMSCommand]` continues to type-check against the canonical `OMSCommand` (the discriminated union shape is preserved).
* Remove any references to `OMSInstrument` or `OMSPositionSize` aliases that no longer exist on canonical models — they were transitional names; canonical exposes `Instrument`, `PositionSize`. Update PM consumers accordingly.

### 3\. Update `validation.py`

* Replace `command.position_size.sector` (line ~394) with `sector_resolver(command.instrument.underlying)` where `sector_resolver: Callable[[str], str]` is already a parameter of the surrounding `validate_pm_envelope` call (per decision (B)). For `OptionInstrument` and `StrategyInstrument`, the field is named `underlying` already — same access pattern. For `EquityInstrument`, the design schema names it `ticker` not `underlying`; canonical `EquityInstrument` from story 01a uses `ticker` per design. **Resolution:** `_check_embedded_command_sector` reads from `command.instrument` and dispatches: `EquityInstrument` → `ticker`; `OptionInstrument` / `StrategyInstrument` → `underlying`. A small helper like `def _instrument_resolution_key(instrument) -> str` keeps the dispatch clean.
* Update the function's docstring and `field_path=` annotation to reference `commands[{i}].instrument` rather than `commands[{i}].position_size.sector` (since sector is now derived, not a wire-format field).
* Confirm no other PM-side code reads `position_size.sector`. Grep `command.position_size.sector` and `OMSPositionSize` across `src/alphamind/decision/portfolio_manager/` to catch incidental references.

### 4\. Update `tests/decision/portfolio_manager/`

* Replace `tactical_exit` with `conviction_reduced` in `test_validation.py:123` and any other test fixtures (per parent decision (E)).
* Where tests construct OMS commands directly, the construction shape changes: `OpenCommand(instrument=EquityInstrument(...), position_size=PositionSize(quantity=N, dollar_value=N), entry_order=EntryOrder(...), target=Target(...), invalidation_legs=(...), thesis=Thesis(...))`. Tests that previously passed minimal shapes need to be re-authored to construct the full canonical shape — accept the verbosity; test fixture builders can centralize common fields.
* `_check_embedded_command_sector` tests update to assert: when `sector_resolver(ticker)` returns a sector outside `active_sectors`, validation fails with the documented criterion name; when it returns a sector inside, validation passes. Use a stub `sector_resolver` like `lambda t: "tech" if t == "NVDA" else "financials"`.

### 5\. Refresh PM verify fixtures

After steps 1–4 land and tests pass, run `uv run python scripts/verify_pm.py` to regenerate `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json` against canonical OMS shapes. Live SDK invocation; per-scenario expected runtime ~5 minutes; total runtime ~20 minutes. The refreshed fixtures will:

* Drop `position_size.sector` everywhere it appeared.
* Carry full `entry_order`, `target`, `invalidation_legs`, `thesis.components` shapes per design.
* Use `conviction_reduced` (if any synthesizer brief surfaces a partial-close scenario; otherwise no occurrence change).

If `verify_pm.py` runs cleanly, commit the regenerated fixtures. If it fails, the failure is informative — likely a downstream PM-validator code path that still references a removed transitional field.

### Out of scope

* Engine-stub upgrade to consume canonical models — story 03
* `submit_engine_envelope` write function — story 04
* Any change to canonical models in `src/alphamind/execution/oms/` — story 01a authored them; this story only consumes them

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/oms_command_models.py` is either deleted or contains only re-exports from `alphamind.execution.oms.command_models` (no type definitions).
- [ ] `src/alphamind/decision/portfolio_manager/models.py` imports OMS command types from `alphamind.execution.oms` (or `.command_models`); does not import from `alphamind.decision.portfolio_manager.oms_command_models` (which no longer defines types).
- [ ] `src/alphamind/decision/portfolio_manager/validation.py:_check_embedded_command_sector` derives sector via `sector_resolver(...)` applied to `command.instrument.ticker` (equity) or `command.instrument.underlying` (option / strategy); does not reference `command.position_size.sector`.
- [ ] No file under `src/alphamind/decision/portfolio_manager/` references `tactical_exit`; every reference is replaced with `conviction_reduced`.
- [ ] No file under `src/alphamind/decision/portfolio_manager/` references `OMSPositionSize` or `OMSInstrument` (transitional alias names that no longer exist on canonical models).
- [ ] `tests/decision/portfolio_manager/test_validation.py` constructs OMS commands using canonical full shapes (`OpenCommand` with full sub-records); does not reference the transitional minimal shapes.
- [ ] `uv run pytest tests/decision/portfolio_manager/ -n auto` passes.
- [ ] `tests/fixtures/decision/pm/{normal,halt,emergency,synchronous_rejection}.json` refreshed against canonical OMS shape (no `position_size.sector` keys; full `entry_order` / `invalidation_legs[]` / `thesis.components[]`).
- [ ] `uv run python scripts/verify_pm.py` exits zero against refreshed fixtures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/ -n auto` to confirm PM tests pass against canonical models. Run `uv run python scripts/verify_pm.py` to confirm live PM verify still passes against refreshed fixtures (~20 min runtime). Spot-check one fixture file to confirm `position_size` carries `quantity` / `dollar_value` and not `sector`.
