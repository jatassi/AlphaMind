# Validation-tool market-price scope is held-book-only — active-universe tickers can't be validated

## Symptom

`validate_guardrail`'s market data (`ValidationToolState.library_market.underlying_prices`) is populated only from currently-open broker positions. Any active-universe ticker that is not in the held book has no spot price, so — post-[ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped) — `validate_guardrail` returns `overall: UNAVAILABLE` with `unavailable_reason: missing_market_price` for it, in every instrument type (equity and options).

This is the direct cause of the CSCO drop reported in [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped): CSCO was in the active universe (priced in the synthesizer brief) but not held, so the analyst could not validate any expression of its short post-earnings fade thesis.

## Evidence

`src/alphamind/scheduler/phase1_inputs.py` — `_build_market_inputs` populates `underlying_prices` from `positions`:

```
underlying_prices: dict[str, float] = {
    pos.symbol: float(pos.current_price) for pos in positions if pos.current_price is not None
}
```

`positions` is the open broker position set returned by `gather_phase1_inputs`. The resulting `MarketInputs` is threaded as `library_market` into `run_decision_pipeline` (`orchestrator.py:_build_decision_kwargs`) and reaches every decision agent's validation tool. The assembler's `price_map` (`pipeline/decision.py:_price_lookup_from_assembled`) is likewise held-book + pending-only.

## Root cause

The validation tool's market scope was sized to the held book — the only price set Phase 1 needs for snapshot assembly — but the decision agents propose against the full active universe. Coverage is structurally narrower than the candidate set.

## Scope

Source latest spot prices for the **active-universe** tickers — not just held positions — into `MarketInputs.underlying_prices`.

The price store exists: `ohlcv_bars` (`persistence/models.py`) is keyed `(ticker, timeframe, period_start)` with `adj_close` / `unadj_close` columns and a foreign key to `asset_universe.ticker`. The active universe is the `asset_universe` table (the `active_universe_tickers` helper in `data_sources/_common` already exposes it). The latest bar's close per active-universe ticker is a usable spot price for guardrail projection.

Extend `_build_market_inputs` (or its caller in `gather_phase1_inputs`) to read latest `ohlcv_bars` closes for the active-universe tickers and merge them into `underlying_prices`, with held-position `current_price` taking precedence where both exist (a live broker quote beats an EOD bar). Mind freshness: an EOD close is staler than a live position quote — decide whether a staleness bound applies, consistent with `config.snapshot_freshness_max_price_age_seconds`.

## Acceptance criteria

- [ ] `MarketInputs.underlying_prices` covers every active-universe single-name ticker, not just held positions.
- [ ] `validate_guardrail` on an active-universe-but-unheld ticker returns PASS/FAIL, not `UNAVAILABLE` / `missing_market_price`.
- [ ] Held-position prices take precedence over EOD bars where both exist.
- [ ] Price staleness is bounded, or explicitly documented as acceptable for guardrail projection.

## Verification

* Unit-test `_build_market_inputs` (or the new universe-price path) against a seeded `ohlcv_bars` plus a held-position set: confirm the merged map covers universe tickers and held prices win on overlap.
* End-to-end: in a decision invocation, confirm an unheld active-universe ticker validates through `validate_guardrail` without an `UNAVAILABLE` result.

## Notes

Follow-up to [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped). [ALP-581](https://linear.app/alphamind-jatassi/issue/ALP-581/csco-validation-infrastructure-gap-analysts-clean-fade-thesis-dropped) made the gap a distinguishable `UNAVAILABLE` outcome; this issue closes it for market price. The `UNAVAILABLE` / `missing_market_price` path remains the correct fallback for genuinely uncovered tickers (new listings, data outages). Peer to the borrow-cost resolver wiring (separate issue).
