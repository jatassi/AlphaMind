# 04b — Strategy/mleg-position Phase 1 fill integration

## Goal

Extend `src/alphamind/execution/state_persistence/write_paths/phase1.py` to remove the `NotImplementedError` on strategy-position (`StrategyInstrumentSpec` with `instrument_type=STRATEGY`) fill integration paths. Implement the mleg-specific atomicity contract per `broker-adapter.md § Multi-leg fill events`: a strategy position transitions PENDING → OPEN only when **every leg** has reached `filled` status; per-leg children fills accumulate against the parent until that condition is met. Strategy-level cost basis is the net debit/credit (sum of long-leg premiums paid minus short-leg premiums received, scaled by per-leg ratios and contract multipliers). Realized P/L on close uses the same net-pricing convention.

This story completes the Phase 1 narrowing's removal — after stories 03c (options) and this story (strategy/mleg), `phase1.py` carries no `NotImplementedError` paths gated on instrument type.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path: fill integration — the 9-step Phase 1 sequence this story extends for strategy positions.
* `docs/design/05-execution-layer/broker-adapter.md` § Multi-leg (`mleg`) fill events § Atomicity — the strategy is not considered open until every leg has reached `filled` status. Under thin liquidity, `partial_fill` events on individual legs at different timestamps; OMS waits for all legs.
* `docs/design/05-execution-layer/broker-adapter.md` § Multi-leg fill events § Cancellation — partial-fill residuals after cancel are surfaced as `partially_filled` strategy state; the monitor reconciles via `GET /v2/orders`.
* `docs/design/05-execution-layer/orders-and-brackets.md` § Multi-leg strategies — strategy-level bracket on underlying not on per-leg; net debit/credit pricing.
* `docs/design/05-execution-layer/position-model.md` — `StrategyPositionDetails` fields.
* `src/alphamind/portfolio_state/records/positions.py` — `StrategyPositionDetails`, `StrategyLeg`, `PositionRecord`.
* `src/alphamind/portfolio_state/records/orders.py` — `StrategyInstrumentSpec`.
* `src/alphamind/execution/state_persistence/write_paths/phase1.py` — existing equity + post-03c options paths; this story adds the strategy branch.
* `src/alphamind/execution/broker_adapter/fill_stream.py` (story 02f) — `FillReport.parent_client_order_id`, `parent_alpaca_order_id` for per-leg children correlation.
* `tests/execution/state_persistence/test_phase1_options.py` (story 03c) — mirror the test structure for strategy positions.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (G).

## Depends on

* <issue id="4daa91dc-8670-45ee-9111-a8c91d863c2a">ALP-388</issue> (03c — Options-position Phase 1 fill integration). The single-leg-options handlers serve as the foundation strategy-leg fills compose from.
* <issue id="7690cddb-d84d-4a0e-bc52-e7942f0480c4">ALP-384</issue> (02f — trade_updates fill-stream subscriber). Provides the parent + per-leg `FillReport` shape this story consumes.

## Scope

Source under `src/alphamind/execution/state_persistence/write_paths/phase1.py` (existing file — extend). Tests at `tests/execution/state_persistence/test_phase1_strategy.py` (new file).

### 1. Strategy-fill correlation

Per-leg `FillReport`s arrive with `parent_client_order_id` populated. Phase 1 collects them and groups by parent. The dispatcher:

```python
# Pseudo:
parent_fills: dict[str, list[FillReport]] = {}  # client_order_id → list of leg reports
for fill in unprocessed_fills:
    if fill.parent_client_order_id is not None:
        parent_fills.setdefault(fill.parent_client_order_id, []).append(fill)
    elif fill.client_order_id in <strategy positions in flight>:
        # Parent strategy event itself (the "filled" event for the whole strategy)
        ...
```

The strategy-record in `StrategyPositionDetails` is updated incrementally:

* On per-leg `filled` event: mark that leg's `filled_qty` against its `quantity_ratio × strategy_qty`.
* When all legs reach `filled` status: position transitions PENDING → OPEN.
* The strategy-level `bracket_activated` event fires only at this transition.

### 2. Net debit/credit pricing

Strategy cost basis is computed as the sum across legs of:

```
sum_per_leg(
    sign_for_leg * leg_fill_price * leg_filled_qty * contract_multiplier
)
```

where `sign_for_leg` is `+1` for long legs (`buy_to_open`) and `-1` for short legs (`sell_to_open`). The result can be positive (net debit, paid premium) or negative (net credit, received premium).

Strategy-level realized P/L on close uses the same convention with closing fill prices.

### 3. Atomicity edge cases

Per design, mleg fills can arrive partially under thin liquidity:

* All legs `filled` together (typical case): position activates atomically.
* Some legs `partially_filled`: position remains PENDING; `position_partially_filled` activity-log entry written for diagnostic.
* Some legs `filled`, others still `partially_filled` or `new`: PENDING continues.
* After the strategy is canceled with some legs partially filled: position transitions to a `partially_filled` strategy state per `broker-adapter.md § Multi-leg fill events § Cancellation`. Surface as `bracket_incomplete_warning` activity-log entry; the strategist resolves via the next invocation per the strategy-leg-orphan escalation in the broker-adapter doc.

