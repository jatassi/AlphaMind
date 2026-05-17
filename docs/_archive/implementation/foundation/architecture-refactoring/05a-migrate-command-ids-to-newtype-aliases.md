# 05a — Migrate command IDs to NewType aliases

## Goal

Apply the `NewType` aliases created in story 04 to the identifier fields across `decision/`, `execution/`, `portfolio_state/`, and `commands/`. After this story, every identifier field in a function signature, dataclass field, or Pydantic model uses its `NewType` alias (`OrderId`, `PositionId`, `EnvelopeId`, `Symbol`, etc.) rather than bare `str`; mypy --strict catches ID-confusion bugs at type-check time.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L4 — identifier-typing motivation
* Story 04 (<issue id="c0c8bfb2-285e-4624-9d1d-7f1b6c8a9e7b">ALP-460</issue>) — defines the 11 NewType aliases this story applies
* `src/alphamind/commands/command_models.py` — main boundary file; every command variant has identifier fields
* `src/alphamind/portfolio_state/records/{positions,orders,theses}.py` — internal identifier fields
* `src/alphamind/execution/oms/command_ids.py` — `PMCommandIdComponents` / `EngineCommandIdComponents` parsers; use `EnvelopeId` and `CommandId` after migration
* `src/alphamind/decision/portfolio_manager/models.py:222-259` — `PMAnalystEnvelope`, `PMStrategistEnvelope` carry `envelope_id`, `source_recommendation_id`, `position_id`, `invocation_id` as bare `str`

## Depends on

* 04 (<issue id="c0c8bfb2-285e-4624-9d1d-7f1b6c8a9e7b">ALP-460</issue>) — NewType aliases + constructors must exist
* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — `commands/` package must exist with `command_models.py` so its identifier fields can migrate

## Scope

In scope: every identifier field in Pydantic models, frozen dataclasses, and function signatures across `commands/`, `decision/`, `execution/`, `portfolio_state/records/`. Tests update as fields retype.

### 1\. Boundary models in `commands/`

`commands/command_models.py` — `OMSCommand` variants carry IDs:

* Every `position_id: str` → `position_id: PositionId`
* Every `order_id: str` → `order_id: OrderId`
* Every `bracket_id: str` → `bracket_id: BracketId`
* Every `command_id: str` → `command_id: CommandId`
* Symbol fields → `Symbol` or `OccSymbol` (the OCC symbol is its own type; pure equity Symbol is the default)

Pydantic accepts NewType aliases; for stricter validation use `Field(pattern=...)` only at the boundary entry point (where the regex was already), then internal code trusts the type.

### 2\. Decision-layer envelope models

`decision/portfolio_manager/models.py:222-259` — `PMAnalystEnvelope`, `PMStrategistEnvelope`:

* `envelope_id: str = Field(pattern=...)` → `envelope_id: EnvelopeId` (Pydantic validator calls `envelope_id(value)` constructor from `_kernel.ids`)
* `source_recommendation_id: str` → its own NewType (add `RecommendationId` to `_kernel/ids.py` in this story if not already there)
* `position_id: str` → `position_id: PositionId`
* `invocation_id: str` → `invocation_id: InvocationId`

Similar migration for analyst/strategist output schemas (`analyst/models.py`, `strategist/models.py`).

### 3\. Execution-layer parsers

`execution/oms/command_ids.py` — `PMCommandIdComponents.envelope_id: str` → `EnvelopeId`. `EngineCommandIdComponents.command_id: str` → `CommandId`. Parser functions (`parse_pm_command_id`, `parse_engine_command_id`) take `str` and return components whose ID fields are NewType.

### 4\. Portfolio-state records

`portfolio_state/records/positions.py`, `orders.py`, `theses.py`, etc. — Pydantic model fields:

* `position_id: str` → `PositionId`
* `order_id: str` → `OrderId`
* `thesis_id: str` → `ThesisId`
* `symbol: str` → `Symbol` (assets) or `OccSymbol` (options)

Note: this story migrates only the identifier fields, not the surrounding Pydantic→dataclass conversion (that's story 10d).

### 5\. Broker adapter

`execution/broker_adapter/` — Alpaca order IDs surface as `AlpacaOrderId`, internal references as `OrderId`. The mapping happens at the broker boundary (when receiving fill updates / order acks).

### Out of scope

Money migration is story 05b. Pydantic→dataclass conversion is Wave 10. This story converts ID fields only; the surrounding model machinery stays as it is.

## Acceptance criteria

- [ ] Every `position_id`, `order_id`, `bracket_id`, `command_id`, `envelope_id`, `invocation_id`, `thesis_id`, `symbol` field in `src/alphamind/commands/`, `decision/portfolio_manager/`, `execution/oms/`, `execution/broker_adapter/`, `portfolio_state/records/` uses its NewType alias from `alphamind._kernel.ids` rather than bare `str`.
- [ ] Boundary regex validation moves from per-field `Field(pattern=...)` to the constructor functions in `_kernel/ids.py` (called from the model's validator if Pydantic requires it).
- [ ] `mypy --strict` flags an error for synthetic test `def f(o: PositionId): ... ; f(some_envelope_id)`.
- [ ] `uv run lint-imports` passes.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

Spot-check: `grep -rn "position_id: str\|order_id: str\|envelope_id: str" src/alphamind/{commands,decision/portfolio_manager,execution/oms,portfolio_state/records}/` returns zero hits. Test that introducing a deliberate type confusion (e.g., passing an `EnvelopeId` to a function expecting `PositionId`) fails `mypy --strict`.