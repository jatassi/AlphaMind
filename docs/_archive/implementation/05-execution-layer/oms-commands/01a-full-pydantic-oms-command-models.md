# 01a — Full Pydantic OMS command models

## Goal

Land the canonical broker-grade Pydantic models for the five OMS command types (OPEN / CLOSE / ADJUST / CANCEL / ADD) at `src/alphamind/execution/oms/command_models.py`. Translates `docs/design/05-execution-layer/oms-command-schema.md` to a discriminated-union Pydantic shape with all required + optional fields, full sub-records, and per-design `model_validator`s for cross-field invariants. Downstream consumers in this work tree (story 02a engine envelope, story 02b PM coordinated edit, story 03 engine-stub upgrade) import from this module. Also delete the empty `src/alphamind/execution/oms_commands/` skeleton per parent decision (A) — `oms/` is the canonical home.

## Reading

* `docs/design/05-execution-layer/oms-command-schema.md` — the entire JSON Schema (Draft 2020-12) is the contract; every `$defs` entry has a Pydantic counterpart in this story
* `docs/design/05-execution-layer/oms-commands.md` § OPEN / CLOSE / ADJUST / CANCEL / ADD — prose semantics for each command's required parameters and design intent
* `docs/design/05-execution-layer/oms-commands.md` § Notes on cross-field invariants — invariants enforced "by the validation pipeline, OMS command intake layer, or guardrail layer (not expressible in JSON Schema)"; story 01a implements the structurally-expressible subset via `model_validator`
* `src/alphamind/decision/portfolio_manager/oms_command_models.py` — the transitional minimal shape; story 01a's models supersede this. Note the `tactical_exit` literal that should be `conviction_reduced` per design (parent decision (E))
* `src/alphamind/decision/portfolio_manager/models.py` — pattern for Pydantic discriminated-union envelopes with `model_validator(mode="after")` invariants (`pm_analyst_envelope` / `pm_strategist_envelope`)
* `src/alphamind/decision/strategist/models.py` lines around `close_rationale_type` — mirrors the design's four-value enum (`thesis_invalidated` / `target_reached` / `conviction_reduced` / `risk_management`); the canonical `CloseCommand` matches this enum exactly
* `docs/design/05-execution-layer/orders-and-brackets.md` — semantics of price-trigger / time-trigger / event-invalidation legs and the hard-backstop requirement (`at least one is_hard=true`)

## Depends on

(No same-tree blockers — Wave 1.)

## Scope

In scope, all under `src/alphamind/execution/oms/`. Tests at `tests/execution/oms/`.

### 1\. `command_models.py`

Author the module exporting:

