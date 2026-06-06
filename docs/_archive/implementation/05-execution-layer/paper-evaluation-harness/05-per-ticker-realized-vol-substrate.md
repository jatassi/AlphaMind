# 05 — Per-ticker realized-vol substrate

## Goal

Stands up the missing per-ticker realized-volatility producer so the paper-evaluation harness wedge in story 04a ([ALP-528](https://linear.app/alphamind-jatassi/issue/ALP-528/04a-wedge-integration-into-fill-stream-consumer-paper-mode-only)) actually produces non-trivial estimates instead of gracefully no-opping on an empty `Mapping[str, RealizedVolEntry]`. Ships five concrete deliverables: a pure compute function, a new SQL table + migration + ORM, a per-invocation persister wired into the distillation orchestrator, a read function returning a `Mapping[str, RealizedVolEntry]`, and the construction-site swap-ins at three production sites (`phase1_inputs.py`, `continuous_monitor/__main__.py` ✕ 2).

Status is Backlog at draft time so the work tree (stories 01–04b) can dispatch and validate without this. Promote to Todo when ready to dispatch this story alongside or after 04a.

## Reading

* `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py:76` — `RealizedVolEntry(underlying, trailing_30d_realized_vol)` typed shape (consumer-side; record exists)
* `src/alphamind/risk_guardrails/guardrail_evaluation/iv_sourcing.py:88` § `FixtureIvProvider.__init__` — the consumer that takes `realized_vol: Mapping[str, RealizedVolEntry]`
* `src/alphamind/distillation/q7/_helpers_compute.py` § `_log_returns_from_closes` — existing log-returns helper; reuse, do NOT re-implement
* `src/alphamind/distillation/orchestrator.py:631` § `_realized_vols_from_log_returns` — the existing market-wide `(short, long)` tuple producer; this story mirrors its formula at per-ticker granularity
* `src/alphamind/distillation/orchestrator.py` — the per-invocation entry that this story's persister hooks into; identify the right call-site near the existing per-ticker indicator computes (q1/q3/q6/q7 loaders already iterate the universe — co-locate the new producer with that iteration)
* `src/alphamind/persistence/models.py` § existing distillation tables — pattern to mirror for the new `TickerRealizedVolRow` mapping
* `src/alphamind/persistence/migrations/versions/` — existing migration style (atomic `_add_<table>.py` files, `upgrade()` + `downgrade()`)
* `src/alphamind/scheduler/phase1_inputs.py:202` — wiring site #1: `iv_provider=FixtureIvProvider(surface={}, realized_vol={})` for Reg T attribution's `MarketInputs`
* `src/alphamind/execution/continuous_monitor/__main__.py:387` — wiring site #2: same empty FixtureIvProvider for the greeks-refresh / breach-loop IV-fallback path
* `src/alphamind/execution/continuous_monitor/__main__.py` § fill_stream_consumer construction (post-04a) — wiring site #3: 04a's `realized_vol_map = {}` for the harness `MapVolLookup`
* `src/alphamind/execution/paper_evaluation_harness/lookups.py` (will exist post-[ALP-528](https://linear.app/alphamind-jatassi/issue/ALP-528/04a-wedge-integration-into-fill-stream-consumer-paper-mode-only)) — `MapVolLookup(mapping)` adapter that wraps the Mapping this story populates

## Depends on

* `ALP-528` (story 04a) — defines the `MapVolLookup` adapter and the `realized_vol_map` construction at the third wiring site (`continuous_monitor/__main__.py` fill_stream_consumer construction). This story replaces the empty-map default at all three sites; the third one only exists after 04a lands.

## Scope

Five deliverables. All under `src/alphamind/distillation/realized_vol.py` (new — single module), `src/alphamind/persistence/`, `src/alphamind/scheduler/`, and `src/alphamind/execution/continuous_monitor/`. Tests at `tests/distillation/test_realized_vol.py` and extensions to `tests/scheduler/test_phase1_inputs.py` + `tests/execution/continuous_monitor/test_main.py` (or wherever the existing wiring-site tests live).

### 1\. Pure compute function

New module `src/alphamind/distillation/realized_vol.py` with:

```python
def compute_trailing_realized_vol(
    closes: tuple[float, ...],
    *,
    min_returns: int = 5,
    annualization_factor: float = 252.0,
) -> float | None: ...
```

* Compute log returns via the existing `_log_returns_from_closes` from `distillation/q7/_helpers_compute.py` (import; do NOT re-implement).
* If `len(log_returns) < min_returns`, return `None` (insufficient data).
* Otherwise return `std(log_returns, ddof=1) * sqrt(annualization_factor)`. Sample-std (`ddof=1`) matches the convention `RealizedVolEntry`'s docstring describes; document the choice inline.
* Pure: no I/O, no module-level config imports. Constants for `min_returns` and `annualization_factor` are defaults; the per-invocation caller passes 30-day-derived input but the function is horizon-agnostic.

The function is named `compute_trailing_realized_vol` (not `compute_trailing_30d_realized_vol`) because the caller controls the horizon by choosing how many closes to pass. The persister deliverable (#3) passes 30 trading days.

### 2\. SQL table + migration + ORM

* New table `ticker_realized_vol` with schema:
  * `ticker: TEXT NOT NULL`
  * `as_of_date: TEXT NOT NULL` (ISO date, e.g., `2026-05-17`)
  * `trailing_30d_realized_vol: REAL NOT NULL`
  * `invocation_id: TEXT NOT NULL` (FK to `invocations.invocation_id`, `ON DELETE RESTRICT`)
  * `computed_at: TEXT NOT NULL` (ISO datetime)
  * `PRIMARY KEY (ticker, as_of_date)` — at most one row per ticker per trading day; same-day re-invocations upsert
  * Index `ix_ticker_realized_vol_as_of_date` on `as_of_date` for the read path's "latest per ticker" query
* New Alembic migration `src/alphamind/persistence/migrations/versions/<hash>_add_ticker_realized_vol.py` with `upgrade()` creating the table + indexes and `downgrade()` dropping them. Mirror the migration style of `f7a9d3c2e5b1_add_fill_records_and_ca_ledger.py`.
* New SQLAlchemy mapped class `TickerRealizedVolRow` in `src/alphamind/persistence/models.py` (alongside the existing distillation tables). Frozen column shape per the schema above.

### 3\. Per-invocation persister

New function in `src/alphamind/distillation/realized_vol.py`:

```python
async def persist_per_ticker_realized_vol(
    handle: InvocationHandle,
    *,
    tickers: Iterable[str],
    closes_by_ticker: Mapping[str, tuple[float, ...]],
    as_of_date: date,
    lookback_days: int = 30,
) -> int: ...
```

* For each ticker in `tickers`, take the trailing `lookback_days` closes from `closes_by_ticker[ticker]`.
* Call `compute_trailing_realized_vol`. If the result is None (insufficient data), skip the ticker.
* Upsert into `ticker_realized_vol` on the `(ticker, as_of_date)` PK (`ON CONFLICT DO UPDATE` for the `trailing_30d_realized_vol`, `invocation_id`, and `computed_at` columns). Use the existing SQLAlchemy upsert pattern (matches `regt_margin_attribution`'s pattern in `phase1.py`).
* Return the count of rows written.
* All writes join `handle.session`'s transaction — surrounding `InvocationContext` commits on clean exit.

Wire into the distillation orchestrator: identify where the orchestrator iterates the per-ticker universe (the q1/q3/q6/q7 loaders already pull `closes_by_ticker` for the universe — co-locate the call so the closes are loaded once and re-used). The subagent finds the right insertion point and threads `closes_by_ticker` through.

### 4\. Read function

New function in `src/alphamind/distillation/realized_vol.py`:

```python
async def read_realized_vol_map(
    session: AsyncSession,
    *,
    tickers: Iterable[str] | None = None,
    as_of_date: date | None = None,
) -> Mapping[str, RealizedVolEntry]: ...
```

* If `as_of_date` is None, returns the latest row per ticker (subquery: `MAX(as_of_date) GROUP BY ticker`).
* If `tickers` is None, returns entries for every ticker in the table; otherwise filters to the provided set.
* Returns a `dict[str, RealizedVolEntry]`. Empty dict when no rows match.
* Pure read; no writes.

### 5\. Wiring at three production sites

Replace each empty-map construction with a populated one:

* `src/alphamind/scheduler/phase1_inputs.py:202`:

  ```python
  realized_vol_map = await read_realized_vol_map(
      handle.session,
      tickers=tuple(p.symbol for p in positions),
  )
  iv_provider=FixtureIvProvider(surface={}, realized_vol=realized_vol_map)
  ```

  The position-restricted tickers keep the map size bounded.
* `src/alphamind/execution/continuous_monitor/__main__.py:387` (greeks-refresh / breach-loop FixtureIvProvider):

  ```python
  async with session_factory() as db:
      realized_vol_map = await read_realized_vol_map(db, tickers=open_position_underlyings)
  iv_provider = FixtureIvProvider(surface={}, realized_vol=realized_vol_map)
  ```

  Refresh cadence: on monitor startup, then re-fetch on a daily timer (the producer runs at invocation cadence, but the monitor stays long-lived). Add a 24h refresh task in the monitor's runtime so the map doesn't go stale on a multi-day continuous run.
* `src/alphamind/execution/continuous_monitor/__main__.py` § fill_stream_consumer construction (post-04a):

  ```python
  realized_vol_map = await read_realized_vol_map(db, tickers=open_position_underlyings)
  vol_lookup = MapVolLookup(realized_vol_map)
  ```

  Same daily-refresh treatment as the greeks-refresh map. Or share a single cached Mapping object across both consumers — coordinate with the existing monitor runtime to avoid two duplicate fetches.

### Out of scope

* IV-surface production. `surface={}` stays empty; this story only populates `realized_vol={}`.
* Backfilling historical per-ticker vol for the existing fill_records. The harness annotates fills going forward; pre-existing fills' `live_execution_estimate_json` stays NULL.
* Modeling experiments around the vol horizon (5d / 20d / 30d). 30d matches the `RealizedVolEntry` docstring; revisit only with evidence.
* Computing realized vol for non-equity instruments (options, strategy). The map is keyed by equity underlying ticker; options inherit their underlying's vol via the harness composition (story 03 already handles this — it passes `realized_volatility` keyed on the underlying).
* Modifying `FixtureIvProvider` itself. The existing fallback chain (surface → realized_vol → error) is unchanged; this story just supplies a non-empty `realized_vol`.

## Acceptance criteria

- [ ] `compute_trailing_realized_vol(closes)` returns a non-negative float for `len(closes) >= 6` (5 returns) with non-zero variance.
- [ ] `compute_trailing_realized_vol(closes)` returns `None` for `len(closes) < min_returns + 1` (insufficient data).
- [ ] `compute_trailing_realized_vol` is monotone-increasing in input volatility: a synthetic constant-volatility series at 0.30 produces a larger result than the same series at 0.20.
- [ ] The new Alembic migration creates `ticker_realized_vol` with the documented schema; `alembic upgrade head` and `alembic downgrade -1` both succeed against a fresh DB.
- [ ] `TickerRealizedVolRow` is importable from `alphamind.persistence.models`.
- [ ] `persist_per_ticker_realized_vol` writes one row per ticker into `ticker_realized_vol`, skipping tickers with insufficient closes.
- [ ] Same-day re-invocations of `persist_per_ticker_realized_vol` upsert (no duplicate-PK error; latest values overwrite).
- [ ] `read_realized_vol_map(session)` returns a populated `dict[str, RealizedVolEntry]` after the persister has written rows.
- [ ] `read_realized_vol_map(session, tickers=("AAPL",))` filters to the requested set.
- [ ] `read_realized_vol_map(session, as_of_date=date(2020, 1, 1))` returns rows pinned to that as_of_date (when present) or empty (when absent).
- [ ] After this story, `phase1_inputs.gather_phase1_inputs` constructs `FixtureIvProvider` with a non-empty `realized_vol` Mapping for every open-position underlying that has a row in `ticker_realized_vol`.
- [ ] `continuous_monitor/__main__.py` startup constructs both the greeks-refresh `FixtureIvProvider` and the harness `MapVolLookup` with non-empty maps; the maps refresh on a daily timer.
- [ ] After the distillation orchestrator runs once on a universe with sufficient close-price history, `verify_debug_e2e.py` produces at least one paper-mode fill with a non-null `live_execution_estimate_json` in `fill_records` (sanity check for the end-to-end path).
- [ ] `uv run pytest --testmon -n auto` passes; drop `--testmon` for the final verification per CLAUDE.md.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all clean.
- [ ] import-linter contract: `distillation/realized_vol.py` does NOT import from `risk_guardrails/`, `execution/`, or `scheduler/`. The `RealizedVolEntry` import direction goes the other way (the wiring sites import the read function).

## Verification

Scoped tests (`tests/distillation/test_realized_vol.py` for compute + persister + read; extensions to phase1_inputs and continuous_monitor wiring tests for the integration). End-to-end sanity via `scripts/verify_debug_e2e.py` after the producer runs once.

## Deferred at drafting time, but dispatch-ready

This story is intentionally drafted with full implementation detail so it can be dispatched alongside the harness work tree if the operator wants the harness to produce real per-fill estimates immediately. Default state is Backlog so the work tree (01–04b) lands independently first; promote to Todo to include in dispatch.

When dispatched: model selection Opus (per-ticker integration into the distillation orchestrator's iteration site requires judgment; migration design is mechanical-leaning but the orchestrator hook is not). Wedges into the dependency graph as `04a → 05` (parallel with 04b).