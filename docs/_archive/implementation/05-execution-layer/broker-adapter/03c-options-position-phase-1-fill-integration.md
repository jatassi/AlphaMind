# 03c — Options-position Phase 1 fill integration

## Goal

Extend `src/alphamind/execution/state_persistence/write_paths/phase1.py` to remove the `NotImplementedError` on options-position fill integration paths. Add per-step handlers for entry / add / close fills targeting `OptionsInstrumentSpec` instruments: cost-basis math accounting for `contract_multiplier` (typically 100), realized P/L on close using premium-paid vs premium-received with multiplier scaling, options-bracket activation marker (the actual bracket trigger lives in the continuous monitor, but Phase 1 records the activation event when the entry fills), and the activity-log entries listed in `state-persistence.md § Position lifecycle events`.

This is a **coordinated edit** to existing state-persistence write-path code (already shipped under <issue id="beaf98a0-a9fc-44a8-ac46-32e50f604345">ALP-119</issue>). The orchestrator must run the full state-persistence test suite after integration to catch regressions on equity paths.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path: fill integration — the 9-step Phase 1 sequence this story extends for options.
* `docs/design/05-execution-layer/position-model.md` — `OptionsPositionDetails` fields (strike, expiration, contract_type, contract_multiplier, premium_paid, current_premium, etc.).
* `docs/design/05-execution-layer/orders-and-brackets.md` § Options price-based stops: trigger on the underlying — confirms options brackets fire monitor-side; Phase 1 records bracket activation when entry fills.
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — the existing equity-path implementation. Inspect the `NotImplementedError` raise points; this story removes them by adding options-aware branches. Likely structure: per-instrument-type dispatch on `instrument_type=OPTIONS`.
* `src/alphamind/portfolio_state/records/positions.py` — `OptionsPositionDetails`, `PositionRecord`, `PositionFill`, `OptionGreeks`. Greek fields are **not populated** by Phase 1 — the guardrail-evaluation library populates them at OPEN/ADD validation; the monitor refreshes them.
* `src/alphamind/portfolio_state/records/orders.py` — `OptionsInstrumentSpec`, `OrderRecord`, `BracketRecord`, `BracketLeg`.
* `src/alphamind/execution/broker_adapter/fill_stream.py` (story 02f) — `FillReport` carries `occ_symbol` and `position_intent`; this story consumes those fields to dispatch the right options-handling branch.
* `tests/execution/state_persistence/` — existing equity test patterns; mirror them for options.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (G).

## Depends on

* <issue id="7690cddb-d84d-4a0e-bc52-e7942f0480c4">ALP-384</issue> (02f — trade_updates fill-stream subscriber + fill-report translator). Provides the `FillReport` shape this story consumes.

## Scope

Source under `src/alphamind/execution/state_persistence/write_paths/phase1.py` (existing file — extend). Tests at `tests/execution/state_persistence/test_phase1_options.py` (new file). The structure mirrors the existing `test_phase1.py` (equity tests).

### 1. Remove `NotImplementedError` raises on options paths

Locate every call site in `phase1.py` that raises `NotImplementedError` for `instrument_type == OPTIONS` and replace with the actual implementation. Likely sites: position update, cost-basis update, bracket update, P/L computation, activity-log entry construction.

### 2. Cost-basis math for options entries

For an options OPEN or ADD fill:

```python
# Cost basis on options entry:
# - Long position (buy_to_open): cost_basis += fill_price * fill_quantity * contract_multiplier
# - Short position (sell_to_open): cost_basis -= fill_price * fill_quantity * contract_multiplier
#   (negative cost basis for shorts — premium received)
#
# Average cost basis after partial fill, on add, etc., follows the same
# weighted-average formula as equity, scaled by contract_multiplier.
```

`contract_multiplier` comes from the `OptionsPositionDetails` record (typically 100; design says it's per-instrument).

### 3. Realized P/L on close

For an options CLOSE fill:

```python
# Realized P/L:
#   long position closed: realized_pl = (close_fill_price - avg_entry_cost) * close_fill_qty * multiplier
#   short position closed: realized_pl = (avg_entry_cost - close_fill_price) * close_fill_qty * multiplier
#
# Apply per-fill (partial closes accumulate). Total realized P/L at full
# close is sum of per-partial-close realized P/L deltas.
```

### 4. Options bracket activation event

When an options entry fills, Phase 1 records a `bracket_activated` activity-log event. The bracket-record's protective leg is monitor-managed (not a broker-submitted order — `BracketLeg.order_id is None` for the price-stop leg, since Alpaca doesn't support brackets on options). The activation marker indicates "monitor should now begin watching the underlying stream for the trigger price."

The Phase 1 step doesn't itself arm anything — the continuous monitor work tree (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>) handles the actual underlying-stream subscription. Phase 1 just transitions `BracketLeg.status` from `PENDING_ACTIVATION` to `ACTIVE` and writes the activity-log entry.

### 5. Cash-ledger updates for options

Options fills affect cash by `fill_price * fill_quantity * contract_multiplier`:

