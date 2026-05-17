# 05b — Migrate boundary money to Money/Price (Decimal at boundary)

## Goal

Migrate every monetary field in boundary types from `float` to `Money`/`Price` (Decimal-backed `NewType` from story 04). Parse Alpaca's string-returned monetary fields once at the boundary via `money(broker_str)`. After this story, internal monetary arithmetic uses `Decimal` precision; binary-float drift on buying-power, fills, fees, and prices is gone.

This is one of the two largest stories in the work tree. Pair with story 06a (activity_log refactor includes its own 16 money-field migration).

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L4 — system-wide motivation; lists hot files
* `.claude/skills/python-architecture/references/data-and-types.md` § D3 — Money primitive shape
* Story 04 (<issue id="c0c8bfb2-285e-4624-9d1d-7f1b6c8a9e7b">ALP-460</issue>) — defines `Money`, `Price`, `money()`, `price()`, `signed_money()`
* `src/alphamind/execution/broker_adapter/queries.py:58-73` — `TradeAccountSnapshot.cash: float`, `PositionSnapshot.avg_entry_price: float`, etc.
* `src/alphamind/execution/state_persistence/write_paths/records.py:88-93` — `FillRecord.fill_price: float`, `fees_usd: float`
* `src/alphamind/commands/command_models.py` (after 02b) — every OMS command variant carries price/dollar-value fields
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/models.py` — `OrderParameters.price`, `PriceCondition.trigger_price`, bracket levels
* `src/alphamind/execution/state_persistence/write_paths/phase2.py:1313-1359` — `cash_row.reserved_capital_usd` accumulator (the buying-power source of truth)
* `src/alphamind/portfolio_state/snapshot.py:33-36,63-64` — top-level USD aggregates

## Depends on

* 04 (<issue id="c0c8bfb2-285e-4624-9d1d-7f1b6c8a9e7b">ALP-460</issue>) — Money/Price types must exist
* 02b (<issue id="ff95f73f-431c-4d94-af8a-5729ae654354">ALP-458</issue>) — `commands.command_models` must exist as the new home of OMS command schemas (their money fields migrate here)

## Scope

In scope: every monetary field in boundary types — broker-API surface (`queries.py`), command envelopes (`commands/command_models.py`), decision-layer models, fill records, cash ledger writers, portfolio-state snapshot, P/L aggregates. Tests update accordingly.

### 1\. Broker-API boundary types

`execution/broker_adapter/queries.py`:

* `TradeAccountSnapshot.cash: float` → `Money`
* `PositionSnapshot.avg_entry_price: float` → `Price`
* `PositionSnapshot.cost_basis: float` → `Money`
* Any other USD fields → `Money`

Where Alpaca SDK returns strings, parse via `money(raw_str)` / `price(raw_str)` at the boundary (the wrapper class methods that consume SDK objects). Where SDK returns floats (rare), convert via `money(Decimal(str(raw_float)))` — never `Decimal(raw_float)` directly (the float→Decimal cast preserves binary representation including drift).

### 2\. Command envelope money fields

`commands/command_models.py`:

* `OpenCommand.entry_price: float`, `OpenCommand.stop_price: float`, `OpenCommand.target_price: float` → `Price`
* `OpenCommand.notional_usd: float` → `Money`
* `CloseCommand.limit_price: float` → `Price`
* `AdjustCommand.new_stop: float`, `new_target: float` → `Price`
* All `dollar_value`, `premium_at_risk`, `*_usd` fields → `Money`

### 3\. Decision-layer models

`decision/analyst/models.py:197,223`, `decision/strategist/models.py:145,155`:

* `OrderParameters.price: float` → `Price`
* `PriceCondition.trigger_price: float` → `Price`
* `BracketAdjustNewStopLevel.trigger_price: float` → `Price`
* `BracketAdjustNewTargetLevel.price: float` → `Price`

Decision input bundles carry `total_portfolio_value_usd: float`, `available_for_new_positions_usd: float` (~13 occurrences across `input_bundle.py` and `runner.py`) → `Money`.

### 4\. Cash ledger + fill records

`execution/state_persistence/write_paths/records.py:88-93`:

* `FillRecord.fill_price: float` → `Price`
* `FillRecord.fees_usd: float` → `Money`

`execution/state_persistence/write_paths/phase2.py:1313-1359` `_reserve_capital` / `_release_capital`:

* `cash_row.reserved_capital_usd: float` → `Money`
* Accumulator math uses `Decimal` arithmetic: `cash_row.reserved_capital_usd = cash_row.reserved_capital_usd + amount_usd` is now `Money` + `Money` → `Money`. The `max(... - ..., 0.0)` patch at line 1349 disappears (Decimal subtraction is exact).

### 5\. Portfolio-state snapshot top-level aggregates

`portfolio_state/snapshot.py:33-36, 63-64`:

* `total_unrealized_pnl_usd: float`, `total_unrealized_pnl_pct_of_portfolio: float`, `daily_total_pnl_usd: float`, etc. → `Money` for USD fields; pct fields stay `float` (they're ratios, not money preservation).

### 6\. Codec layer coordination

`execution/state_persistence/tables/*_codec.py` files (codecs for orders, positions, fills, activity_log) — convert between Python `Decimal` and the SQLite column type (TEXT-encoded Decimal or NUMERIC). Verify each codec preserves precision round-trip. The codec is the durability boundary; once it round-trips correctly, internal code trusts the type.

### Out of scope

Activity_log money fields (16 of them) are migrated by story 06a as part of the activity_log refactor — that story coordinates with the activity_log codec, which has unique sidecar JSON encoding. Money in distillation (ratios/multiples) is excluded — those aren't money preservation; the 7 distillation `: float` sites are false positives.

## Acceptance criteria

- [ ] Every USD field in `commands/command_models.py`, `decision/*/models.py`, `execution/broker_adapter/queries.py`, `execution/state_persistence/write_paths/records.py`, `portfolio_state/snapshot.py` uses `Money` or `Price` from `alphamind._kernel.money`.
- [ ] The `_reserve_capital` / `_release_capital` accumulator in `phase2.py` operates on `Money` (Decimal-backed); the `max(... - ..., 0.0)` patch at line 1349 is removed.
- [ ] Alpaca SDK string-returned monetary fields parse via `money(raw_str)` / `price(raw_str)` at the broker-adapter boundary; the codec round-trip preserves Decimal precision.
- [ ] `grep -rn ": float" src/alphamind/{commands,decision,execution/broker_adapter,execution/state_persistence/write_paths,portfolio_state/snapshot.py}` returns no money-field hits (ratios/pct fields are still float — manually verify each remaining hit is non-money).
- [ ] `uv run lint-imports` passes.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

Synthetic precision test: process a sequence of 7 `money("0.1")` debits against a cash row; final balance equals exactly `money("-0.7")`, not `Decimal("-0.7000000000000001")`. Spot-check Alpaca-roundtrip test: simulate broker returning `cash="1234567.89"` string; assert `TradeAccountSnapshot.cash == money("1234567.89")` exactly. Activity_log round-trip via codec preserves Money fields (this test re-runs in story 06a too).