### 4. Strategy-level activity-log entries

Same set as equity / options, with strategy-specific detail:

* `position_opened` — when all legs reach filled. Mechanism: `mleg_strategy_filled`. Detail: `strategy_type`, `legs[]` per-leg fill prices and quantities, net cost basis.
* `position_added` — ADD on a strategy: same multi-leg fill correlation; new average cost basis.
* `position_reduced` — partial CLOSE: subset of strategy units closed, others remain.
* `position_closed` — full CLOSE: all legs of strategy closed; net realized P/L.
* `bracket_activated` — fires at the strategy-OPEN transition (not on per-leg fill).
* `bracket_incomplete_warning` — when a cancel leaves a partial residual.
* `cash_debited` / `cash_credited` — net debit (long strategies, e.g., long call spread) or net credit (short strategies, e.g., short straddle).

### 5. Per-leg attribution within the strategy

Each `PositionFill` log entry on the parent strategy carries per-leg detail (which leg, leg fill price, leg fill quantity). The OMS uses this for reconciliation against Alpaca's per-leg view and for per-leg Greeks tracking (greeks-refresh from monitor populates per-leg).

### 6. Test coverage

New tests under `tests/execution/state_persistence/test_phase1_strategy.py`:

* All-legs-filled atomicity: 4-leg iron condor with all legs filling on the same timestamp transitions PENDING → OPEN; net cost basis matches the sum-with-signs formula.
* Staggered fills: 3 legs filled on T1, 4th leg filled on T2 — position remains PENDING after T1, transitions OPEN at T2.
* Partial fill on one leg: position remains PENDING; diagnostic entry written.
* Cancel mid-fill: 2 legs filled, 2 not; cancel arrives — position transitions to a partially_filled strategy state with `bracket_incomplete_warning` written.
* Strategy CLOSE: all legs close-filled; net realized P/L computed.
* ADD on a strategy: scaled per-leg fills; average cost basis updates.
* Equity Phase 1 tests + options Phase 1 tests (from 03c) remain green — no regression.

### Out of scope

* Greeks refresh (per-leg or strategy-level) — continuous monitor (<issue id="734df060-3ba0-4514-8e97-bfc009e6b677">ALP-123</issue>).
* Strategy bracket triggering on the underlying — continuous monitor.
* Strategy-leg orphan resolution (when cancel leaves partial fills) — strategist's next-invocation re-evaluation, per `broker-adapter.md § Multi-leg fill events § Cancellation`.
* CA on optioned underlyings producing adjusted contracts — corporate-actions work tree (<issue id="6936fa0f-6f46-4da7-9787-473ae297e197">ALP-124</issue>).
* Updates to the project tracker — covered in story 05.

## Acceptance criteria

- [ ] `src/alphamind/execution/state_persistence/write_paths/phase1.py` no longer contains any `NotImplementedError` raise paths gated on `instrument_type=STRATEGY`.
- [ ] All-legs-filled atomic OPEN: 4-leg iron condor transitions PENDING → OPEN when the last leg's `filled` event arrives; cost basis = signed sum across legs.
- [ ] Long call spread (long lower call, short higher call): net cost basis is positive (net debit).
- [ ] Short put spread (short higher put, long lower put): net cost basis is negative (net credit; treat as -ve = "premium received").
- [ ] Staggered legs: position transitions only at the last leg's filled event; intermediate state remains PENDING.
- [ ] Partial-leg fill: position stays PENDING; `position_partially_filled` activity-log entry written.
- [ ] Cancel mid-fill with 2 of 4 legs filled: position transitions to a `partially_filled` strategy state; `bracket_incomplete_warning` activity-log entry written naming the unfilled leg(s).
- [ ] Strategy CLOSE: all legs close-fill (with optionally staggered timing); position transitions OPEN → CLOSED only when last leg closes; net realized P/L computed correctly.
- [ ] ADD on a strategy: per-leg ratios scale by `additional_quantity`; new entry fills correlate to the addition; average cost basis recomputes.
- [ ] `bracket_activated` activity-log entry fires only at the strategy-level OPEN transition (not on per-leg fills).
- [ ] All Phase 1 mutations remain atomic — failure mid-integration rolls back; both parent and per-leg fills remain unprocessed.
- [ ] Existing equity tests in `tests/execution/state_persistence/test_phase1.py` and options tests in `test_phase1_options.py` (story 03c) remain green.
- [ ] New tests in `tests/execution/state_persistence/test_phase1_strategy.py` cover the scenarios in §6.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` is green.
- [ ] `uv run pytest -n auto` is green (full suite).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/state_persistence/ -n auto -v` — every test passes (equity + options + strategy).
* Run `uv run pytest -n auto` — full suite green.
* Spot-check the `RUNBOOK_state_persistence.md` Phase 1 narrowing notes — update them to mention that all instrument types (equity / options / strategy) are now supported by Phase 1.
* Lint clean per CLAUDE.md.
