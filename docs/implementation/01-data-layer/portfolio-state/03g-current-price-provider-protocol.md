---
status: in_progress
completed_date:
commit_id:
---

# 03g — Current price provider protocol

## Goal

Declare the slim read-only protocol the snapshot assembler uses to fetch current prices for mark-to-market computations (market value, P/L, distance-to-target, fill-probability context) at `src/alphamind/portfolio_state/pricing.py`. Production wiring against the quant-1a database lands in a future story when the data-layer collector exposes a query helper. This story provides the `CurrentPriceProvider` Protocol, the `PriceQuote` value object, the `PriceSource` enum, the `UnknownTickerError` exception, and a test-and-fixture-only stub — nothing more.

## Reading

- `../../../design/01-data-layer/internal/portfolio-state.md` § 1a — "current market value: quantity × current price using the latest price from quant 1a"; cross-reference map showing categories 1a, 2a, and 4b need market-data cross-reference; fill-probability context (category 4b) requires current price vs. limit comparison
- `../../../design/01-data-layer/external/quantitative.md` § Q1 (price and volume) — the quant data the system collects; this story does NOT consume it directly but declares the read interface portfolio state needs; the production provider will SELECT from stored OHLCV close records
- `../../../design/05-execution-layer/state-persistence.md` § Read paths § Invocation snapshot — mark-to-market market values are "computed" at delivery time using current prices; the assembler must deliver a snapshot even when prices are stale
- `02-package-skeleton-and-config.md` — `pricing.py` is listed as story 03g's target; the `snapshot_freshness.max_price_age_seconds` config knob (defined in `config/portfolio_state.yaml`) is the freshness threshold this story's staleness contract uses
- `../../../implementation/03-analysis-layer/synthesizer/05b-portfolio-state-read-protocol.md` — sibling pattern: Protocol + Stub, async methods, frozen Pydantic value objects, in-scope/out-of-scope split, acceptance-criteria style; `is_error: True` as in-band recoverable flag is the direct analogue of `is_stale: True` here

## Depends on

- 02

This story does NOT depend on the production quant-1a runtime existing — it declares the protocol and provides a stub for tests. When the data-layer collector exposes an OHLCV query helper, a future story implements `CurrentPriceProvider` against it; no change to this story's code is required.

## Scope

In scope, all under `src/alphamind/portfolio_state/pricing.py`:

1. **`PriceSource` enum** — `StrEnum`, members in ALL CAPS:
   - `INTRADAY_QUOTE` — a live intraday tick or real-time quote record
   - `OHLCV_CLOSE` — the last OHLCV bar's close price (stored in `OhlcvBars` per `src/alphamind/persistence/models.py`)
   - `STALE_FALLBACK` — no fresh data was available within the freshness window; the returned price is the best-available stale record; always paired with `is_stale=True`

2. **`PriceQuote`** — Pydantic v2 `BaseModel`, `model_config = {"frozen": True}`:
   - `ticker: str`
   - `price_usd: float` — positive; rejected if ≤ 0
   - `as_of_timestamp: datetime` — tz-aware UTC; the timestamp on the underlying price record, not the time the read was performed
   - `source: PriceSource`
   - `is_stale: bool` — the provider sets this to `True` when `as_of_timestamp` is older than the `freshness_threshold_seconds` the caller supplied; the assembler propagates this to a per-position staleness flag (story 08)

3. **`UnknownTickerError`** — inherits from `ValueError`; raised by `get_quote` when the ticker is not tracked by the provider; staleness is never an exception — it is in-band via `is_stale`.

4. **`CurrentPriceProvider`** — `typing.Protocol`, decorated with `@runtime_checkable`:
   - `async def get_quote(self, ticker: str, *, freshness_threshold_seconds: float) -> PriceQuote` — returns a quote whose `is_stale` reflects whether `as_of_timestamp` is older than `freshness_threshold_seconds`. Raises `UnknownTickerError` for unrecognized tickers. Never raises on staleness.
   - `async def get_quotes(self, tickers: tuple[str, ...], *, freshness_threshold_seconds: float) -> dict[str, PriceQuote]` — bulk variant; returns a dict keyed by ticker. Tickers not tracked by the provider are omitted from the result dict; the method does not raise for unrecognized tickers. Each returned quote carries `is_stale` set independently per the same freshness threshold.

5. **`StubCurrentPriceProvider`** — concrete implementation of `CurrentPriceProvider`; for tests and fixtures only; no I/O:
   - Constructor: `StubCurrentPriceProvider(quotes: dict[str, PriceQuote], now: datetime)` — `quotes` is the backing store (keyed by ticker), `now` is the stub's clock (a tz-aware UTC datetime supplied at construction so tests can simulate any point in time without freezing system time).
   - `get_quote`: looks up `ticker` in `quotes`; raises `UnknownTickerError` if absent. Returns the stored `PriceQuote` with `is_stale` recomputed: `True` if `(now - quote.as_of_timestamp).total_seconds() > freshness_threshold_seconds`, `False` otherwise. The returned object is a new `PriceQuote` instance (frozen model copy-with-update) reflecting the recomputed `is_stale`; the backing dict is not mutated.
   - `get_quotes`: applies `get_quote` logic per ticker; silently omits tickers absent from `quotes`; never raises.

