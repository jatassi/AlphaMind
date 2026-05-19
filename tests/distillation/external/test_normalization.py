"""Tests for the deterministic normalization primitives — story 02-distillation/06.

Cover the seven primitives in ``alphamind.distillation.normalization`` plus the
named-constant unit conventions in ``alphamind.distillation.units``. Each test
exercises observable behavior through the public interface; no implementation
details (zoneinfo internals, smoothing-loop internals) are inspected.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta, timezone

import pytest

from alphamind.distillation.normalization import (
    EXTENDED_HOURS_WEIGHT,
    align_to_window,
    atr_normalize,
    compute_atr,
    extended_hours_confidence_weight,
    macro_surprise,
    macro_surprise_zscore,
    percentile_rank,
    to_et,
)

# ---------------------------------------------------------------------------
# to_et: UTC → ET conversion with DST handling
# ---------------------------------------------------------------------------


def test_to_et_converts_summer_utc_to_edt() -> None:
    """A summer UTC moment converts to EDT (UTC-4)."""
    # 2026-07-15 17:30 UTC is 13:30 EDT (DST in effect).
    summer_utc = datetime(2026, 7, 15, 17, 30, tzinfo=UTC)
    converted = to_et(summer_utc)
    assert converted.year == 2026
    assert converted.month == 7
    assert converted.day == 15
    assert converted.hour == 13
    assert converted.minute == 30
    # The wall-clock instant must be preserved.
    assert converted.utcoffset() == timedelta(hours=-4)


def test_to_et_converts_winter_utc_to_est() -> None:
    """A winter UTC moment converts to EST (UTC-5)."""
    # 2026-01-15 17:30 UTC is 12:30 EST (no DST).
    winter_utc = datetime(2026, 1, 15, 17, 30, tzinfo=UTC)
    converted = to_et(winter_utc)
    assert converted.year == 2026
    assert converted.month == 1
    assert converted.day == 15
    assert converted.hour == 12
    assert converted.minute == 30
    assert converted.utcoffset() == timedelta(hours=-5)


def test_to_et_rejects_naive_datetime() -> None:
    """A naive datetime carries no timezone — raises rather than guessing."""
    # The test deliberately constructs a naive datetime to exercise rejection;
    # DTZ001 is the rule that prevents naive datetimes in production code.
    naive = datetime(2026, 4, 27, 14, 30)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        to_et(naive)


def test_to_et_rejects_non_utc_timezone() -> None:
    """An aware datetime with a non-UTC offset is a contract violation."""
    other_tz = datetime(2026, 4, 27, 14, 30, tzinfo=timezone(timedelta(hours=5)))
    with pytest.raises(ValueError, match="UTC"):
        to_et(other_tz)


def test_to_et_dst_regression_pins_summer_and_winter() -> None:
    """Pin one summer date and one winter date so DST transitions cannot drift silently.

    The same UTC wall-clock instant produces different ET wall-clock instants
    across DST states; the two cases together prove the conversion respects
    the calendar.
    """
    summer_utc = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    winter_utc = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer_et = to_et(summer_utc)
    winter_et = to_et(winter_utc)
    # Summer: UTC-4 → 12:00 UTC = 08:00 EDT
    assert summer_et.hour == 8
    # Winter: UTC-5 → 12:00 UTC = 07:00 EST
    assert winter_et.hour == 7
    # And the offsets must differ — that's the DST transition test.
    assert summer_et.utcoffset() != winter_et.utcoffset()


# ---------------------------------------------------------------------------
# align_to_window: anchor each timestamp to its ET-boundary window start
# ---------------------------------------------------------------------------


def test_align_to_window_15min_anchors_to_quarter_hour() -> None:
    """A 15-minute window anchors to the nearest preceding :00/:15/:30/:45 ET."""
    # 2026-04-27 14:37 UTC → 10:37 EDT → window start 10:30 EDT.
    ts = datetime(2026, 4, 27, 14, 37, tzinfo=UTC)
    mapping = align_to_window([ts], "15min")
    anchor = mapping[ts]
    et = to_et(ts)
    assert anchor.hour == 10
    assert anchor.minute == 30
    assert anchor.second == 0
    # Anchor is in ET.
    assert anchor.utcoffset() == et.utcoffset()


def test_align_to_window_1h_anchors_to_hour_boundary() -> None:
    """A 1-hour window anchors to the preceding ET hour boundary."""
    # 2026-04-27 14:37 UTC → 10:37 EDT → window start 10:00 EDT.
    ts = datetime(2026, 4, 27, 14, 37, tzinfo=UTC)
    anchor = align_to_window([ts], "1h")[ts]
    assert anchor.hour == 10
    assert anchor.minute == 0


def test_align_to_window_4h_anchors_to_four_hour_block() -> None:
    """A 4-hour window anchors to a fixed-offset 4-hour ET grid starting at 00:00 ET."""
    # 2026-04-27 14:37 UTC → 10:37 EDT → window start 08:00 EDT (8 = 2 * 4).
    ts = datetime(2026, 4, 27, 14, 37, tzinfo=UTC)
    anchor = align_to_window([ts], "4h")[ts]
    assert anchor.hour == 8
    assert anchor.minute == 0


def test_align_to_window_1d_anchors_to_midnight_et() -> None:
    """A 1-day window anchors to 00:00 ET on the same ET calendar day."""
    # 2026-04-27 14:37 UTC → 10:37 EDT → day start 2026-04-27 00:00 EDT.
    ts = datetime(2026, 4, 27, 14, 37, tzinfo=UTC)
    anchor = align_to_window([ts], "1d")[ts]
    assert anchor.year == 2026
    assert anchor.month == 4
    assert anchor.day == 27
    assert anchor.hour == 0
    assert anchor.minute == 0


def test_align_to_window_1w_anchors_to_monday_midnight_et() -> None:
    """A 1-week window anchors to Monday 00:00 ET of the containing week.

    2026-04-27 is a Monday; 2026-04-29 (Wednesday) lives in the same week
    starting Monday 2026-04-27 00:00 ET.
    """
    ts = datetime(2026, 4, 29, 14, 37, tzinfo=UTC)
    anchor = align_to_window([ts], "1w")[ts]
    assert anchor.year == 2026
    assert anchor.month == 4
    assert anchor.day == 27
    assert anchor.hour == 0
    assert anchor.minute == 0
    # Confirm Monday (weekday() == 0).
    assert anchor.weekday() == 0


def test_align_to_window_handles_multiple_timestamps() -> None:
    """The mapping returns one entry per input timestamp."""
    ts_a = datetime(2026, 4, 27, 14, 37, tzinfo=UTC)
    ts_b = datetime(2026, 4, 27, 14, 49, tzinfo=UTC)
    mapping = align_to_window([ts_a, ts_b], "15min")
    assert mapping[ts_a].minute == 30
    assert mapping[ts_b].minute == 45


# ---------------------------------------------------------------------------
# atr_normalize: price move expressed as multiples of ATR
# ---------------------------------------------------------------------------


def test_atr_normalize_returns_move_divided_by_atr() -> None:
    """``atr_normalize(move, atr)`` returns ``move / atr``."""
    assert atr_normalize(price_move=2.5, atr=1.25) == pytest.approx(2.0)


def test_atr_normalize_preserves_sign_for_signed_move() -> None:
    """A signed move passes its sign through — caller decides abs vs. signed."""
    assert atr_normalize(price_move=-3.0, atr=1.5) == pytest.approx(-2.0)


def test_atr_normalize_rejects_zero_atr() -> None:
    """A zero ATR is a data error, not a runtime fallback."""
    with pytest.raises(ValueError, match="atr"):
        atr_normalize(price_move=1.0, atr=0.0)


def test_atr_normalize_rejects_negative_atr() -> None:
    """A negative ATR is a data error — ATR is by definition non-negative."""
    with pytest.raises(ValueError, match="atr"):
        atr_normalize(price_move=1.0, atr=-0.5)


# ---------------------------------------------------------------------------
# compute_atr: Wilder's smoothed True Range
# ---------------------------------------------------------------------------


# Canonical 20-bar fixture exercising both gap-based True Range and
# intraday-range True Range. Includes an overnight gap up at bar 5 and a gap
# down at bar 15 so the True Range computation is non-trivial. Bars are
# (high, low, close).
_CANONICAL_BARS: list[tuple[float, float, float]] = [
    (10.50, 10.00, 10.20),
    (10.60, 10.10, 10.40),
    (10.80, 10.30, 10.50),
    (10.70, 10.20, 10.30),
    (10.50, 10.00, 10.10),
    (11.50, 10.80, 11.20),  # gap up
    (11.80, 11.10, 11.60),
    (12.00, 11.40, 11.80),
    (11.90, 11.30, 11.50),
    (11.70, 11.10, 11.30),
    (11.60, 11.00, 11.20),
    (11.50, 10.90, 11.10),
    (11.40, 10.80, 11.00),
    (11.30, 10.70, 10.90),
    (11.20, 10.60, 10.80),
    (10.20, 9.50, 9.80),  # gap down
    (10.00, 9.40, 9.70),
    (9.90, 9.30, 9.60),
    (9.80, 9.20, 9.50),
    (9.70, 9.10, 9.40),
]


def _true_ranges(bars: list[tuple[float, float, float]]) -> list[float]:
    """Independent True Range series — one entry per bar after the first.

    Local reference implementation; kept separate from the production helper
    so the test cross-checks the production code rather than re-using it.
    """
    ranges: list[float] = []
    for index in range(1, len(bars)):
        high, low, _ = bars[index]
        prev_close = bars[index - 1][2]
        ranges.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )
    return ranges


def _wilder_reference_atr(
    bars: list[tuple[float, float, float]],
    period: int,
) -> float:
    """Independent reference implementation of Wilder ATR for cross-check.

    Uses the conventional Wilder seed (simple mean of the first ``period``
    True Range values) followed by Wilder smoothing
    ``ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period``.
    """
    true_ranges = _true_ranges(bars)
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def test_compute_atr_matches_wilder_reference_on_canonical_fixture() -> None:
    """Canonical 20-bar fixture: result matches the independent Wilder reference."""
    highs = [bar[0] for bar in _CANONICAL_BARS]
    lows = [bar[1] for bar in _CANONICAL_BARS]
    closes = [bar[2] for bar in _CANONICAL_BARS]
    expected = _wilder_reference_atr(_CANONICAL_BARS, period=14)
    actual = compute_atr(highs, lows, closes, period=14)
    assert actual == pytest.approx(expected)


def test_compute_atr_uses_wilder_smoothing_not_simple_moving_average() -> None:
    """Wilder smoothing produces a different value than a simple moving average.

    The True Range series is identical for both methods; only the smoothing
    differs. A simple moving average of the last ``period`` TR values would
    drop the earliest TR values and weight the rest equally; Wilder retains
    influence from earlier TRs via geometric decay. The fixture is constructed
    with non-uniform True Range so the two methods produce different numbers.
    """
    highs = [bar[0] for bar in _CANONICAL_BARS]
    lows = [bar[1] for bar in _CANONICAL_BARS]
    closes = [bar[2] for bar in _CANONICAL_BARS]
    period = 14
    sma_atr = sum(_true_ranges(_CANONICAL_BARS)[-period:]) / period
    wilder_atr = compute_atr(highs, lows, closes, period=period)
    assert wilder_atr != pytest.approx(sma_atr)


def test_compute_atr_raises_when_inputs_too_short() -> None:
    """Wilder ATR needs at least ``period + 1`` bars to compute one TR per period."""
    period = 14
    highs = [10.0] * period
    lows = [9.5] * period
    closes = [9.8] * period
    with pytest.raises(ValueError, match="period"):
        compute_atr(highs, lows, closes, period=period)


def test_compute_atr_raises_when_sequences_misaligned() -> None:
    """Highs/lows/closes must be the same length — misalignment is a caller bug."""
    highs = [10.0, 10.5, 11.0]
    lows = [9.5, 9.8]  # one short
    closes = [9.8, 10.2, 10.6]
    with pytest.raises(ValueError, match="length"):
        compute_atr(highs, lows, closes, period=2)


# ---------------------------------------------------------------------------
# extended_hours_confidence_weight: blanket-discount tag
# ---------------------------------------------------------------------------


def test_extended_hours_confidence_weight_regular_session_returns_one() -> None:
    """The regular session carries no discount — weight is 1.0."""
    assert extended_hours_confidence_weight("regular") == 1.0


def test_extended_hours_confidence_weight_pre_market_returns_extended_constant() -> None:
    """Pre-market carries the extended-hours blanket discount."""
    assert extended_hours_confidence_weight("pre_market") == EXTENDED_HOURS_WEIGHT


def test_extended_hours_confidence_weight_after_hours_returns_extended_constant() -> None:
    """After-hours carries the extended-hours blanket discount."""
    assert extended_hours_confidence_weight("after_hours") == EXTENDED_HOURS_WEIGHT


def test_extended_hours_confidence_weight_overnight_returns_extended_constant() -> None:
    """Overnight carries the extended-hours blanket discount."""
    assert extended_hours_confidence_weight("overnight") == EXTENDED_HOURS_WEIGHT


def test_extended_hours_weight_constant_is_named_not_a_magic_number() -> None:
    """The blanket-discount weight is a named module-level constant.

    Per the story spec, the 0.5 weight must be a named constant
    (``EXTENDED_HOURS_WEIGHT``) referenced from the function. The lookup
    constant is the source of truth; the test asserts the constant exists
    as a float in the documented range so any future change is visible at
    review.
    """
    assert isinstance(EXTENDED_HOURS_WEIGHT, float)
    # The documented blanket discount sits strictly between zero and one;
    # zero would be "ignore", one would be "full credence", and the discount
    # is by definition between the two.
    assert 0.0 < EXTENDED_HOURS_WEIGHT < 1.0


def test_extended_hours_confidence_weight_rejects_unknown_session() -> None:
    """An unknown session string is a caller bug — raise rather than silently default."""
    with pytest.raises(ValueError, match="session"):
        extended_hours_confidence_weight("lunch_break")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# macro_surprise: signed deviation in native units
# ---------------------------------------------------------------------------


def test_macro_surprise_returns_signed_deviation() -> None:
    """Surprise is ``actual - consensus`` in native units, sign preserved."""
    # CPI: actual 3.2% YoY, consensus 3.0% YoY → +0.2 surprise (hawkish).
    assert macro_surprise(actual=3.2, consensus=3.0) == pytest.approx(0.2)


def test_macro_surprise_returns_negative_when_actual_below_consensus() -> None:
    """A miss to the downside is negative surprise — caller maps direction."""
    # Jobless claims: actual 220k, consensus 240k → -20 surprise (dovish for stocks).
    assert macro_surprise(actual=220.0, consensus=240.0) == pytest.approx(-20.0)


def test_macro_surprise_returns_zero_on_inline_print() -> None:
    """Actual == consensus → zero surprise."""
    assert macro_surprise(actual=2.0, consensus=2.0) == 0.0


# ---------------------------------------------------------------------------
# macro_surprise_zscore: z-score against trailing distribution
# ---------------------------------------------------------------------------


def test_macro_surprise_zscore_against_trailing_window() -> None:
    """A surprise's z-score equals (surprise - mean) / population_stdev of the trailing window.

    Population standard deviation is the natural choice for a fixed historical
    sample; the implementation pins this convention so the assertion can
    compute the expected value directly rather than assert a hardcoded float.
    """
    trailing = [-2.0, -1.0, 0.0, 1.0, 2.0]
    z = macro_surprise_zscore(surprise=2.0, trailing_surprises=trailing)
    expected = (2.0 - statistics.fmean(trailing)) / statistics.pstdev(trailing)
    assert z == pytest.approx(expected)


def test_macro_surprise_zscore_rejects_empty_trailing_window() -> None:
    """An empty trailing window cannot produce a z-score — raise instead of dividing by zero."""
    with pytest.raises(ValueError, match="trailing"):
        macro_surprise_zscore(surprise=1.0, trailing_surprises=[])


def test_macro_surprise_zscore_rejects_zero_variance_trailing_window() -> None:
    """Zero-variance trailing window: z-score is undefined; raise rather than divide by zero."""
    with pytest.raises(ValueError, match="variance"):
        macro_surprise_zscore(surprise=1.0, trailing_surprises=[0.5, 0.5, 0.5])


# ---------------------------------------------------------------------------
# percentile_rank: <=-convention rank with zero-variance / empty sentinel
# ---------------------------------------------------------------------------


def test_percentile_rank_ranks_value_against_distribution() -> None:
    """Percentile = (count history <= value) / n * 100 on a non-degenerate distribution."""
    assert percentile_rank([1.0, 2.0, 3.0, 4.0, 5.0], 3.0) == 60.0
    assert percentile_rank([1.0, 2.0, 3.0, 4.0, 5.0], 5.0) == 100.0
    assert percentile_rank([1.0, 2.0, 3.0, 4.0, 5.0], 0.0) == 0.0


def test_percentile_rank_empty_history_returns_none() -> None:
    """Empty history: no signal; sentinel None rather than a fabricated 0 / 100."""
    assert percentile_rank([], 5.0) is None


def test_percentile_rank_zero_variance_history_returns_none() -> None:
    """All-identical history (zero variance): rank is undefined; sentinel None."""
    assert percentile_rank([0.0, 0.0, 0.0, 0.0], 0.0) is None
    assert percentile_rank([0.0, 0.0, 0.0, 0.0], 5.0) is None
    assert percentile_rank([0.5, 0.5, 0.5], 0.5) is None
