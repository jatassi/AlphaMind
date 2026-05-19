"""Deterministic normalization primitives — story 02-distillation/06.

The seven pure functions exposed here are the unit/time/volatility/macro
normalizers every per-category computation reuses. Each takes its inputs as
arguments and returns its result; no I/O, no module-level mutable state, no
SQLAlchemy / file / logger imports — the contract is the function signature
and the function body.

Reference docs:

- ``docs/design/02-distillation-layer/external.md`` § 1 Normalization and
  formatting — the five primitive families this library exposes.
- ``docs/design/01-data-layer/external/quantitative.md`` §§ 1f, 1g — ATR
  definition (Wilder, 14-period default) and the extended-hours
  confidence-discount semantics.
- ``docs/design/01-data-layer/external/quantitative.md`` § 6 — macro release
  surprise framing (deviation from expectations rather than absolute level).
- ``docs/design/01-data-layer/collector/storage.md`` § Cross-cutting rules —
  UTC throughout the storage layer; ET conversion is a presentation concern
  handled here.

DST handling uses :mod:`zoneinfo` (Python 3.9+); :mod:`pytz` is deliberately
avoided per the story-06 spec.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Time-zone constants
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")
"""Eastern time zone with DST handled by the platform IANA database.

Used for every UTC→ET conversion and every ET-anchored window-alignment
boundary.
"""


# ---------------------------------------------------------------------------
# Window vocabulary
# ---------------------------------------------------------------------------

Window = Literal["15min", "1h", "4h", "1d", "1w"]
"""The five window values supported by :func:`align_to_window`.

The vocabulary mirrors ``ohlcv_bars.timeframe`` per
``docs/design/01-data-layer/collector/storage.md`` § Cross-cutting rules so
multi-source data shares the same anchoring grid as the bar table.
"""


_WINDOW_DURATION: dict[Window, timedelta] = {
    "15min": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}
"""Sub-day windows expressed as a fixed offset from the day-start ET boundary.

The day and week windows anchor differently (calendar-day midnight ET, ISO
Monday midnight ET) so they are not entries in this map; the dispatch in
:func:`align_to_window` handles them by branch rather than offset.
"""


# ---------------------------------------------------------------------------
# Session vocabulary and confidence-weight constant
# ---------------------------------------------------------------------------

Session = Literal["pre_market", "regular", "after_hours", "overnight"]
"""The four session values per ``ohlcv_bars.session`` in storage.md.

Matches the storage-layer column convention so the distillation layer reads
the session column directly without remapping.
"""


EXTENDED_HOURS_WEIGHT: float = 0.5
"""Blanket confidence discount applied to extended-hours metrics.