* `buy_to_open`: cash debited by premium paid.
* `sell_to_open`: cash credited by premium received.
* `buy_to_close`: cash debited by premium paid (covering the short).
* `sell_to_close`: cash credited by premium received (closing the long).

Margin held adjustments for short options are formula-based (Alpaca's margin formula on underlying price + strike + volatility); for this story, defer to Alpaca's reported `maintenance_margin` (already on `TradeAccountSnapshot`). Don't re-derive locally.

### 6. Activity-log entries for options

Each options fill produces the same set of activity-log entries as equity, with options-specific detail. Per `state-persistence.md § Event type catalog`:

* `position_opened` — entry fill: ticker is the underlying, mechanism is `order_fill`, options-specific fields: `occ_symbol`, `strike`, `expiration`, `contract_type`, `contract_multiplier`, premium paid/received.
* `position_added` — ADD fill: same options-specific fields as opened, plus `additional_quantity` and new average cost basis.
* `position_reduced` — partial CLOSE fill.
* `position_closed` — full CLOSE fill: includes `realized_pl`, exit method (`pm-decision` or `monitor_underlying_stop_triggered` etc.).
* `bracket_activated` — entry fill: protective leg transitions to `ACTIVE`. For options, mention the underlying ticker and trigger price (monitored by <issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* `cash_debited` / `cash_credited` — premium flow with reason `entry_fill_options` / `exit_fill_options`.

### 7. Greeks NOT populated by this story

Greeks (`delta`, `gamma`, `theta`, `vega`) on the post-fill position are NOT populated by Phase 1 — the guardrail-evaluation library populates them at OPEN/ADD command validation, and the continuous monitor refreshes them on the schedule + move-trigger. Phase 1 leaves the existing `OptionGreeks` field on the position record unchanged (or sets it from the validation-time greeks attached to the order).

### 8. Test coverage

Mirror the equity test structure. New tests under `tests/execution/state_persistence/test_phase1_options.py`:

* OPEN long call entry fill: position transitions PENDING → OPEN with correct cost basis and contract multiplier.
* OPEN short put entry fill: position transitions with negative cost basis.
* ADD fill: average cost basis recomputes correctly.
* Partial CLOSE: realized P/L accumulates; remaining quantity is correct.
* Full CLOSE: realized P/L final; position transitions OPEN → CLOSED; bracket DISSOLVED.
* Bracket activation event written on entry fill.
* Cash-ledger entries match expected premium flow.
* Activity-log entries are written in the documented order with options-specific detail.
* Mleg parent fill (without legs filled): position remains PENDING; no premature activation.

### Out of scope

* Mleg/strategy-position Phase 1 fill integration — story 04b.
* Greeks refresh — continuous monitor (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Underlying-stream stop triggering — continuous monitor.
* Options corporate-action handling (early exercise, assignment, expiration) — corporate-actions work tree (<issue id="6936fa0f-6f46-4da7-9787-473ae297e197">ALP-124</issue>).
* Margin computation for short options — defer to Alpaca's reported `maintenance_margin`.

## Acceptance criteria

- [ ] `src/alphamind/execution/state_persistence/write_paths/phase1.py` no longer contains any `NotImplementedError` raise paths gated on `instrument_type=OPTIONS`.
- [ ] Phase 1 integrates an options entry fill: position transitions PENDING → OPEN; `cost_basis`, `quantity`, and `entry_timestamp` are populated; `OptionGreeks` field is preserved (not overwritten).
- [ ] For a long call entry: cost_basis == fill_price × fill_qty × contract_multiplier (positive).
- [ ] For a short put entry: cost_basis is negative (premium received).
- [ ] ADD fill: average cost basis recomputes via weighted-average across all entry fills.
- [ ] Partial CLOSE fill: realized_pl is computed correctly; position remains OPEN with reduced quantity; `position_reduced` activity-log entry written.
- [ ] Full CLOSE fill: position transitions OPEN → CLOSED; bracket transitions to DISSOLVED; `position_closed` activity-log entry written with `realized_pl` and `exit_method`.
- [ ] On entry fill, `BracketLeg.status` for monitor-managed legs transitions PENDING_ACTIVATION → ACTIVE; `bracket_activated` activity-log entry is written.
- [ ] Cash-ledger updates: entry debits cash by premium paid; close credits cash by premium received; both per the contract-multiplier-scaled formula.
- [ ] All Phase 1 mutations remain atomic — failure mid-integration rolls back; fills remain unprocessed.
- [ ] Existing equity Phase 1 tests in `tests/execution/state_persistence/test_phase1.py` remain green (no regressions).
- [ ] New tests in `tests/execution/state_persistence/test_phase1_options.py` cover the scenarios listed in §8 above.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` is green.
- [ ] `uv run pytest -n auto` is green (full suite).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/state_persistence/ -n auto -v` — equity tests still pass + new options tests pass.
* Run `uv run pytest -n auto` — full suite green; no regressions in OMS / state-persistence / portfolio_state.
* Spot-check the `RUNBOOK_state_persistence.md` Phase 1 narrowing notes — update them to mention that options paths are now supported (mleg/strategy still NotImplementedError until story 04b lands).
* Lint clean per CLAUDE.md.
