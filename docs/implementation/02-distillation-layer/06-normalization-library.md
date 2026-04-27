---
status: not_started
completed_date:
commit_id:
---

# 06 — Normalization library

## Goal

Implement the deterministic normalization primitives that every per-category computation reuses: unit/time alignment, ATR-relative move expression, extended-hours confidence-discount tagging, and macro surprise framing. Pure functions — no I/O, no state.

## Reading

- `docs/design/02-distillation-layer/external.md` § 1 Normalization and formatting — the five primitives this library exposes
- `docs/design/01-data-layer/collector/storage.md` § Cross-cutting rules — UTC throughout, ISO 8601 TEXT timestamps, `session` column convention on `ohlcv_bars`, paired `adj_*`/`unadj_*` columns
- `docs/design/01-data-layer/external/quantitative.md` §§ 1f, 1g — ATR definition (14-period) and extended-hours confidence-discount semantics
- `docs/design/01-data-layer/external/quantitative.md` § 6 — macro release surprise framing (deviation from expectations rather than absolute level)

## Depends on

- 04 (calibration framework — extended-hours confidence is expressed as a multiplier, but the primitive is purely a normalization helper; no calibration tagging happens inside the library)
- 05 (output envelope — the macro surprise primitive returns values destined for `OutputBlock.payload`; no formatting concerns here)

## Scope

In scope: under `src/alphamind/distillation/normalization.py` —

- `to_et(timestamp_utc: datetime) -> datetime` — wraps `zoneinfo.ZoneInfo("America/New_York")`. Handles DST transparently. Validates the input is timezone-aware and UTC; raises if not.
- `align_to_window(timestamps: Sequence[datetime], window: Literal["15min", "1h", "4h", "1d", "1w"]) -> dict[datetime, datetime]` — given a sequence of UTC timestamps, return a mapping from each timestamp to its window-anchored ET start. The five window values match `ohlcv_bars.timeframe`. The week starts Monday 00:00 ET; the day starts at 00:00 ET. Used to align multi-source data to a common window.
- `atr_normalize(price_move: float, atr: float) -> float` — returns `price_move / atr`. Raises if `atr <= 0` (a non-positive ATR is a data error, not a runtime fallback). Caller decides absolute-value vs. signed.
- `compute_atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float` — Wilder's smoothed True Range over `period`. Returns the most recent ATR value. Raises if any sequence is shorter than `period + 1`. The sequences must be aligned (one entry per bar).
- `extended_hours_confidence_weight(session: Literal["pre_market", "regular", "after_hours", "overnight"]) -> float` — returns `1.0` for `regular` and `0.5` for `pre_market` / `after_hours` / `overnight`. The 0.5 weight is a named constant (`EXTENDED_HOURS_WEIGHT = 0.5`) referenced from this function. The weight is "blanket discount on thin-liquidity moves, not a per-metric judgment" per [external.md § 1](../../../design/02-distillation-layer/external.md#1-normalization-and-formatting).
- `macro_surprise(actual: float, consensus: float) -> float` — returns `actual - consensus` in the indicator's native units. Caller is responsible for sign convention (a positive surprise on CPI is hawkish, a positive surprise on jobless claims is dovish). The framing primitive is signed deviation; downstream computations apply directional interpretation.
- `macro_surprise_zscore(surprise: float, trailing_surprises: Sequence[float]) -> float` — returns the surprise as a z-score against its trailing distribution. Raises if `trailing_surprises` is empty. Caller passes the relevant trailing window (per `macro_surprise_percentile` window — typically 24 months of releases for the named indicator). The z-score is the input to the percentile-based anomaly threshold from `threshold-calibration.md`.
- A small `units.py` (sibling) defining named constants for the unit conventions: `BPS_PER_PCT = 100.0`, `PCT_TO_RATIO = 0.01`. Avoid magic numbers in callers.
- Unit tests:
  - `to_et` round-trips a known UTC moment to the expected ET datetime across both DST states (one summer date, one winter date).
  - `to_et` rejects a naive datetime.
  - `align_to_window` correctly anchors to ET boundaries for each window value (week-of-year, day-of-month, hourly, sub-hourly).
  - `compute_atr` returns the documented Wilder-smoothed value against a known fixture (use a 20-bar canonical example).
  - `compute_atr` raises on insufficient input.
  - `atr_normalize` rejects `atr <= 0`.
  - `extended_hours_confidence_weight` returns `1.0` for `regular`, `0.5` for the other three sessions, and raises for any other string.
  - `macro_surprise_zscore` raises on empty trailing window.

Out of scope:
- Per-category indicator computations (stories 08*).
- Persistent baseline maintenance (story 07).
- The actual ATR baseline state (lives in `distillation_ticker_baseline` from story 03; this library computes an ATR from raw bar inputs but doesn't read or write the baseline table).

## Notes

The library is intentionally pure: every function takes its inputs as arguments and returns its result. No SQLAlchemy session, no config object, no logger. Tests run in-process with no fixtures beyond plain Python data — keeps the contract obvious and the surface small.

`compute_atr` is a building block used by both per-ticker baseline maintenance (story 07/08a) and ad-hoc consumer-side normalization. Implement Wilder's smoothing (the EMA-with-α=1/period variant) rather than simple moving average — Wilder is the conventional ATR per quant 1c in the data spec.

The `extended_hours_confidence_weight` function is deliberately a constant lookup, not a calibration-aware computation. Per-ticker historical confirmation rates (story 08a / qualitative-derived story) override the blanket weight where available, but that override happens at the consumer; the library exposes the documented blanket discount.

`macro_surprise` and `macro_surprise_zscore` are split because not every consumer needs the z-score — some macro reads care about the raw signed deviation (e.g., for narrative briefs). The percentile-based anomaly check (`macro_surprise_percentile = 90`) consumes the z-score and compares against the 90th-percentile cut.

DST handling is the trickiest part. Use `zoneinfo` (Python 3.9+) and have tests pin one summer and one winter date so DST transitions don't silently break alignment. Do NOT use `pytz`.

## Acceptance criteria

- [ ] `to_et` converts UTC to ET correctly for both DST states; rejects naive datetimes.
- [ ] `align_to_window` anchors each of the five window values to the documented ET boundary.
- [ ] `compute_atr` returns Wilder-smoothed ATR for a known fixture; raises on insufficient input.
- [ ] `atr_normalize` returns `move / atr`; rejects `atr <= 0`.
- [ ] `extended_hours_confidence_weight` returns `1.0` / `0.5` per the documented mapping; rejects unknown session strings.
- [ ] `EXTENDED_HOURS_WEIGHT` is a named constant; not a magic number.
- [ ] `macro_surprise` returns signed deviation in native units.
- [ ] `macro_surprise_zscore` returns z-score against trailing window; raises on empty trailing window.
- [ ] All functions are pure (no I/O, no global state) — verifiable by inspection that no module-level mutable state exists and no SQLAlchemy / file / logger imports appear in the file.
- [ ] DST regression test pins one summer and one winter date.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
