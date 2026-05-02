---
status: done
completed_date: 2026-04-27
commit_id: b16f171
---

# 03c — Order and bracket records

## Goal

Define the consumer-facing typed records for orders and brackets at `src/alphamind/portfolio_state/records/orders.py`, with all field constraints validated via Pydantic v2 and a corresponding test suite at `tests/portfolio_state/records/test_orders.py`. Covers the pending-order surface (raw state category 4b) and the bracket-binding records that join orders to theses and track protective leg state.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` — sections 4b (pending orders) and 5c (position modification trail) for the order-modification context; section 4b defines the `age_hours` semantics the strategist consumes
- `../../../design/05-execution-layer/orders-and-brackets.md` — order vocabulary (atomic, contingent, instrument-specific), mandatory thesis-linked bracket structure, three invalidation types, hard-backstop requirement, P/L-based bracket legs, fill-anchor recalculation, bracket modification, corporate action handling
- `../../../design/05-execution-layer/state-persistence.md` § Orders, § Brackets — authoritative persistence-layer field list: order_id, position_id, bracket_id, order role, instrument specification, direction, order type, quantity, price parameters, status, duration, Alpaca order ID chain, modification count, protective leg structure, bracket status, modification history
- `02-package-skeleton-and-config.md` — the package layout this story populates; `src/alphamind/portfolio_state/records/orders.py` is listed as story 03c's target
- `03a-position-records.md` — parallel sibling; `InstrumentType` is declared in `positions.py` and should be imported by `orders.py` rather than redeclared (cross-import within `records/`)
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: frozen Pydantic v2, StrEnum discriminators, validation tests, in-scope/out-of-scope split, AC style

## Depends on

- 02
- 03a (imports `InstrumentType` from `positions.py`)

## Scope

In scope:

1. **Enums** — all `StrEnum`, members in ALL CAPS:

   - `OrderRole`: `ENTRY`, `TAKE_PROFIT`, `PRICE_STOP`, `TIME_STOP`, `CLOSE`, `ADD_ENTRY` — per state-persistence.md "order role: entry, take-profit, price-stop, time-stop, close, add-entry"
   - `OrderType`: `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT` — per state-persistence.md and orders-and-brackets.md Tier 1
   - `OrderDirection`: `BUY`, `SELL`, `BUY_TO_OPEN`, `SELL_TO_OPEN`, `BUY_TO_CLOSE`, `SELL_TO_CLOSE` — per state-persistence.md and orders-and-brackets.md Tier 3 (options)
   - `OrderDuration`: `DAY`, `GTC`, `GTD`, `IOC`, `FOK` — per state-persistence.md "duration: day, GTC, GTD, IOC, FOK"
   - `OrderStatus`: `PENDING`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `EXPIRED`, `REJECTED` — per state-persistence.md "status: pending, partially-filled, filled, cancelled, expired, rejected"
   - `BracketStatus`: `PENDING_ENTRY`, `ACTIVE`, `COMPLETED`, `DISSOLVED` — per state-persistence.md "status: pending-entry, active, completed, dissolved"
   - `BracketLegType`: `TAKE_PROFIT`, `PRICE_STOP`, `TIME_EXPIRATION`, `EVENT_INVALIDATION` — per state-persistence.md "leg type: take-profit, price-stop, time-expiration, event-invalidation". **Canonical declaration site.** The thesis records story (03b) imports this from `orders.py`. If 03b landed first with a duplicate declaration, remove the duplicate from `theses.py` and import from here.
   - `BracketLegEnforcement`: `MECHANICAL`, `ADVISORY` — per state-persistence.md "enforcement: mechanical or advisory"
   - `BracketLegStatus`: `PENDING_ACTIVATION`, `ACTIVE`, `TRIGGERED`, `CANCELLED` — per state-persistence.md "status: pending-activation, active, triggered, cancelled"

2. **Value objects** — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`:

   - `InstrumentSpec` — discriminated by `instrument_type: InstrumentType` (imported from `positions.py`). One typed shape per instrument class; a unified model with Optional fields per type is acceptable if the discriminator-validator enforces exactly one shape populated:
     - EQUITY shape: `ticker: str`
     - OPTIONS shape: `underlying: str`, `strike: float`, `expiration: date`, `contract_type: OptionContractType` (imported from `positions.py`), `contract_multiplier: float`
     - STRATEGY shape: `legs: tuple[InstrumentSpec, ...]` (recursive — each leg is itself an InstrumentSpec; non-empty)
     - Validator: for EQUITY, equity-only fields populated and options/strategy fields absent; for OPTIONS, options-only fields populated; for STRATEGY, `legs` non-empty and each leg individually valid. Mismatched or ambiguously-populated spec raises `ValidationError`.

   - `PriceParameters` — `limit_price: float | None`, `stop_trigger_price: float | None`. Validated against `order_type` (passed as a constructor argument or via a `model_validator` that receives `order_type` from the parent `OrderRecord`):
     - `MARKET` ⇒ both `None`
     - `LIMIT` ⇒ `limit_price` non-`None`, `stop_trigger_price` `None`
     - `STOP` ⇒ `stop_trigger_price` non-`None`, `limit_price` `None`
     - `STOP_LIMIT` ⇒ both non-`None`
     - Validation is enforced at the `OrderRecord` level (where `order_type` is available) rather than inside `PriceParameters` itself. `PriceParameters` is a plain container; `OrderRecord` cross-validates the pair.

   - `OrderRecord` — the consumer-facing per-order record:
     - Identity / binding: `order_id: str`, `position_id: str | None` (null for entry orders rejected before position creation), `bracket_id: str`
     - Role / classification: `role: OrderRole`, `instrument_spec: InstrumentSpec`, `direction: OrderDirection`, `order_type: OrderType`
     - Execution parameters: `price_parameters: PriceParameters`, `quantity: float`, `duration: OrderDuration`
     - Status: `status: OrderStatus`
     - Alpaca linkage: `alpaca_order_id: str`, `alpaca_order_id_chain: tuple[str, ...]` (ordered audit trail of replacements; `alpaca_order_id` is the most recent entry — see note below)
     - Timestamps: `submission_timestamp: datetime` (tz-aware UTC), `last_update_timestamp: datetime` (tz-aware UTC)
     - Fill state: `filled_quantity: float` (cumulative across all partials), `avg_fill_price: float | None` (None until first fill), `remaining_quantity: float`
     - History: `modification_count: int`
     - Provenance: `originating_thesis_id: str | None`, `originating_pm_command_id: str | None`
     - Consumer-facing computed: `age_hours: float` (hours since `submission_timestamp`; computed at delivery by the assembler — story 06; shape declared here per portfolio-state.md § 4b "Order age: hours since placement")

   - `BracketLegModification` — one entry per modification event in the bracket's modification history:
     - `timestamp: datetime` (tz-aware UTC)
     - `pm_command_id: str | None` (null for fill-anchor recalculation, non-null for PM-initiated modifications)
     - `source: str` — one of `"pm"`, `"fill-anchor-recalculation"`, `"corporate_action_adjustment"`
     - `field_changed: str`
     - `old_value: str`
     - `new_value: str`
     - `rationale: str`

   - `BracketLeg` — one leg definition within a bracket's protective set:
     - `leg_id: str`
     - `leg_type: BracketLegType`
     - `order_id: str | None` (null for `EVENT_INVALIDATION` legs, which have no implementing order per orders-and-brackets.md "event-based invalidation … the engine cannot enforce mechanically")
     - `trigger_condition: str` (price level, timestamp, or qualitative event description rendered as a stable string)
     - `enforcement: BracketLegEnforcement`
     - `status: BracketLegStatus`
     - `pl_based: bool` (True if this leg uses P/L-based anchoring per orders-and-brackets.md § P/L-based bracket legs; False for absolute-price legs)

   - `BracketRecord` — the consumer-facing per-bracket record:
     - `bracket_id: str`
     - `position_id: str`
     - `status: BracketStatus`
     - `entry_order_id: str`
     - `protective_legs: tuple[BracketLeg, ...]` (non-empty; validated below)
     - `modification_history: tuple[BracketLegModification, ...]` (ordered chronologically; may be empty for unmodified brackets)
     - `corporate_action_cancellation_reason: str | None` (non-null when the bracket was cancelled due to a corporate action per orders-and-brackets.md § Corporate action handling)

