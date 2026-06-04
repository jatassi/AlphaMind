# 02c — greeks_refresh freshness gate + watchdog

## Goal

`greeks_refresh` is the third price-cache consumer with no freshness gate — previously unfiled, surfaced by this epic. `_spot_for_position` reads `cache.get(ticker).price` and the cycle builds `{ticker: quote.price}` discarding `as_of`, so a wedged price stream makes greeks recompute against a frozen spot **and** can fire the move-based out-of-cadence trigger off a frozen price — and those greeks then feed `bracket_stops`' P/L triggers (02b). This story migrates greeks' spot reads onto 01b's freshness-aware reads so a stale/missing spot neither fires the move trigger nor anchors a recompute (it preserves prior greeks via the existing `refresh_failed` path), and drives the loop through 01a's `supervised_loop` — `greeks_refresh` does not beat today, so this brings it under the watchdog.

## Reading

* `src/alphamind/execution/continuous_monitor/greeks_refresh/task.py` — `_spot_for_position` (`cache.get(ticker).price`, no freshness); `_is_due` (the move-based trigger `|spot - anchor|`); the cycle's `{ticker: quote.price}` snapshot that drops `as_of`; `_build_failed_greeks` (the `refresh_failed=True` preserve-prior-greeks path to reuse); the run-forever loop (`while True` + sleep, no `beat` today).
* `src/alphamind/execution/continuous_monitor/greeks_refresh/wiring.py` — `register_task` (currently no heartbeat).
* `src/alphamind/execution/continuous_monitor/underlying_stream/cache.py` — 01b's `read` / `read_all` + `PriceRead` result type.
* `src/alphamind/config/models/continuous_monitor.py` — `underlying_price_max_age_seconds` (01b); `greeks_refresh_inspection_cadence_seconds`, `greeks_refresh_underlying_move_threshold_pct`.
* `src/alphamind/execution/continuous_monitor/supervisor.py` — 01a's `supervised_loop` seam.
* `ALP-825` — names greeks as the move-trigger consumer feeding `bracket_stops`' P/L triggers.

## Depends on

* 01a ([ALP-826](https://linear.app/alphamind-jatassi/issue/ALP-826/01a-per-cadence-default-on-stall-watchdog)) — `supervised_loop` seam + per-cadence watchdog.
* 01b ([ALP-827](https://linear.app/alphamind-jatassi/issue/ALP-827/01b-freshness-aware-underlyingpricecache-read-api)) — `read` / `read_all` + `PriceRead` + `underlying_price_max_age_seconds`.

## Scope

In scope: `src/alphamind/execution/continuous_monitor/greeks_refresh/task.py` and `greeks_refresh/wiring.py`. Tests at `tests/execution/continuous_monitor/greeks_refresh/test_task.py`.

### 1\. Freshness-gated spot reads

`_spot_for_position` resolves spot through `cache.read(ticker, as_of=now, max_age_seconds=config.underlying_price_max_age_seconds)`, and the cycle's snapshot uses `cache.read_all(tickers, as_of=now, max_age_seconds=...)` instead of `{ticker: quote.price}`. Only a `FRESH` result yields a usable spot.

### 2\. Gate the move trigger

`_is_due`'s move-based branch must not fire on a stale spot — a frozen price must not look like a 0% move *or* a large move. With no `FRESH` spot, the move trigger is inert for that position (the scheduled-interval trigger is unaffected; it is time-based, not price-based).

### 3\. Gate the recompute

A due position whose spot reads `STALE`/`MISSING` does not recompute greeks against a frozen price; it preserves prior greeks via the existing `refresh_failed=True` path (`_build_failed_greeks`) so downstream consumers widen their derivation-uncertainty buffer (the established "couldn't refresh" semantic), rather than emitting confident greeks off a dead price.

### 4\. Drive the loop through `supervised_loop`

Replace the run-forever `while True` + trailing sleep with `async for _ in supervisor.supervised_loop("greeks_refresh", config.greeks_refresh_inspection_cadence_seconds): <cycle>`, bringing greeks under the watchdog (it is currently unwatched).

### Out of scope

* Escalation/alert — the single global-stale signal is `breach_loop`'s (02d). This story skips/preserves + logs only.
* The IV provider, the closed-form recompute math, and the scheduled-interval trigger logic — unchanged.

## Acceptance criteria

- [ ] The move-based trigger does not fire for a position whose cached spot reads `STALE`/`MISSING` (no false out-of-cadence recompute off a frozen price).
- [ ] A due position with a `STALE`/`MISSING` spot preserves prior greeks with `refresh_failed=True` rather than recomputing against the stale spot.
- [ ] A position with a `FRESH` spot evaluates the move trigger and recomputes exactly as before.
- [ ] The scheduled-interval trigger still fires on schedule (time-based, independent of spot freshness).
- [ ] `greeks_refresh` runs via `supervised_loop` at `greeks_refresh_inspection_cadence_seconds` and is watched; a stalled cycle trips the watchdog.
- [ ] `uv run pytest tests/execution/continuous_monitor/greeks_refresh/test_task.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Unit tests over the greeks cycle with a cache seeded at controlled `as_of` offsets: a stale spot does not trigger a move-based recompute and a due-but-stale position yields `refresh_failed=True` prior greeks; a fresh spot recomputes normally; the scheduled trigger fires regardless. A watchdog test asserts greeks runs via `supervised_loop` and a stalled cycle trips.