Per ``docs/design/02-distillation-layer/external.md`` § 1 Normalization and
formatting: "blanket discount on thin-liquidity moves, not a per-metric
judgment." A constant rather than a literal so the discipline is searchable
and the name carries the rationale.
"""


# ---------------------------------------------------------------------------
# UTC → ET conversion
# ---------------------------------------------------------------------------


def to_et(timestamp_utc: datetime) -> datetime:
    """Convert a UTC datetime to America/New_York wall-clock time.

    DST is handled by :mod:`zoneinfo`'s IANA database — the same UTC instant
    converts differently in summer (UTC-4) and winter (UTC-5).

    Raises :class:`ValueError` if the input is naive (no timezone) or carries
    a non-UTC offset; these cases are caller bugs rather than runtime
    fallbacks because the storage layer is UTC throughout per
    ``docs/design/01-data-layer/collector/storage.md`` § Cross-cutting rules.
    """
    if timestamp_utc.tzinfo is None:
        raise ValueError(
            f"to_et requires a timezone-aware UTC datetime; got naive {timestamp_utc!r}"
        )
    if timestamp_utc.utcoffset() != timedelta(0):
        raise ValueError(
            f"to_et requires UTC; got offset {timestamp_utc.utcoffset()!r} on {timestamp_utc!r}"
        )
    return timestamp_utc.astimezone(_ET)


# ---------------------------------------------------------------------------
# Window alignment
# ---------------------------------------------------------------------------


def _anchor_in_et(et_timestamp: datetime, window: Window) -> datetime:
    """Return the window-start ET datetime for one already-converted ET timestamp."""
    day_start = et_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "1d":
        return day_start
    if window == "1w":
        # Monday is weekday() == 0; subtract that many days to land on Monday.
        return day_start - timedelta(days=day_start.weekday())
    duration = _WINDOW_DURATION[window]
    bucket_index = int((et_timestamp - day_start) // duration)
    return day_start + duration * bucket_index


def align_to_window(
    timestamps: Sequence[datetime],
    window: Window,
) -> dict[datetime, datetime]:
    """Map each UTC timestamp to its window-anchored ET start.

    The five window values match ``ohlcv_bars.timeframe`` per
    ``docs/design/01-data-layer/collector/storage.md`` § Cross-cutting rules.
    Day windows anchor to 00:00 ET on the same ET calendar day; week windows
    anchor to Monday 00:00 ET of the containing ISO week; sub-day windows
    anchor to a fixed-offset grid starting at 00:00 ET.

    Each input timestamp must be UTC (validated by :func:`to_et`); the
    returned mapping uses the original timestamps as keys so callers can join
    back to source rows without an additional conversion table.
    """
    return {timestamp: _anchor_in_et(to_et(timestamp), window) for timestamp in timestamps}


# ---------------------------------------------------------------------------
# ATR normalization and Wilder ATR
# ---------------------------------------------------------------------------


def atr_normalize(price_move: float, atr: float) -> float:
    """Express a price move as a multiple of ATR.

    Returns ``price_move / atr`` with the sign of ``price_move`` preserved;
    the caller decides whether to take the absolute value. Raises
    :class:`ValueError` when ``atr <= 0`` because a non-positive ATR is a
    data error per
    ``docs/design/01-data-layer/external/quantitative.md`` § 1f, not a
    runtime fallback.
    """
    if atr <= 0:
        raise ValueError(f"atr_normalize requires a positive atr; got {atr!r}")
    return price_move / atr


def _true_range_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
) -> list[float]:
    """Return one True Range value per bar after the first.

    True Range per bar is ``max(high - low, |high - prev_close|, |low - prev_close|)``
    per ``docs/design/01-data-layer/external/quantitative.md`` § 1f. The
    sequence is one shorter than the input because the first bar has no
    previous close.
    """
    ranges: list[float] = []
    for index in range(1, len(closes)):
        high = highs[index]
        low = lows[index]
        prev_close = closes[index - 1]
        ranges.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )
    return ranges


def compute_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float:
    """Wilder's smoothed Average True Range over ``period`` bars.

    Wilder smoothing is ``ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period``
    seeded with the simple mean of the first ``period`` True Range values —
    the conventional definition referenced by
    ``docs/design/01-data-layer/external/quantitative.md`` § 1c. Simple
    moving average of TR is *not* used per the story-06 spec.

    ``period`` defaults to the conventional 14 — the same value used by every
    distillation reference (`quantitative.md` § 1c, § 1f). The argument is
    parameterized so callers can override for sensitivity tests.

    Raises :class:`ValueError` when the input sequences are misaligned or
    shorter than ``period + 1`` bars (need ``period`` True Range values plus
    one previous close). Returns the most recent smoothed ATR.
    """
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise ValueError(
            f"compute_atr requires equal-length sequences; "
            f"got highs={len(highs)} lows={len(lows)} closes={len(closes)}"
        )
    if len(closes) < period + 1:
        raise ValueError(
            f"compute_atr requires at least period+1 bars; got {len(closes)} for period={period}"
        )
    true_ranges = _true_range_series(highs, lows, closes)
    seed = sum(true_ranges[:period]) / period
    atr = seed
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------------------------------------------------------------------------
# Extended-hours confidence weight
# ---------------------------------------------------------------------------


_SESSION_WEIGHTS: dict[Session, float] = {
    "regular": 1.0,
    "pre_market": EXTENDED_HOURS_WEIGHT,
    "after_hours": EXTENDED_HOURS_WEIGHT,
    "overnight": EXTENDED_HOURS_WEIGHT,
}
"""Session → confidence weight lookup.