3. **Validation rules** (enforced via Pydantic `model_validator`):

   - `PriceParameters` cross-validation (in `OrderRecord`): `order_type` determines which of `limit_price` / `stop_trigger_price` must be non-`None` vs. `None` per the four-case table above. Violation raises `ValidationError`.
   - `quantity > 0`; `filled_quantity >= 0`; `remaining_quantity >= 0`; `modification_count >= 0`. Each violating value raises `ValidationError`.
   - Quantity invariant: for orders where `status` is not `REJECTED` and not `CANCELLED`, `filled_quantity + remaining_quantity == quantity`. For `REJECTED`: `filled_quantity == 0` (no fills ever occurred), `remaining_quantity == quantity`. For `CANCELLED` after partial fills: `remaining_quantity == quantity - filled_quantity` (i.e., the invariant still holds; `CANCELLED` does not exempt the equality — the exemption clause in the AC is about whether the sum *must reach* `quantity`, not whether the equality holds).
   - `alpaca_order_id_chain` non-empty; `alpaca_order_id` equals `alpaca_order_id_chain[-1]` (the most recent ID in the chain is the current active ID).
   - `age_hours >= 0`.
   - `BracketRecord.protective_legs` non-empty per orders-and-brackets.md "hard backstop requirement."
   - Hard-backstop rule: at least one leg in `protective_legs` must have `enforcement == MECHANICAL` and `leg_type` in `{TAKE_PROFIT, PRICE_STOP, TIME_EXPIRATION}`. A bracket with only `EVENT_INVALIDATION` legs or only `ADVISORY` legs fails validation.
   - `status == PENDING_ENTRY` ⇒ all `protective_legs` have `status == PENDING_ACTIVATION`.
   - `status == DISSOLVED` ⇒ all `protective_legs` have `status == CANCELLED`.
   - `InstrumentSpec` STRATEGY shape: `legs` non-empty.
   - `BracketLeg` with `leg_type == EVENT_INVALIDATION` ⇒ `order_id` is `None`; with any other `leg_type` ⇒ `order_id` may be non-`None` (not required to be non-`None`, since the order may not yet exist for a pending-activation leg).