* **Vocabulary literals** — `CommandType = Literal["open", "close", "adjust", "cancel", "add"]`; `AssetType = Literal["equity", "option", "strategy"]`; `Direction = Literal["long", "short"]`; `ContractType = Literal["call", "put"]`; `EntryOrderType = Literal["market", "limit", "stop_limit"]`; `TargetType = Literal["absolute_price", "pl_percentage", "pl_dollar"]`; `BracketOrderType = Literal["market", "limit", "stop", "stop_limit"]`; `LegType = Literal["price", "time", "event"]`; `Comparator = Literal["<", ">=", "<", ">"]`; `CloseRationaleType = Literal["thesis_invalidated", "target_reached", "conviction_reduced", "risk_management"]` (matches design — no `tactical_exit`); `RiskManagementSubtype = Literal["pm_directed", "engine_guardrail"]`; `ComponentType = Literal["entry_rationale", "target_rationale", "invalidation_rationale"]`; `StrategyType = Literal["vertical_spread", "calendar_spread", "straddle", "strangle", "iron_condor", "custom"]`; `AdjustmentCategory` and similar where the schema specifies an enum.
* **Instrument variants (discriminated on** `asset_type`) — `EquityInstrument(asset_type, ticker, direction)`; `OptionInstrument(asset_type, underlying, strike, expiration, contract_type, direction)`; `StrategyInstrument(asset_type, strategy_type, underlying, legs: tuple[StrategyLeg, ...])`. `StrategyLeg(strike, expiration, contract_type, direction, quantity_ratio)`. `Instrument` is `Annotated[EquityInstrument | OptionInstrument | StrategyInstrument, Discriminator("asset_type")]`. Strategy `legs` enforces `Field(min_length=2)` per `minItems: 2`.
* **Order / sizing / target sub-records** — `EntryOrder(type, limit_price, stop_price)` with `model_validator(mode="after")` enforcing `limit` ⇒ `limit_price`, `stop_limit` ⇒ both. `PositionSize(quantity, dollar_value, premium_at_risk)` — **no** `sector` field per parent decision (B). `Target(target_type, price, pl_percentage, pl_dollar, order_type)` with `model_validator` enforcing the design's three conditional shapes (`absolute_price` ⇒ `price`; `pl_percentage` ⇒ `pl_percentage` + `price`; `pl_dollar` ⇒ `pl_dollar` + `price`).
* **Invalidation legs (discriminated on** `type`) — `PriceLeg(type, is_hard, condition: PriceCondition, order_parameters: BracketOrderParameters)` with `is_hard=True` enforced; `TimeLeg(type, is_hard, condition: TimeCondition, order_parameters)` with `is_hard=True` enforced; `EventLeg(type, is_hard, condition: EventCondition)` with `is_hard=False` enforced and no `order_parameters`. `PriceCondition(underlying_trigger, comparator, trigger_price)`; `TimeCondition(deadline)`; `EventCondition(event_description)`. `BracketOrderParameters(order_type, limit_price)`. `InvalidationLeg = Annotated[PriceLeg | TimeLeg | EventLeg, Discriminator("type")]`.
* **Thesis sub-records** — `ThesisComponent(component_type, linked_leg, instrument_reference, narrative, key_assumptions: tuple[str, ...])`; `Thesis(summary, components: tuple[ThesisComponent, ...])` with `Field(min_length=1)` on components.
* **Five command variants (discriminated on** `command_type`):
  * `OpenCommand(command_id?, command_type, instrument, entry_order, position_size, target, invalidation_legs, thesis)` with `model_validator` enforcing **at least one** `is_hard=True` leg in `invalidation_legs` (per the schema's `description`).
  * `CloseCommand(command_id?, command_type, position_id, quantity, order_type, limit_price?, close_rationale_type, invalidation_reason?, risk_management_subtype?)` where `quantity` is `Annotated[float | Literal["all"], Field(strict=False)]` (`exclusiveMinimum: 0` for numeric). `model_validator` enforces: `order_type=="limit"` ⇒ `limit_price`; `close_rationale_type=="thesis_invalidated"` ⇒ `invalidation_reason`; `close_rationale_type=="risk_management"` ⇒ `risk_management_subtype`; `close_rationale_type=="conviction_reduced"` ⇒ `quantity != "all"`.
  * `AdjustCommand(command_id?, command_type, position_id, adjustment_rationale, new_stop_level?, new_target_level?, new_time_expiration?, new_event_invalidation?, thesis_component_updates?)` with `model_validator` enforcing **at least one** of the five optional change-fields is set (the design's `anyOf`). `NewStopLevel(trigger_price, order_type, limit_price?)`; `NewTargetLevel(target_type?, price?, pl_percentage?, pl_dollar?, order_type)`; `NewEventInvalidation(event_description)`.
  * `CancelCommand(command_id?, command_type, order_id, cancel_reason)`.
  * `AddCommand(command_id?, command_type, position_id, additional_quantity, additional_dollar_value, entry_order, thesis_addition_component, bracket_adjustment?)` where `thesis_addition_component` is constrained to `component_type == "entry_rationale"` (design's `allOf`); `BracketAdjustment(new_stop_level?, new_target_level?, new_time_expiration?)` with `model_validator` enforcing **at least one** is set.
* **Discriminated union** — `OMSCommand = Annotated[OpenCommand | CloseCommand | AdjustCommand | CancelCommand | AddCommand, Discriminator("command_type")]`.
* **Schema-export accessor** — `def oms_command_schema() -> dict[str, Any]` returns the JSON Schema dict via `TypeAdapter(OMSCommand).json_schema()`. Mirrors the PM's `envelope_schema()` accessor pattern.
* `command_id` — every command variant has an optional `command_id: str | None = None` field with the design's regex pattern documented in the docstring (PM-originated `^inv-[^.]+\.ENV-(REC|SA|SA-ORD)-[0-9]+\.[0-9]+\.[0-9]+$` or engine-originated `^MON\.[^.]+\.[0-9]+\.[0-9]+$`). Field-level pattern enforcement is deferred to story 03's intake (the LLM produces envelopes with `command_id=None`; the OMS fills it).

### 2\. `__init__.py`

Re-export the public surface from `command_models.py` so callers can `from alphamind.execution.oms import OMSCommand, OpenCommand, ...`. Preserve existing engine-stub re-exports (`SubmissionResult`, etc.) — append, don't replace.

### 3\. Delete empty `src/alphamind/execution/oms_commands/`

The empty package skeleton at `src/alphamind/execution/oms_commands/` (with empty `__init__.py`) is removed per parent decision (A). Also delete `tests/execution/oms_commands/` (empty test skeleton).

### 4\. Tests at `tests/execution/oms/test_command_models.py`

Cover for each variant: happy-path construction, frozen/immutable model_config, discriminator round-trip via `TypeAdapter[OMSCommand]`, every `model_validator` invariant (each conditional rule has a positive case + a negative case raising `ValidationError` with a recognizable error message). Cover the schema-export accessor returns a valid JSON Schema dict with the discriminated `oneOf` shape.

### Out of scope

* Command-ID derivation utility — story 01b
* Engine envelope models — story 02a
* PM's coordinated edit removing the transitional `oms_command_models.py` — story 02b
* Refining the engine-stub `submit_envelope_mcp.py` to use canonical models — story 03
* Layer-2/3 cross-envelope or cross-command validators (those live in PM and stay there) — out of this work tree's scope

## Acceptance criteria

- [ ] `src/alphamind/execution/oms/command_models.py` exists and exports `OMSCommand`, `OpenCommand`, `CloseCommand`, `AdjustCommand`, `CancelCommand`, `AddCommand`, `Instrument`, `EquityInstrument`, `OptionInstrument`, `StrategyInstrument`, `EntryOrder`, `PositionSize`, `Target`, `InvalidationLeg`, `PriceLeg`, `TimeLeg`, `EventLeg`, `Thesis`, `ThesisComponent`, `oms_command_schema`, plus all literal aliases.
- [ ] `PositionSize` has `quantity`, `dollar_value`, `premium_at_risk` fields and **no** `sector` field (parent decision (B)).
- [ ] `CloseCommand.close_rationale_type` Literal is `"thesis_invalidated" | "target_reached" | "conviction_reduced" | "risk_management"` (no `tactical_exit`, parent decision (E)).
- [ ] Every Pydantic model uses `model_config = ConfigDict(frozen=True)` (immutability invariant).
- [ ] `TypeAdapter(OMSCommand).validate_python(...)` round-trips each variant by `command_type` discriminator.
- [ ] `OpenCommand` raises `ValidationError` when `invalidation_legs` contains zero `is_hard=True` legs.
- [ ] `CloseCommand` raises `ValidationError` for: `order_type="limit"` without `limit_price`; `close_rationale_type="thesis_invalidated"` without `invalidation_reason`; `close_rationale_type="risk_management"` without `risk_management_subtype`; `close_rationale_type="conviction_reduced"` with `quantity="all"`.
- [ ] `AdjustCommand` raises `ValidationError` when none of `new_stop_level` / `new_target_level` / `new_time_expiration` / `new_event_invalidation` / `thesis_component_updates` is set.
- [ ] `AddCommand.thesis_addition_component.component_type` is constrained to `"entry_rationale"` (raises on other values).
- [ ] `EntryOrder` raises `ValidationError` for: `type="limit"` without `limit_price`; `type="stop_limit"` without both prices.
- [ ] `Target` raises `ValidationError` for: `target_type="absolute_price"` without `price`; `target_type="pl_percentage"` without `pl_percentage` + `price`; `target_type="pl_dollar"` without `pl_dollar` + `price`.
- [ ] `PriceLeg` and `TimeLeg` enforce `is_hard=True`; `EventLeg` enforces `is_hard=False` and disallows `order_parameters`.
- [ ] `StrategyInstrument.legs` raises `ValidationError` when fewer than two legs are supplied.
- [ ] `oms_command_schema()` returns a dict with `oneOf` over the five variants and a `discriminator` block keyed on `command_type`.
- [ ] `src/alphamind/execution/oms_commands/` directory is removed; `tests/execution/oms_commands/` is removed.
- [ ] `tests/execution/oms/test_command_models.py` covers every acceptance criterion above and passes under `uv run pytest tests/execution/oms/test_command_models.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/execution/oms/test_command_models.py -n auto` to confirm the model surface. Spot-check `oms_command_schema()` output against `docs/design/05-execution-layer/oms-command-schema.md` § Schema — every `$defs` entry should appear in the generated schema's `$defs` (or as inline definitions). Confirm `src/alphamind/execution/oms_commands/` is absent via `ls src/alphamind/execution/`.
