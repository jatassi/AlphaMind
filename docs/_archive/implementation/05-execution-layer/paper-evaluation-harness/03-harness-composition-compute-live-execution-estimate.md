# 03 — Harness composition `compute_live_execution_estimate`

## Goal

Composes the fee, spread, and impact primitives (stories 01 / 02a / 02b) into a single `compute_live_execution_estimate(...)` function that produces a `LiveExecutionEstimate` for one fill. The function dispatches on `instrument_type` (EQUITY / OPTIONS) to substitute the correct data inputs (equity ADV vs. options-contract volume) and the correct fee rules. Returns `None` when required inputs are missing for that contract (per parent decision H — graceful no-estimate fallback rather than synthetic values). Strategy / mleg parent fills never reach this function (per decision G).

## Reading

* `docs/design/05-execution-layer/paper-evaluation-harness.md` — full document; particularly § Sub-models and § What the harness does not do
* `src/alphamind/portfolio_state/records/positions.py:90` — `LiveExecutionEstimate` 4-field typed shape (final per parent decision A)
* `src/alphamind/portfolio_state/records/positions.py:25` — `InstrumentType` enum (EQUITY / OPTIONS / STRATEGY)
* `src/alphamind/config/models/execution.py` — `PaperHarness`, `OrderType`, `FeeSchedule` (the composition reads `spread_buffer_pct`, `impact_coefficients[order_type]`, and `fee_schedule` from this config)
* `src/alphamind/execution/paper_evaluation_harness/fees.py` — story 01's `compute_regulatory_fees`
* `src/alphamind/execution/paper_evaluation_harness/spread.py` — story 02a's `estimate_spread`
* `src/alphamind/execution/paper_evaluation_harness/impact.py` — story 02b's `estimate_impact`
* `src/alphamind/_kernel/money.py` — Money / Price arithmetic

## Depends on

* `ALP-524` (story 01) — provides `compute_regulatory_fees` + `FeeSchedule`
* `ALP-525` (story 02a) — provides `estimate_spread`
* `ALP-526` (story 02b) — provides `estimate_impact`

## Scope

In scope, under `src/alphamind/execution/paper_evaluation_harness/harness.py`. Tests at `tests/execution/paper_evaluation_harness/test_harness.py`.

### 1\. `compute_live_execution_estimate` function

New module with:

```python
def compute_live_execution_estimate(
    *,
    fill_price: Price,
    fill_quantity: float,
    instrument_type: InstrumentType,
    side: Literal["buy", "sell"],
    order_type: OrderType,
    adv_shares: float | None,
    realized_volatility: float | None,
    config: PaperHarness,
) -> LiveExecutionEstimate | None: ...
```