4. **Tests** at `tests/portfolio_state/records/test_orders.py`:

   - Each enum: correct member set, correct string values, immutability (assignment to member attribute raises `AttributeError` or `ValidationError` as applicable).
   - `PriceParameters` / `OrderRecord` price cross-validation:
     - MARKET with both prices `None` passes.
     - LIMIT with `limit_price` non-`None` and `stop_trigger_price` `None` passes.
     - STOP with `stop_trigger_price` non-`None` and `limit_price` `None` passes.
     - STOP_LIMIT with both non-`None` passes.
     - LIMIT missing `limit_price` fails.
     - STOP missing `stop_trigger_price` fails.
     - MARKET with `limit_price` set fails.
     - STOP_LIMIT missing either price fails.
   - Quantity invariant:
     - Non-terminal status with `filled + remaining == quantity` passes.
     - Non-terminal status with `filled + remaining != quantity` fails.
     - `REJECTED` with `filled_quantity == 0` and `remaining_quantity == quantity` passes.
     - `CANCELLED` with partial fill where `remaining == quantity - filled` passes.
   - `alpaca_order_id_chain` non-empty rule: empty tuple fails.
   - `alpaca_order_id` equals `alpaca_order_id_chain[-1]`: mismatch fails.
   - `age_hours < 0` fails.
   - `quantity <= 0` fails; `filled_quantity < 0` fails; `remaining_quantity < 0` fails; `modification_count < 0` fails.
   - `BracketRecord.protective_legs` empty fails.
   - Hard-backstop rule: bracket with only `EVENT_INVALIDATION`/`ADVISORY` legs fails; bracket with at least one `MECHANICAL` + mechanical-type leg passes.
   - `PENDING_ENTRY` with a leg not in `PENDING_ACTIVATION` fails; all legs in `PENDING_ACTIVATION` passes.
   - `DISSOLVED` with a leg not in `CANCELLED` fails; all legs `CANCELLED` passes.
   - `InstrumentSpec` STRATEGY with empty `legs` fails; non-empty passes.
   - `EVENT_INVALIDATION` leg with non-`None` `order_id` fails; with `None` passes.
   - `InstrumentSpec` discriminator: EQUITY with equity fields passes; EQUITY with options fields fails; multiple shape fields simultaneously populated fails.

Out of scope:

- Persistence schema for orders/brackets (state-persistence story set).
- Order submission and lifecycle management (broker-adapter, OMS commands story sets).
- Multi-leg strategy order submission (`order_class: mleg`) — that's broker-adapter; this story models the read-side record only.
- Fill records (raw state category — fills are part of the activity log; story 03e covers position_modification_trail filtered views).
- The fill-probability context the strategist sees in 4b (`current price vs. limit`) — that is a computed field added by the assembler (story 06) using current price plus the order's `PriceParameters`; not part of the record itself.

## Notes

Per `feedback_no_inventing_component_names.md`, every type name, enum member, and field name is sourced directly from `orders-and-brackets.md` and `state-persistence.md`. No new field names are introduced here.

`BracketLegType` is canonically declared in `orders.py` because orders/brackets is the structural owner. The thesis records story (03b) imports `BracketLegType` from `alphamind.portfolio_state.records.orders` to cross-reference thesis components to bracket legs. If 03b was implemented first and contains a duplicate `BracketLegType`, treat `orders.py` as the convergence target, remove the duplicate from `theses.py`, and import from here.

