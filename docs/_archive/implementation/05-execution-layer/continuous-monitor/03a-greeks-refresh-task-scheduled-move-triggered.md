# 03a — Greeks refresh task

## Goal

Maintain the freshness of `OptionGreeks` on every open option / strategy position. The task fires on whichever trigger comes first per the design: scheduled (every `greeks_refresh_interval_minutes`, default 15) or move-based (underlying drifts ≥ `greeks_refresh_underlying_move_threshold_pct` from the price at last refresh, default 2.0%). For each due position, fetch IV from the collector-populated `options_chains` table, recompute greeks via the guardrail-evaluation library's Black-Scholes core, and write `delta`, `gamma`, `theta`, `vega`, `as_of_timestamp`, `iv_used`, `refresh_failed=False` to the position record. On IV-fetch failure, retry briefly and on exhaustion mark `refresh_failed=True` while preserving the prior greeks and emit a `greeks_refresh_failed` activity-log event.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4d Greeks refresh orchestration — full refresh-trigger + failure semantics
* `src/alphamind/portfolio_state/records/positions.py` § `OptionGreeks` — freshness fields (`as_of_timestamp`, `iv_used`, `refresh_failed`) landed via [ALP-339](https://linear.app/alphamind-jatassi/issue/ALP-339/01f-optiongreeks-freshness-metadata-sign-convention-docs)
* `src/alphamind/risk_guardrails/guardrail_evaluation/` — Black-Scholes core; the function that computes greeks given `(spot, strike, expiration, contract_type, iv, risk_free_rate, as_of)` is the same primitive guardrail-evaluation calls at OPEN/ADD validation. Read its signature; do not re-implement.
* `src/alphamind/data_sources/polygon/options.py` — what the collector writes to `options_chains` per ticker / expiration / strike, including the IV column
* `docs/design/01-data-layer/collector/storage.md` § Options chains — table schema and freshness expectations
* Parent issue `ALP-123` § Pre-resolved decisions (C), (E) — IV source choice (collector-populated `options_chains`) and the two refresh-trigger knobs
* `ALP-339` (`01f — OptionGreeks freshness metadata + sign convention docs`) — the additive fields this story populates; sign-convention reference for `theta`
* `src/alphamind/execution/continuous_monitor/underlying_stream/` (story 02b) — `UnderlyingPriceCache` reader for the move-based trigger and as a spot input to Black-Scholes
* `src/alphamind/portfolio_state/repository.py` — the writer surface for updating an `OptionsPositionDetails.greeks` field; story 03a touches the existing writeback path or adds a narrow helper

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — supervisor + config
* [ALP-434](https://linear.app/alphamind-jatassi/issue/ALP-434/02b-live-underlying-price-stream-task-alpaca-stockdatastream-iex) (02b) — `UnderlyingPriceCache` for the move trigger and spot input

## Scope

Source under `src/alphamind/execution/continuous_monitor/greeks_refresh/` (new sub-package). Tests at `tests/execution/continuous_monitor/greeks_refresh/`. Repository writer extension (if needed) under `src/alphamind/portfolio_state/repository.py`.

### 1\. `LastRefreshState` per-position tracking

`src/alphamind/execution/continuous_monitor/greeks_refresh/state.py` — in-memory `dict[PositionId, LastRefreshState]` with fields:

```python
@dataclass(frozen=True, slots=True)
class LastRefreshState:
    position_id: str
    last_refreshed_at: datetime
    underlying_price_at_last_refresh: float
```

The task seeds this from each position's existing `OptionGreeks.as_of_timestamp` on startup (positions with `as_of_timestamp is None` are treated as "due now").

### 2\. `IVProvider` typed reader

`src/alphamind/execution/continuous_monitor/greeks_refresh/iv_provider.py`:

```python
@dataclass(frozen=True, slots=True)
class IVQuote:
    occ_symbol: str
    iv: float
    as_of: datetime  # tz-aware UTC; the options_chains row timestamp

async def fetch_iv_from_options_chains(
    session: AsyncSession, *, occ_symbol: str,
) -> IVQuote | None:
    """Read the latest options_chains row for the given OCC symbol; return None
    if no row exists (covered position with no chain data yet).
    """
```

Pure async; SQLAlchemy query against the `options_chains` table. The column names must match what `polygon.options` writes (read the collector's writer to confirm before authoring the SELECT).

### 3\. Greeks recomputation

`src/alphamind/execution/continuous_monitor/greeks_refresh/recompute.py`:

```python
def recompute_greeks(
    *, position: PositionRecord, iv: float, spot: float, as_of: datetime,
    risk_free_rate: float,
) -> OptionGreeks:
    """Call the guardrail-evaluation Black-Scholes primitive and assemble the
    resulting greeks into an OptionGreeks with as_of_timestamp=as_of,
    iv_used=iv, refresh_failed=False.
    """
```

Pure synchronous function; no I/O. For strategy positions, recompute each leg's greeks and aggregate per the existing `StrategyPositionDetails.strategy_greeks` semantics — re-use the helper Sergei's guardrail evaluation library already provides for OPEN/ADD validation; do not invent new aggregation logic.

### 4\. Refresh orchestrator

`src/alphamind/execution/continuous_monitor/greeks_refresh/task.py`:

```python
async def run_greeks_refresh(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    session_factory: Callable[[], AsyncSession],
    risk_free_rate_provider: Callable[[], float],  # from existing config; fixed per session
) -> None:
    """Run-forever task. Every loop iteration (configurable; default 30 s
    inspection cadence):

    1. List open option + strategy positions from the repository.
    2. For each, compute (now - last_refreshed_at) and (|current_spot -
       price_at_last_refresh| / price_at_last_refresh).
    3. Pick positions where either trigger fires:
         - now - last_refreshed_at >= greeks_refresh_interval_minutes
         - move pct >= greeks_refresh_underlying_move_threshold_pct
    4. For each due position:
         - Fetch IV via fetch_iv_from_options_chains.
         - If IV fetch fails, retry up to N times with exponential backoff
           (mirror broker-adapter retry shape).
         - On exhaustion: write refresh_failed=True to OptionGreeks while
           preserving prior delta/gamma/theta/vega and the prior as_of;
           emit a greeks_refresh_failed activity-log event with the
           underlying ticker + the as_of of the prior refresh.
         - On success: recompute greeks via recompute_greeks; write the
           full updated OptionGreeks to the position record; update
           LastRefreshState for this position.
    5. Sleep until the next inspection tick. Outside market hours, pause
       the loop (no spot stream; theta still accumulates via the formula's
       Δt). Use the venue calendar from broker-adapter (already shipped in
       ALP-121) to detect market hours.
    """
```

Add `greeks_refresh_inspection_cadence_seconds: int = 30` to `ContinuousMonitorConfig` for the per-loop inspection rate (separate from `greeks_refresh_interval_minutes` which is the per-position refresh interval).

### 5\. Repository writer

If `PortfolioStateRepository` does not already have a narrow `update_position_greeks(position_id, greeks)` helper, add one. Use the existing per-position writeback path; do not invent new persistence patterns.

### 6\. Activity-log event

Add `EventType.GREEKS_REFRESH_FAILED = "GREEKS_REFRESH_FAILED"` (additive enum value) and a `GreeksRefreshFailedDetail` Pydantic detail type:

```python
class GreeksRefreshFailedDetail(BaseModel):
    model_config = ConfigDict(frozen=True)
    underlying_ticker: str
    occ_symbol: str
    failure_reason: str  # short string: "iv_fetch_timeout" | "iv_fetch_404" | ...
    prior_as_of: datetime | None  # the as_of_timestamp of the greeks now preserved
```

Update `EVENT_DETAIL_TYPES` accordingly. Append-only enum / mapping change.

### 7\. Supervisor registration

Extend `__main__.py` to construct `risk_free_rate_provider` (from existing config — search for how guardrail-evaluation reads its `risk_free_rate` today) and register the task as `"greeks_refresh"`.

### 8\. Tests at `tests/execution/continuous_monitor/greeks_refresh/`

* `test_state.py` — `LastRefreshState` defaults seed from existing `OptionGreeks.as_of_timestamp`; positions with `as_of_timestamp is None` are treated as due now.
* `test_iv_provider.py` — `fetch_iv_from_options_chains` returns the latest row's IV when one exists; `None` when no row matches; tz-aware `as_of`.
* `test_recompute.py` — `recompute_greeks` for a single-leg long call produces a positive delta, positive gamma, **negative theta** (per [ALP-339](https://linear.app/alphamind-jatassi/issue/ALP-339/01f-optiongreeks-freshness-metadata-sign-convention-docs) sign convention), positive vega.
* `test_task.py` — using fakes for the repository, cache, and `fetch_iv_from_options_chains`:
  * Scheduled trigger: position with `last_refreshed_at` older than the interval is picked up; greeks updated; activity log untouched on success.
  * Move trigger: position whose underlying moved > the threshold is picked up even when within the interval.
  * IV fetch failure: position with failing IV fetch → `refresh_failed=True`, prior `delta/gamma/theta/vega/as_of` preserved, activity-log entry with `GREEKS_REFRESH_FAILED` event type emitted.
  * Off-hours pause: a fixture clock outside market hours produces no refreshes.
  * Strategy position: a multi-leg strategy refreshes per-leg, and the aggregated `strategy_greeks` is updated.

### Out of scope

* Live underlying-price stream subscription — story 02b.
* IV-fetch retry shape — reuse the broker-adapter's retry helper.
* Greeks consumption by the breach loop (`MarketInputs.iv_provider`) — story 03b reads the fresh greeks from the repository; story 03a only writes.
* Per-refresh IV sourcing from a separate vendor pull — deferred (per parent decision C).

## Acceptance criteria

- [ ] `LastRefreshState`, `IVQuote`, `IVProvider`, and `recompute_greeks` are importable from `alphamind.execution.continuous_monitor.greeks_refresh`.
- [ ] `fetch_iv_from_options_chains(session, occ_symbol=...)` returns the latest IV row for the OCC symbol; returns `None` when no row exists.
- [ ] `recompute_greeks` for a long call returns positive `delta`, positive `gamma`, negative `theta`, positive `vega`, with `iv_used == iv`, `refresh_failed=False`, `as_of_timestamp == as_of`.
- [ ] `run_greeks_refresh` triggers on the **scheduled** path: when `now - last_refreshed_at >= interval`, the position is refreshed and `last_refreshed_at` updates.
- [ ] `run_greeks_refresh` triggers on the **move** path: when `|spot - price_at_last_refresh| / price_at_last_refresh >= threshold`, the position is refreshed.
- [ ] On IV-fetch failure (after retries exhausted), `OptionGreeks.refresh_failed` is set `True`, `as_of_timestamp` and the four greeks are left at their prior values, and an `EventType.GREEKS_REFRESH_FAILED` activity-log entry is emitted with `GreeksRefreshFailedDetail`.
- [ ] `EventType.GREEKS_REFRESH_FAILED` and `GreeksRefreshFailedDetail` are added additively; existing event-detail mappings still pass; `tests/portfolio_state/events/` covers the new entries.
- [ ] Outside market hours (per the venue calendar), the loop pauses — no refreshes are written.
- [ ] Strategy positions: per-leg greeks refresh and the aggregated `strategy_greeks` is updated through the same code path the guardrail-evaluation library uses at OPEN/ADD.
- [ ] `greeks_refresh_inspection_cadence_seconds` is added to `ContinuousMonitorConfig` with default 30 and positive-integer validator.
- [ ] Tests at `tests/execution/continuous_monitor/greeks_refresh/` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/greeks_refresh/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/greeks_refresh/ -n auto -v`.
* `uv run pytest -n auto` — full suite green.
* Manual smoke during story 05's e2e verification: start the monitor with a fixture options position; observe the refresh fire on the interval and on a synthetic move > threshold.
* Lint clean per CLAUDE.md.