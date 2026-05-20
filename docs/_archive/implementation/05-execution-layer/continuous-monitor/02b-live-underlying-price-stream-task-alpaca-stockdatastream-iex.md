# 02b — Live underlying-price stream task

## Goal

Open a long-lived Alpaca `StockDataStream` (IEX feed) subscription to the underlying equity tickers needed by the breach-evaluation loop (story 03b) and the options bracket-stop watcher (story 04c), maintaining an in-memory `{ticker: (price, as_of)}` cache. Subscribe lazily — only to underlyings for currently-open positions plus strategy-underlying tickers — and update subscriptions on position-lifecycle events (open / close). Reconnect on disconnect; respect the configured reconnect budget. Expose the cache as a typed reader other tasks (03a greeks refresh, 03b breach loop, 04c options bracket-stop) consume without coupling to the stream's transport details.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4b, 4e — what the underlying-price stream powers
* `docs/design/05-execution-layer/orders-and-brackets.md` § Options price-based stops: trigger on the underlying — why we need a clean real-time underlying feed (not derived options prices) for trigger decisions
* Parent issue `ALP-123` § Pre-resolved decisions (B), (G) — provider choice (Alpaca IEX, smoke-tested 2026-05-10) and subscription-scope rule (open-positions only)
* `alpaca-py` `StockDataStream` source — `alpaca.data.live.stock.StockDataStream`; the registered-handler + `stream.run()` background-task pattern (the same pattern used by [ALP-384](https://linear.app/alphamind-jatassi/issue/ALP-384/02f-trade-updates-fill-stream-subscriber-fill-report-translator)'s `subscribe_trade_updates`)
* `alpaca.data.enums.DataFeed` — `DataFeed.IEX` is the value Story 02b uses (string `"iex"` does not satisfy alpaca-py's enum dispatch)
* `src/alphamind/execution/broker_adapter/fill_stream.py` — the broker-adapter primitive Story 02c wraps; same vendor library and similar lifecycle shape but the data-stream and trade-updates websockets are distinct endpoints with distinct subscription verbs
* `src/alphamind/portfolio_state/repository.py` (or wherever `PortfolioStateRepository` lives) — how to read current open-position tickers; the snapshot's `positions` (equity + options + strategy) tells the monitor which underlyings to subscribe to
* `src/alphamind/portfolio_state/records/positions.py` — `PositionRecord`, `OptionsPositionDetails`, `StrategyPositionDetails` — what fields give the underlying ticker for each instrument type
* `ALP-432` (01) — `MonitorSupervisor.register_task` is the entry point this story wires into

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config + logging

## Scope

Source under `src/alphamind/execution/continuous_monitor/underlying_stream/` (new sub-package). Tests at `tests/execution/continuous_monitor/underlying_stream/`. No vendor-side changes; the broker-adapter package owns no new modules in this story.

### 1\. `UnderlyingPriceCache` typed reader

`src/alphamind/execution/continuous_monitor/underlying_stream/cache.py`:

```python
@dataclass(frozen=True, slots=True)
class UnderlyingQuote:
    ticker: str
    price: float
    as_of: datetime  # tz-aware UTC

class UnderlyingPriceCache:
    """Thread-safe in-memory mapping of underlying ticker → latest quote.

    Readers (story 03a, 03b, 04c) call `get(ticker)` for the freshest tick
    or `get_all()` for a snapshot. Writers (this story's stream consumer)
    call `update(quote)`.
    """
    def get(self, ticker: str) -> UnderlyingQuote | None: ...
    def get_all(self) -> Mapping[str, UnderlyingQuote]: ...
    def update(self, quote: UnderlyingQuote) -> None: ...
```

Use an `asyncio.Lock` (or sufficient atomic primitives) for write safety; reads are lock-free against a snapshot of the underlying dict.

### 2\. Subscription manager

`src/alphamind/execution/continuous_monitor/underlying_stream/subscriptions.py`:

```python
async def compute_target_underlyings(
    repository: PortfolioStateRepository,
) -> frozenset[str]:
    """Return the set of underlying tickers that should currently be subscribed.

    Includes:
    * `position.ticker` for every open equity position.
    * `position.options_details.underlying_ticker` for every open options position.
    * Every leg's underlying for every open strategy position.
    """
```

Reads through the repository; pure inputs → output. No subscription side effects (those live in the consumer task).

### 3\. Stream consumer task

`src/alphamind/execution/continuous_monitor/underlying_stream/task.py`:

```python
async def run_underlying_stream(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    factory: AlpacaStreamFactory,  # built from creds; mockable in tests
) -> None:
    """Run-forever task. Builds a StockDataStream(feed=DataFeed.IEX), subscribes
    to compute_target_underlyings(repository), updates the cache on each quote,
    and re-evaluates the target set on a configurable cadence (default 30 s)
    to pick up newly-opened / newly-closed positions.
    """
```

Implementation notes:

* Construct `StockDataStream(api_key, secret_key, feed=DataFeed.IEX)`. Creds from `os.environ["ALPACA_PAPER_KEY"]` / `["ALPACA_PAPER_SECRET"]` (or `ALPACA_LIVE_KEY` / `ALPACA_LIVE_SECRET` when `session.mode == "live"`).
* Use an `asyncio.Queue` bridge so the alpaca-py handler enqueues quotes and the task drains them into the cache (same pattern as [ALP-384](https://linear.app/alphamind-jatassi/issue/ALP-384/02f-trade-updates-fill-stream-subscriber-fill-report-translator)'s `subscribe_trade_updates`).
* Diff the target set every `subscription_refresh_seconds` (new field on `ContinuousMonitorConfig`, default 30 s) and call `stream.subscribe_quotes(handler, *added)` + `stream.unsubscribe_quotes(*removed)` accordingly.
* On disconnect: catch the websocket exception, log, sleep with exponential backoff bounded by `config.max_reconnect_attempts`, and reconnect. If the budget exhausts, propagate the exception so the supervisor's per-task exit path fires.
* On `asyncio.CancelledError` (shutdown): call `stream.stop_ws()` and exit cleanly.

### 4\. Supervisor registration

In `src/alphamind/execution/continuous_monitor/__main__.py` (extending Story 01's entry point), construct the `UnderlyingPriceCache` and register the stream task under the name `"underlying_stream"`. The cache is injected into later tasks via a shared container (DI pattern — pass through to other tasks as they register).

### 5\. New config field

Add `subscription_refresh_seconds: int = 30` to `ContinuousMonitorConfig`; default to 30 s; field validator rejects non-positive.

### 6\. Tests at `tests/execution/continuous_monitor/underlying_stream/`

* `test_cache.py` — cache update / get behavior; concurrent updates are observable.
* `test_subscriptions.py` — `compute_target_underlyings` returns the union of equity tickers + options underlyings + strategy-leg underlyings from a fixture repository; empty when no open positions.
* `test_task.py` — using a fake `AlpacaStreamFactory` that injects synthetic quotes through the bridge queue:
  * Cache is populated as quotes arrive.
  * Target-set re-diff at the configured cadence triggers subscribe / unsubscribe calls.
  * Disconnect (synthetic) triggers reconnect with exponential backoff; cache survives.
  * Budget-exhausted reconnect propagates the exception out.
  * `asyncio.CancelledError` triggers `stop_ws()` and clean exit.

### Out of scope

* Reading the cache to compute `MarketInputs` — that's story 03b.
* Greeks recomputation when underlying moves — story 03a.
* Options bracket-stop trigger evaluation — story 04c.
* SIP feed support — `Literal["alpaca-iex"]` today; SIP is a future provider-string addition.
* Cache persistence across monitor restarts — the cache is in-memory; on restart the stream re-subscribes and rebuilds.

## Acceptance criteria

- [ ] `UnderlyingPriceCache` is importable from `alphamind.execution.continuous_monitor.underlying_stream`; `update` + `get` + `get_all` work as documented; concurrent writes preserve last-writer-wins semantics.
- [ ] `compute_target_underlyings(repository)` returns a `frozenset[str]` matching the union of open-position underlyings; returns the empty set when no positions are open; multiple positions on the same underlying produce a single set entry.
- [ ] `run_underlying_stream` constructs `StockDataStream(feed=DataFeed.IEX)` using paper-account creds (mode=paper) or live creds (mode=live).
- [ ] On startup, the task subscribes to the initial target set via `stream.subscribe_quotes(handler, *targets)`.
- [ ] Every quote received from the handler is translated into an `UnderlyingQuote(ticker, price, as_of)` with `as_of` tz-aware UTC and written to the cache.
- [ ] On every `subscription_refresh_seconds` tick, the task re-runs `compute_target_underlyings`, computes added / removed deltas, and issues `subscribe_quotes` / `unsubscribe_quotes` accordingly.
- [ ] On synthetic disconnect, the task retries with exponential backoff up to `config.max_reconnect_attempts`; cache values survive the disconnect.
- [ ] When the reconnect budget is exhausted, the task raises and the supervisor's exit logging records the failure.
- [ ] On `asyncio.CancelledError`, the task calls `stream.stop_ws()` and returns cleanly.
- [ ] `subscription_refresh_seconds` is added to `ContinuousMonitorConfig` with a positive-integer validator and a default of 30; `config/continuous_monitor.yaml` documents the value.
- [ ] Tests at `tests/execution/continuous_monitor/underlying_stream/` use a fake `AlpacaStreamFactory` (or alpaca-py's test harness) — no live websocket calls in tests.
- [ ] `uv run pytest tests/execution/continuous_monitor/underlying_stream/ -n auto` passes.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/underlying_stream/ -n auto -v`.
* Manual smoke (operator-run): start the monitor against the paper creds in a market-hours window, open a paper position in `SPY` via the broker adapter (or any other test mechanism), and confirm `monitor.log` shows a `subscribe_quotes(SPY)` entry followed by ongoing quote-cache updates. Off-hours, the connection + subscription should still succeed even though no quotes arrive (validated via the 2026-05-10 smoke test against `wss://stream.data.alpaca.markets/v2/iex`).
* Lint clean per CLAUDE.md.