`BracketLeg.pl_based` distinguishes target/profit-management legs that anchor to the actual fill price (per orders-and-brackets.md § P/L-based bracket legs: "P/L targets express the thesis's risk/reward structure … anchoring to planned entry would silently distort intended risk/reward") from absolute-price legs that do not recalculate. The flag matters for `modification_history` accounting: `source == "fill-anchor-recalculation"` entries in `BracketRecord.modification_history` are generated only when a `pl_based` leg is recalculated after entry fill; absolute-price legs produce no such entries.

`OrderRecord.age_hours` matches the strategist's context per portfolio-state.md § 4b: "Order age: hours since placement. The strategist flags orders older than their thesis's expected entry window." The field is consumer-facing and computed at delivery by the assembler; the shape is declared here so the type contract is stable. Per `feedback_avoid_numeric_anchors.md`, no field docstring imposes thresholds — downstream interpretation is not the record's job.

`InstrumentType` and `OptionContractType` are imported from `positions.py` (story 03a) rather than re-declared. This keeps the cross-import within the `records/` subpackage and avoids duplicate definitions. The `InstrumentSpec` here serves the order-side instrument reference; the `EquityPositionDetails` / `OptionsPositionDetails` in `positions.py` serve the position-side instrument detail. They reference the same discriminator enum values.

`alpaca_order_id_chain` is a `tuple[str, ...]` ordered chronologically — the earliest assigned Alpaca ID at index 0, the current active ID at the last index. `alpaca_order_id` is always equal to `alpaca_order_id_chain[-1]`, which the validator enforces. This mirrors state-persistence.md: "Alpaca order ID chain: ordered array of all Alpaca order IDs this logical order has held across replacements, for audit trail on modified orders."

`CANCELLED` orders with prior partial fills satisfy `remaining_quantity == quantity - filled_quantity` — the quantity invariant holds in all non-`REJECTED` cases. `REJECTED` orders have `filled_quantity == 0` and `remaining_quantity == quantity` because no fills ever occurred.

Per `feedback_per_producer_schema.md`, the equity/options/strategy shape split in `InstrumentSpec` via discriminator is the per-producer pattern. A unified `InstrumentSpec` with Optional fields per instrument type and a discriminator-validator that enforces exactly one shape populated is an acceptable implementation of this pattern.

## Acceptance criteria

- [ ] `OrderRole`, `OrderType`, `OrderDirection`, `OrderDuration`, `OrderStatus`, `BracketStatus`, `BracketLegType`, `BracketLegEnforcement`, and `BracketLegStatus` each define exactly the documented members.
- [ ] `InstrumentSpec`, `PriceParameters`, `OrderRecord`, `BracketLegModification`, `BracketLeg`, and `BracketRecord` are frozen Pydantic v2 models with the documented field sets.
- [ ] `PriceParameters` cross-validation (enforced at `OrderRecord` level) passes for all four MARKET/LIMIT/STOP/STOP_LIMIT cases and fails for each documented violation.
- [ ] Quantity invariant `filled_quantity + remaining_quantity == quantity` enforced for non-terminal statuses; `REJECTED` ⇒ `filled_quantity == 0`; `CANCELLED` partial-fill semantics documented and tested.
- [ ] `quantity > 0`, `filled_quantity >= 0`, `remaining_quantity >= 0`, `modification_count >= 0` — each violation has a failing test.
- [ ] `alpaca_order_id_chain` non-empty; `alpaca_order_id == alpaca_order_id_chain[-1]` — both rules have passing and failing tests.
- [ ] `age_hours >= 0` — violation has a failing test.
- [ ] `BracketRecord.protective_legs` non-empty — violation has a failing test.
- [ ] Hard-backstop rule (at least one `MECHANICAL` leg of type `TAKE_PROFIT`, `PRICE_STOP`, or `TIME_EXPIRATION`) enforced — violation has a failing test.
- [ ] `PENDING_ENTRY` ⇒ all protective legs `PENDING_ACTIVATION` — rule has passing and failing tests.
- [ ] `DISSOLVED` ⇒ all protective legs `CANCELLED` — rule has passing and failing tests.
- [ ] `InstrumentSpec` discriminator: EQUITY/OPTIONS/STRATEGY each pass with correct fields; mismatch or multiple-shape violation fails.
- [ ] `EVENT_INVALIDATION` leg with non-`None` `order_id` fails; `None` passes.
- [ ] `BracketLegType` is importable by `theses.py` from `alphamind.portfolio_state.records.orders`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
