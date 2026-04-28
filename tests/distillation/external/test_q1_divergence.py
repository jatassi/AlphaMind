"""Tests for the multi-timeframe divergence flag — story 08a.

The divergence flag fires when an indicator at one timeframe disagrees with
the same indicator at the next-higher timeframe. The canonical example is
4h RSI bearish (< 30) while daily RSI is healthy (> 50) — surfaced
prominently for sector researchers per
``docs/design/02-distillation-layer/external.md`` § 2.
"""

from __future__ import annotations

from alphamind.distillation.q1.divergence import (
    DivergenceFlag,
    detect_rsi_divergences,
)
from alphamind.distillation.q1.indicators import RsiResult

# Conventional RSI bands: oversold below 30, overbought above 70, "healthy"
# is the wide neutral region centered on 50. The numeric breakpoints come
# from Wilder's original definition, not from configuration.
RSI_OVERSOLD = 30.0
RSI_HEALTHY = 50.0
RSI_OVERBOUGHT = 70.0


class TestDetectRsiDivergences:
    def test_bearish_4h_vs_healthy_daily_fires_divergence(self) -> None:
        """4h RSI < 30 while daily RSI > 50 produces one divergence flag."""
        per_tf = {
            "15min": RsiResult(value=55.0, period=14),
            "1h": RsiResult(value=52.0, period=14),
            "4h": RsiResult(value=25.0, period=14),
            "1d": RsiResult(value=60.0, period=14),
            "1w": RsiResult(value=58.0, period=14),
        }
        flags = detect_rsi_divergences(per_tf)
        assert any(
            f.lower_timeframe == "4h" and f.higher_timeframe == "1d" and f.kind == "rsi"
            for f in flags
        )

    def test_aligned_timeframes_produce_no_divergence(self) -> None:
        """When every timeframe agrees, no divergence flag fires."""
        per_tf = {
            "15min": RsiResult(value=55.0, period=14),
            "1h": RsiResult(value=56.0, period=14),
            "4h": RsiResult(value=57.0, period=14),
            "1d": RsiResult(value=58.0, period=14),
            "1w": RsiResult(value=58.0, period=14),
        }
        flags = detect_rsi_divergences(per_tf)
        assert flags == ()

    def test_overbought_4h_vs_healthy_daily_also_fires(self) -> None:
        """4h overbought (>70) while daily is healthy is the symmetric divergence."""
        per_tf = {
            "15min": RsiResult(value=55.0, period=14),
            "1h": RsiResult(value=56.0, period=14),
            "4h": RsiResult(value=78.0, period=14),
            "1d": RsiResult(value=55.0, period=14),
            "1w": RsiResult(value=55.0, period=14),
        }
        flags = detect_rsi_divergences(per_tf)
        assert any(f.lower_timeframe == "4h" and f.kind == "rsi" for f in flags)

    def test_each_flag_carries_paired_timeframes_and_indicator_kind(self) -> None:
        """The flag carries the lower/higher timeframe pair so the rollup can list them."""
        per_tf = {
            "15min": RsiResult(value=55.0, period=14),
            "1h": RsiResult(value=20.0, period=14),
            "4h": RsiResult(value=75.0, period=14),
            "1d": RsiResult(value=55.0, period=14),
            "1w": RsiResult(value=55.0, period=14),
        }
        flags = detect_rsi_divergences(per_tf)
        assert all(isinstance(flag, DivergenceFlag) for flag in flags)
        # Two divergences: 1h vs 4h (oversold vs overbought) and 4h vs 1d
        # (overbought vs healthy).
        assert len(flags) >= 2