Unit tests at `tests/portfolio_state/test_pricing.py`:
- `PriceQuote` rejects `price_usd ≤ 0` with a `ValidationError`.
- `PriceSource` has exactly `INTRADAY_QUOTE`, `OHLCV_CLOSE`, `STALE_FALLBACK` — no other members.
- `StubCurrentPriceProvider.get_quote(ticker, freshness_threshold_seconds=N)` returns the stored quote with `is_stale=False` when `as_of_timestamp` is within the threshold.
- `StubCurrentPriceProvider.get_quote(...)` returns a quote with `is_stale=True` when `as_of_timestamp` is older than the threshold.
- Unknown ticker raises `UnknownTickerError`.
- `get_quotes` for a mixed batch (some known, some unknown) returns only known tickers and does not raise.
- `mypy` confirms `StubCurrentPriceProvider` structurally satisfies `CurrentPriceProvider` (no type errors).
- `isinstance(stub, CurrentPriceProvider)` returns `True` (runtime_checkable).

Out of scope:
- The production `CurrentPriceProvider` implementation (a future story in the data-layer collector or pipeline tree).
- The Q1 OHLCV bar query path (`OhlcvBars` in `src/alphamind/persistence/models.py` already exists; the production provider will SELECT from it).
- Live websocket-based intraday quote streaming (Phase 4+ concern; initial production wiring will use latest stored OHLCV close tagged with staleness).
- Caching or memoization (a production implementation may cache transparently behind the Protocol; not part of this contract).
- Per-ticker missing-ticker reporting on `get_quotes` (dict-omission is sufficient per the assembler's fail-recoverable semantics; no typed result envelope is needed).

## Notes

The Protocol-and-stub pattern follows `05b-portfolio-state-read-protocol.md` from the synthesizer work tree: declare the slim contract here, allow the assembler and computation stories to test against the stub, and let the production runtime wire later. This story does NOT depend on the quant-1a runtime existing.

`is_stale` is in-band (returned on the quote) rather than an exception because the snapshot assembler must deliver a snapshot even when prices are stale — staleness propagates to per-position staleness reporting (story 08) without aborting the invocation. The same trade-off as the synthesizer's `is_error: True` recoverable treatment, applied to the assembler instead of an LLM.

The `freshness_threshold_seconds` parameter is per-call rather than provider-state because different consumers may apply different thresholds (the snapshot assembler uses `max_price_age_seconds` from config, but a future intraday monitor might use a tighter threshold). Per-call avoids hidden global state and keeps the Protocol general.

Per `feedback_avoid_numeric_anchors.md`, no threshold is hardcoded in the Protocol or stub. Thresholds come from config (story 02's `snapshot_freshness.max_price_age_seconds`) and are passed at call time by the assembler.

`PriceSource.STALE_FALLBACK` is a sentinel for "we returned something but freshness was not met" — distinct from `INTRADAY_QUOTE` and `OHLCV_CLOSE`, which describe the data-source modality. Future code can branch on either dimension independently.

The stub recomputes `is_stale` from the constructor-supplied `now` rather than calling `datetime.now(tz=UTC)` so tests can simulate staleness without freezing system time. The backing dict is not mutated; `is_stale` is applied by constructing a new frozen `PriceQuote` via `model_copy(update={"is_stale": ..., "source": ...})` or equivalent Pydantic v2 idiom.

Per `feedback_no_inventing_component_names.md`, "current price provider" maps directly to `portfolio-state.md` § 1a's "latest price from quant 1a" usage. `PriceQuote`, `PriceSource`, and `CurrentPriceProvider` are otherwise straightforward; no names are invented.

## Acceptance criteria

- [ ] `PriceSource` enum has exactly `INTRADAY_QUOTE`, `OHLCV_CLOSE`, `STALE_FALLBACK` — no other members.
- [ ] `PriceQuote` is a frozen Pydantic v2 model with `ticker: str`, `price_usd: float`, `as_of_timestamp: datetime` (tz-aware UTC), `source: PriceSource`, `is_stale: bool`; rejects `price_usd ≤ 0` with `ValidationError`.
- [ ] `UnknownTickerError` exists and inherits from `ValueError`.
- [ ] `CurrentPriceProvider` is declared as `typing.Protocol` with `@runtime_checkable` and the two documented async methods with the exact signatures above.
- [ ] `StubCurrentPriceProvider` implements `CurrentPriceProvider`: `get_quote` returns the stored quote with `is_stale` recomputed from `freshness_threshold_seconds` and the constructor-supplied `now`; raises `UnknownTickerError` for absent tickers; does not mutate the backing dict.
- [ ] `get_quotes` omits unknown tickers from the result dict and does not raise.
- [ ] `mypy` confirms `StubCurrentPriceProvider` satisfies `CurrentPriceProvider` with no type errors.
- [ ] `isinstance(stub, CurrentPriceProvider)` returns `True`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