The four entries cover every ``ohlcv_bars.session`` value; an unknown string
indicates a caller bug (the column is CHECK-constrained at the storage
layer) and surfaces as :class:`ValueError` from
:func:`extended_hours_confidence_weight`.
"""


def extended_hours_confidence_weight(session: Session) -> float:
    """Confidence multiplier for a session's price/flow metrics.

    Returns ``1.0`` for the regular session and :data:`EXTENDED_HOURS_WEIGHT`
    for ``pre_market`` / ``after_hours`` / ``overnight`` per
    ``docs/design/02-distillation-layer/external.md`` § 1 Normalization and
    formatting. The weight is "a blanket discount on thin-liquidity moves,
    not a per-metric judgment"; per-ticker confirmation rates from
    story 08a override at the consumer, not here.

    Raises :class:`ValueError` for any session string not in the documented
    four.
    """
    try:
        return _SESSION_WEIGHTS[session]
    except KeyError as exc:
        raise ValueError(
            f"extended_hours_confidence_weight: unknown session {session!r}; "
            f"expected one of {sorted(_SESSION_WEIGHTS)}"
        ) from exc


# ---------------------------------------------------------------------------
# Macro surprise framing
# ---------------------------------------------------------------------------


def macro_surprise(actual: float, consensus: float) -> float:
    """Signed deviation of ``actual`` from ``consensus`` in native units.

    Per ``docs/design/01-data-layer/external/quantitative.md`` § 6 the
    framing primitive is the signed deviation; the caller applies directional
    interpretation (positive CPI surprise is hawkish, positive jobless-claims
    surprise is dovish for stocks).
    """
    return actual - consensus


def percentile_rank(history: Sequence[float], value: float) -> float | None:
    """Percentile of ``value`` against ``history`` (``<=`` convention) in 0..100.

    Returns ``None`` when the rank is undefined:

    - ``history`` is empty.
    - ``history`` has zero variance (every observation identical) — the rank
      reduces to a tautology, not a position. Returning a sentinel rather
      than 100 prevents the ALP-545 pattern where a bootstrap-period
      component reads at 0 against an all-zero trailing series and reports
      a spurious 100th percentile.

    Callers decide how to surface the ``None`` — typically by propagating
    it into their published payload (rather than substituting 0 / 100).
    """
    if not history:
        return None
    first = history[0]
    if all(x == first for x in history):
        return None
    le = sum(1 for x in history if x <= value)
    return float(le) / float(len(history)) * 100.0


def macro_surprise_zscore(
    surprise: float,
    trailing_surprises: Sequence[float],
) -> float:
    """Z-score of ``surprise`` against a trailing distribution.

    The trailing window is typically the last ~24 months of releases for the
    same indicator (the
    ``threshold-calibration.md`` § Class A ``macro_surprise_percentile``
    cut consumes the z-score downstream). Population standard deviation is
    used because the window is a fixed historical sample of all observed
    releases, not a sample from a larger population.

    Raises :class:`ValueError` when ``trailing_surprises`` is empty — a
    z-score against an empty distribution is undefined and the caller is
    expected to skip the normalization rather than receive a sentinel.
    """
    if not trailing_surprises:
        raise ValueError("macro_surprise_zscore requires a non-empty trailing_surprises window")
    mean = statistics.fmean(trailing_surprises)
    population_stdev = statistics.pstdev(trailing_surprises)
    if population_stdev == 0:
        raise ValueError(
            "macro_surprise_zscore: trailing_surprises has zero variance; z-score undefined"
        )
    return (surprise - mean) / population_stdev


__all__ = [
    "EXTENDED_HOURS_WEIGHT",
    "Session",
    "Window",
    "align_to_window",
    "atr_normalize",
    "compute_atr",
    "extended_hours_confidence_weight",
    "macro_surprise",
    "macro_surprise_zscore",
    "percentile_rank",
    "to_et",
]
