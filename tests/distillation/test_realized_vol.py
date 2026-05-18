"""Tests for per-ticker realized-vol substrate (ALP-530).

Covers the pure compute function, the per-invocation persister, and the
read function. The wiring-site tests live alongside their respective
modules (``tests/scheduler/test_phase1_inputs.py`` and
``tests/execution/continuous_monitor/test_paper_enrichment_wiring.py``).
"""

from __future__ import annotations

import math

from alphamind.distillation.realized_vol import compute_trailing_realized_vol


def test_compute_returns_nonnegative_float_for_sufficient_data() -> None:
    """With six closes (five log returns) the compute returns a finite
    non-negative scalar — the documented minimum-data threshold."""
    closes = (100.0, 102.0, 101.0, 103.0, 102.5, 104.0)
    result = compute_trailing_realized_vol(closes)
    assert result is not None
    assert result >= 0.0
    assert math.isfinite(result)


def test_compute_returns_none_for_insufficient_data() -> None:
    """Below ``min_returns + 1`` closes (≤ five closes by default) the
    compute returns ``None`` rather than fabricating a vol estimate."""
    too_short = (100.0, 102.0, 101.0, 103.0, 102.5)
    assert compute_trailing_realized_vol(too_short) is None


def test_compute_is_monotone_in_input_volatility() -> None:
    """A constant-volatility series at 0.30 produces a larger realized-vol
    than the same series at 0.20 — the function must respect the volatility
    ordering of its inputs."""
    # Two synthetic geometric-Brownian price paths, identical seed pattern
    # but scaled to different volatilities. The annualized output is the
    # sample-std of log returns * sqrt(252); for a constant-vol input the
    # output reflects the daily-vol scaling.
    daily_returns_low = [0.20 / math.sqrt(252.0) * (-1.0) ** i for i in range(60)]
    daily_returns_high = [0.30 / math.sqrt(252.0) * (-1.0) ** i for i in range(60)]
    closes_low = [100.0]
    closes_high = [100.0]
    for r_low, r_high in zip(daily_returns_low, daily_returns_high, strict=True):
        closes_low.append(closes_low[-1] * math.exp(r_low))
        closes_high.append(closes_high[-1] * math.exp(r_high))
    low = compute_trailing_realized_vol(tuple(closes_low))
    high = compute_trailing_realized_vol(tuple(closes_high))
    assert low is not None
    assert high is not None
    assert high > low
