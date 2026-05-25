## Symptom

`src/alphamind/portfolio_state/assembler.py:26-30` (module docstring):

> "Known limitation — option pricing (Steps 5/6): Option-contract mark-to-market uses `premium_paid_per_contract` as a stand-in price rather than a live option quote. The resulting `current_market_value_usd` lags premium drift. A future story can introduce an OptionPriceProvider Protocol to close this gap."

No `OptionPriceProvider` Protocol exists in the codebase. The continuous monitor already has `options_contract_snapshots` and an `iv_provider` plumbing in `greeks_refresh/`, but neither feeds the snapshot assembler.

## Production impact

Every snapshot assembly (per pipeline invocation, used by all decision agents and PM) shows option positions valued at their entry premium instead of current MTM. Affects `current_market_value_usd` per option position, `total_portfolio_value_usd` (since options contribute via MV), and derived risk computations (drawdown attribution, position weight, gross exposure).

A position that's drifted 50% on premium since entry shows the same MTM as entry day. The strategist and PM make sizing decisions against this stale view.

## Scope

**(A) Introduce** `OptionPriceProvider` **Protocol.** Sibling to `CurrentPriceProvider` in `portfolio_state/pricing.py`. Returns `PriceQuote(price, source, is_stale)` keyed by OCC symbol or `(underlying, expiration, strike, contract_type)`.

**(B) Concrete impl backed by** `options_contract_snapshots`**.** Read the most recent snapshot per contract; mark stale if older than a config threshold (default 60s, mirroring `UnderlyingPriceCache`'s staleness window).

**(C) Wire into assembler.** Thread the provider into `assemble_snapshot`; use it in Step 5/6 (option pricing) instead of `premium_paid_per_contract`. Preserve the entry-premium fallback when the provider returns `is_stale=True` so missing-quote behavior matches the existing equity path.

**(D) Validate against the continuous monitor's greeks path.** The monitor reads `options_contract_snapshots` for greeks refresh; ensure both paths see consistent snapshots (transaction-isolated reads, single connection).

## Acceptance criteria

- [ ] `OptionPriceProvider` Protocol defined in `portfolio_state/pricing.py`.
- [ ] `SqlOptionPriceProvider` (or equivalent) implementation backed by `options_contract_snapshots`.
- [ ] Assembler uses the provider for option `current_market_value_usd`; falls back to entry premium when `is_stale=True`.
- [ ] Test fixtures cover three cases: live snapshot present, stale snapshot, no snapshot.
- [ ] Known-limitation paragraph in `portfolio_state/assembler.py:26-30` removed.
- [ ] `uv run pytest tests/portfolio_state/ -n auto` passes; lint chain clean.

## Verification

* Strategist/PM input bundles show realistic option market values (not pinned to entry premium).