For `InstrumentType.STRATEGY`: raise `NotImplementedError` (matches story 01's `compute_regulatory_fees` behavior). Strategy parent fills never reach this layer per decision G.

For `InstrumentType.EQUITY`:

* If `adv_shares is None` or `realized_volatility is None`, return `None`. The caller (story 04a) interprets None as "data unavailable, persist with `live_execution_estimate=None`".
* Otherwise:
  * Compute `estimated_spread = estimate_spread(fill_price, adv_shares, realized_volatility, config.spread_buffer_pct)`.
  * Compute `estimated_impact = estimate_impact(fill_shares=fill_quantity, adv_shares=adv_shares, estimated_spread=estimated_spread, order_type=order_type, coefficient=config.impact_coefficients[order_type])`.
  * Compute `estimated_regulatory_fees = compute_regulatory_fees(instrument_type=EQUITY, side=side, fill_quantity=fill_quantity, fill_price=fill_price, schedule=config.fee_schedule)`.
  * Compute `live_adjusted_fill_price`: per the design doc, the paper fill price ± (spread/2 + per-share impact) — `+` for buys (we paid more in live), `-` for sells (we received less). Per-share impact = `estimated_impact / fill_quantity`. Per-share spread component = `estimated_spread / 2`. Convert with the `_kernel/money.py` boundary helpers and wrap with `price(...)` at the return edge.
  * Assemble `LiveExecutionEstimate(estimated_spread_usd=estimated_spread * fill_quantity, estimated_impact_usd=estimated_impact, estimated_regulatory_fees_usd=estimated_regulatory_fees, live_adjusted_fill_price=adjusted_price)`. **Note:** `estimated_spread_usd` carries the per-fill dollar cost (per-share spread × shares × 0.5 since the order crosses only half the spread on average) — confirm the per-share-vs-per-fill convention in the docstring and in the per-share-impact derivation; the field is named `_usd` so it must be a per-fill dollar magnitude.

For `InstrumentType.OPTIONS`:

* `adv_shares` here represents options-contract daily volume (per-contract). If unavailable (the wedge passes `None`), return `None` per decision H.
* `realized_volatility` is the underlying equity's realized vol — same source as equity.
* Same composition as equity, with two changes:
  * `compute_regulatory_fees(instrument_type=OPTIONS, ...)` — options fee dispatch per story 01.
  * The notional for `live_adjusted_fill_price` uses `fill_quantity * 100` shares-equivalent for standard options contracts when computing the per-share impact attribution. Confirm by reading `broker-adapter.md` for the contract multiplier convention.

### 2\. Sign convention for `live_adjusted_fill_price`

Per the design doc § Spread + impact:

* Buys: paper_fill_price + (spread/2 + per_share_impact) → live_adjusted > paper.
* Sells: paper_fill_price - (spread/2 + per_share_impact) → live_adjusted < paper.

The `live_adjusted_fill_price` is the price the order WOULD have filled at live — strictly worse than paper for the buyer (paid more) or seller (received less), assuming non-zero drag. With zero drag (e.g., zero spread and zero impact), `live_adjusted_fill_price == fill_price`.

### 3\. Re-export

Add `compute_live_execution_estimate` to `paper_evaluation_harness/__init__.py` `__all__`.

### Out of scope

* Wedge integration / paper-mode gating (story 04a)
* Looking up `adv_shares` from the distillation repo (story 04a does this)
* Looking up `realized_volatility` (story 04a does this — likely from the same source the breach-loop greeks refresh uses, or from a per-ticker volatility table)
* Looking up `order_type` from the OrderRecord (story 04a does this)
* P/L aggregate (story 04b)
* Persistence (already handled by the existing codec at `src/alphamind/state/tables/fill_records_codec.py:56`)

## Acceptance criteria

- [ ] `compute_live_execution_estimate` for an EQUITY buy with all inputs non-None returns a `LiveExecutionEstimate` whose `live_adjusted_fill_price > fill_price`.
- [ ] Same call but `side="sell"` returns `live_adjusted_fill_price < fill_price`.
- [ ] EQUITY call with `adv_shares=None` returns `None`.
- [ ] EQUITY call with `realized_volatility=None` returns `None`.
- [ ] OPTIONS buy with all inputs non-None returns a `LiveExecutionEstimate` carrying CAT + ORF + OCC fees (no SEC for buy).
- [ ] OPTIONS sell with all inputs non-None returns a `LiveExecutionEstimate` carrying CAT + SEC + ORF + OCC.
- [ ] OPTIONS call with `adv_shares=None` returns `None`.
- [ ] STRATEGY call raises `NotImplementedError` with a message naming decision (G).
- [ ] On every returned `LiveExecutionEstimate`: all three `_usd` fields are non-negative Money; `live_adjusted_fill_price` is strictly positive Price.
- [ ] With zero spread + zero impact (achieved by passing impossible-low-vol or near-floor inputs), `live_adjusted_fill_price == fill_price` (within Decimal-rounding tolerance, after the floor saturates).
- [ ] No DB / HTTP / config-loading imports — only `PaperHarness` (passed in) and the local primitives.
- [ ] `paper_evaluation_harness/__init__.py` re-exports `compute_live_execution_estimate`.
- [ ] `tests/execution/paper_evaluation_harness/test_harness.py` exists and passes under `uv run pytest tests/execution/paper_evaluation_harness/test_harness.py --testmon -n auto`.
- [ ] `uv run pytest --testmon -n auto` passes.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.

## Verification

Scoped test file + full lint chain. The sign-convention assertions (buy → up, sell → down) and the None-on-missing-data assertions anchor the integration contract for story 04a.