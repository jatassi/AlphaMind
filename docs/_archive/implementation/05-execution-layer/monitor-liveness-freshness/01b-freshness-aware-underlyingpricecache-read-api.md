# 01b — Freshness-aware UnderlyingPriceCache read API

## Goal

Give `UnderlyingPriceCache` a freshness-aware read API so a price consumer cannot read a stale price as if it were live. Today `get()` / `get_all()` return a quote with no staleness contract (`underlying_stream/cache.py`), forcing every consumer to remember to gate on `as_of` — which `breach_loop` does ([ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during)) but `bracket_stops` and `greeks_refresh` do not. This story adds reads that return an explicit **Fresh / Stale / Missing** result carrying price and age (decision C), a single shared `underlying_price_max_age_seconds` threshold (decision A), and a cache-boundary **global-stale** detector that backs the one-signal escalation (decision D). This is the Defect B foundation that 02b / 02c / 02d consume.

## Reading

* `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` — `UnderlyingPriceCache`, `UnderlyingQuote` (`ticker`/`price`/`as_of`), `get`, `get_all`; the asyncio-lock single-writer model.
* `src/alphamind/execution/continuous_monitor/breach_loop/task.py` `_run_one_tick` — the hand-rolled [ALP-770](https://linear.app/alphamind-jatassi/issue/ALP-770/monitor-enforces-stops-against-a-frozen-stale-price-sentinel-during) staleness gate (age vs `max_price_age_seconds`, the `stale_tickers`/`missing_tickers`/`expected_tickers` shape) the new reads must support.
* `src/alphamind/execution/continuous_monitor/breach_loop/result.py` — `BreachLoopHealthSignal` (the value-object shape to mirror for the global-stale signal, not duplicate).
* `src/alphamind/portfolio_state/freshness.py` — `SnapshotFreshness`, `is_position_price_stale` (existing freshness vocabulary; align the result variant names with it, do not re-implement).
* `src/alphamind/config/models/continuous_monitor.py` + `config/continuous_monitor.yaml` — where the shared threshold field lands; `breach_loop`'s current `max_price_age_seconds` default.
* `ALP-770` — the freshness semantics (stale excluded + escalation) this API must let consumers reproduce.

## Depends on

* None — foundation.

## Scope

In scope: `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` (the result type + signal type are defined in this module — no new module); `src/alphamind/config/models/continuous_monitor.py`; `config/continuous_monitor.yaml`. Tests at `tests/execution/continuous_monitor/underlying_stream/test_cache.py`.

### 1\. Freshness result type

Model the read outcome as a **frozen-dataclass union** discriminated by a freshness enum (align the enum member / variant names with `portfolio_state/freshness.py` vocabulary), with three variants: `FRESH` carrying `price: float` + `as_of: datetime`; `STALE` carrying `price: float` + `as_of: datetime` + `age_seconds: float`; `MISSING` carrying no price. Expose them under a `PriceRead` union alias the consumer pattern-matches on. The shape must make the safety-critical caller's "only `FRESH` yields a usable `price`" the path of least resistance — a `STALE`/`MISSING` result does not expose `price` through the same attribute a `FRESH` read does.

### 2\. Freshness-aware reads

Add `read(ticker, *, as_of, max_age_seconds) -> PriceRead` for one ticker, and `read_all(tickers, *, as_of, max_age_seconds) -> Mapping[str, PriceRead]` for the open set (the `breach_loop` iteration shape), marking any requested ticker absent from the cache as `MISSING`. These are the enforcement-path reads. Keep the raw `get` / `get_all` unchanged for non-enforcement readers; retiring them is explicitly out of scope (a follow-up once no enforcement path uses them).

### 3\. Shared freshness threshold

Add `underlying_price_max_age_seconds` to `ContinuousMonitorConfig`, sourced from `config/continuous_monitor.yaml`. It is the single threshold all price consumers pass to the reads, replacing `breach_loop`'s hardcoded default (02d removes that default and reads this).

### 4\. Global-stale detector + signal

Add a cache-boundary method `global_staleness(*, as_of, max_age_seconds, expected_tickers) -> <signal> | None` whose condition is precisely: **every ticker in** `expected_tickers` **reads** `STALE` **or** `MISSING` (the writer-wedged case — the whole feed is cold). It returns `None` when at least one expected ticker reads `FRESH`, and `None` when `expected_tickers` is empty (no positions → nothing to escalate). Define the one health-signal value object it returns (mirror `BreachLoopHealthSignal`'s shape). This story provides the detector + signal type and exposes them; the single emitter is wired in 02d through `breach_loop`'s existing health channel (decision D) — no emitter here.

### Out of scope

* Migrating any consumer onto these reads — `bracket_stops` (02b), `greeks_refresh` (02c), `breach_loop` (02d) each adopt them.
* Emitting the global-stale signal — 02d wires the single emitter.

## Acceptance criteria

- [ ] `read` of a ticker whose `as_of` is within `max_age_seconds` returns the `FRESH` variant with the price; one older than the threshold returns `STALE` with the price and a positive `age_seconds`; an absent ticker returns `MISSING`.
- [ ] The union does not expose a `price` on the `STALE`/`MISSING` variants through the same attribute the `FRESH` variant uses (a consumer is forced to branch).
- [ ] `read_all` returns one `PriceRead` per requested ticker, marking absent tickers `MISSING`.
- [ ] `global_staleness` returns the signal iff every expected ticker reads `STALE`/`MISSING`, and `None` when any is `FRESH` or when `expected_tickers` is empty.
- [ ] `underlying_price_max_age_seconds` loads from `config/continuous_monitor.yaml`; `tests/config/test_snapshot.py` is re-pinned for the new field.
- [ ] `uv run pytest tests/execution/continuous_monitor/underlying_stream/test_cache.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Unit tests over the cache with hand-built `UnderlyingQuote`s at controlled `as_of` offsets from a fixed `as_of`: assert the three `read` variants, `read_all`'s per-ticker mapping incl. absent→`MISSING`, and `global_staleness` at its true (all stale/missing) / false (one fresh) / empty boundaries. Re-pin the config snapshot.