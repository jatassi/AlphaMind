# 04b — Pydantic-native discriminated unions for `PositionRecord` and `InstrumentSpec`

## Goal

Replace the hand-rolled discriminator validators on `PositionRecord` (`_check_discriminator`, [positions.py:158](http://positions.py:158)) and `InstrumentSpec` (`_validate_shape` / `_validate_equity_shape` / `_validate_options_shape` / `_validate_strategy_shape`, orders.py:101-154) with Pydantic v2's native `Discriminator` / `Tag` typing. The hand-rolled validators work but: (a) catch the mismatch *post-construct* rather than at parse time, (b) don't produce clean JSON Schema for downstream tools, (c) require maintaining the validator alongside the field set (drift risk), (d) are more verbose than the native primitive. Pydantic v2's discriminated-union pattern was introduced specifically for this case and produces tighter typing, better serialization round-trips, and earlier failure surfaces.

## Reading

* `src/alphamind/portfolio_state/records/positions.py:112-186` — current `PositionRecord` shape with `_check_discriminator` validator.
* `src/alphamind/portfolio_state/records/orders.py:87-154` — current `InstrumentSpec` with three `_validate_*_shape` validators.
* Pydantic v2 discriminated unions docs (current cutoff): `Annotated[Union[A, B, C], Field(discriminator="type")]` or `Discriminator` / `Tag` for callable discriminators.
* `src/alphamind/portfolio_state/records/orders.py:62-67` — `BracketLegType` enum used as discriminator key for the `BracketLeg.trigger` payload (story 03a uses Pydantic-native discriminator already; this story extends the same pattern to PositionRecord and InstrumentSpec).
* `tests/portfolio_state/records/test_positions.py::TestPositionRecordDiscriminator` — existing tests asserting the discriminator behavior; should continue to pass after migration.
* `tests/portfolio_state/records/test_orders.py::TestInstrumentSpecDiscriminator` — same, for InstrumentSpec.
* <issue id="7c6e5055-36fd-43b2-b7fe-575de26f99dc">ALP-345</issue> (story 03a) — already uses Pydantic-native discriminator on `BracketLeg.trigger`; pattern reference.

## Depends on

None. This story is technically independent of all other stories — it changes the validator implementation while preserving the public field shape. Recommended dispatch order: after wave 1 (additive field adds) and 04a (file moves) to minimize concurrent edits to the same lines.

## Scope

Source under `src/alphamind/portfolio_state/records/positions.py` and `src/alphamind/portfolio_state/records/orders.py`. Tests stay at their current locations; behavior contract is preserved (the same constructions succeed/fail).

### 1\. PositionRecord native discriminated union

Replace the current `equity_details / options_details / strategy_details` triple-Optional pattern with a discriminated union:

```python
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, Discriminator, Tag


class EquityPositionDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_type: Literal["EQUITY"] = "EQUITY"  # discriminator tag
    ticker: str
    share_count: float
    average_cost_basis_per_share: float
    borrow_rate_pct: float | None = None
    locate_status: LocateStatus | None = None
    margin_held_usd: float | None = None


class OptionsPositionDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_type: Literal["OPTIONS"] = "OPTIONS"
    underlying_ticker: str
    strike_price: float
    expiration_date: date
    contract_type: OptionContractType
    contract_count: float
    contract_multiplier: float
    premium_paid_per_contract: float
    greeks: OptionGreeks


class StrategyPositionDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_type: Literal["STRATEGY"] = "STRATEGY"
    strategy_type_label: str
    legs: tuple[StrategyLeg, ...]
    net_premium_usd: float
    max_profit_usd: float
    max_loss_usd: float
    breakeven_levels: tuple[float, ...]
    strategy_greeks: OptionGreeks


PositionDetailsPayload = Annotated[
    EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails,
    Field(discriminator="instrument_type"),
]


class PositionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    position_id: str
    thesis_id: str | None
    bracket_id: str | None
    status: PositionStatus
    direction: Direction
    entry_timestamp: datetime | None
    details: PositionDetailsPayload  # replaces three Optional fields
    # ... other fields unchanged
```

Removes the `instrument_type` separate field on `PositionRecord` (now derived from `details.instrument_type`) AND removes `equity_details`/`options_details`/`strategy_details` separate Optional fields (now consolidated under `details`).

This is a BREAKING SHAPE CHANGE. Every consumer that currently reads `position.equity_details.ticker` becomes `position.details.ticker` (with `isinstance(position.details, EquityPositionDetails)` for type narrowing). All 50+ consumers updated in lockstep.

The `_check_discriminator` validator is removed (Pydantic enforces the discriminator natively at parse time).

### 2\. InstrumentSpec native discriminated union

Same pattern. Replace the current `instrument_type` + Optional fields shape with a discriminated union:

```python
class EquityInstrumentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_type: Literal["EQUITY"] = "EQUITY"
    ticker: str

class OptionsInstrumentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_type: Literal["OPTIONS"] = "OPTIONS"
    underlying: str
    strike: float
    expiration: date
    contract_type: OptionContractType
    contract_multiplier: float

class StrategyInstrumentSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_type: Literal["STRATEGY"] = "STRATEGY"
    legs: tuple[OptionsInstrumentSpec, ...]  # tighter than current generic InstrumentSpec

InstrumentSpec = Annotated[
    EquityInstrumentSpec | OptionsInstrumentSpec | StrategyInstrumentSpec,
    Field(discriminator="instrument_type"),
]
```

The strategy spec's `legs` field is tightened from `tuple[InstrumentSpec, ...]` to `tuple[OptionsInstrumentSpec, ...]` (legs of a multi-leg strategy are always options; equity legs would never appear).

The `_validate_shape` and three sub-validators are removed.

### 3\. Backward compatibility

Hand-rolled-validator callers that rely on `position.equity_details is not None` style branching must migrate to `isinstance(position.details, EquityPositionDetails)`. There's no shim — the change is breaking and consumer migration is part of the story scope.

### Out of scope

* Migrating any computations or consumers outside the records files — those are subsumed under the consumer migration in §3.
* Adding additional discriminated unions to other records (e.g., the activity log's event-type discriminator currently uses a runtime mapping table; could be migrated but is a separate story).

## Acceptance criteria

- [ ] `EquityPositionDetails`, `OptionsPositionDetails`, `StrategyPositionDetails` carry `instrument_type: Literal[...]` discriminator tags.
- [ ] `PositionRecord.details: PositionDetailsPayload` field replaces the three Optional fields (`equity_details`, `options_details`, `strategy_details`) and the separate `instrument_type` field.
- [ ] `_check_discriminator` validator is removed from `PositionRecord`.
- [ ] Constructing `PositionRecord(..., details=EquityPositionDetails(...))` succeeds.
- [ ] Constructing `PositionRecord(..., details={"instrument_type": "EQUITY", "ticker": ...})` (dict, parses via discriminator) succeeds.
- [ ] Constructing `PositionRecord(..., details={"instrument_type": "BOGUS", ...})` raises `ValidationError` at parse time (before any post-construct validator runs).
- [ ] `EquityInstrumentSpec`, `OptionsInstrumentSpec`, `StrategyInstrumentSpec` carry `instrument_type` discriminator tags.
- [ ] `InstrumentSpec` is a discriminated union of the three.
- [ ] `_validate_shape` and the three `_validate_*_shape` validators are removed from `InstrumentSpec`.
- [ ] All existing `uv run pytest -n auto` tests pass after consumer migration.
- [ ] `tests/portfolio_state/records/test_positions.py::TestPositionRecordDiscriminator` continues to pass (asserts the same behavior contract).
- [ ] `tests/portfolio_state/records/test_orders.py::TestInstrumentSpecDiscriminator` continues to pass.
- [ ] `PositionRecord.model_json_schema()` includes a discriminator clause keyed on `instrument_type` under `details` (Pydantic native).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean across the repo.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus the migrated discriminator tests.
* Spot-check JSON Schema: `python -c "import json; from alphamind.portfolio_state.records.positions import PositionRecord; print(json.dumps(PositionRecord.model_json_schema(), indent=2)['$defs'].keys())"` — confirm Pydantic-native discriminator output.
* Spot-check parse-time discriminator failure: `python -c "from alphamind.portfolio_state.records.positions import PositionRecord; PositionRecord.model_validate({'details': {'instrument_type': 'BOGUS'}, ...})"` should raise `ValidationError` early in parse.
* Lint clean per CLAUDE.md.