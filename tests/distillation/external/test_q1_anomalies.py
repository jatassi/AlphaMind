"""Tests for the Q1 price/volume anomaly detections — story 08a.

Covers boundary semantics for ``volume_anomaly`` (sigma threshold) and
``price_move_anomaly`` (ATR-multiple threshold). Severity downgrade
under non-calibrated state lives at the publishing-layer cap
(:mod:`alphamind.distillation._severity_cap`, tested in
``test_severity_cap.py``); the producer here uniformly emits
``investigate_now`` per ALP-544.
"""

from __future__ import annotations

import pytest

from alphamind.distillation.q1.anomalies import (
    detect_price_move_anomaly,
    detect_volume_anomaly,
)


class TestDetectVolumeAnomaly:
    def test_fires_at_exactly_the_threshold(self) -> None:
        """At exactly volume_anomaly_sigma stdev, the flag fires (inclusive boundary)."""
        baseline_mean = 1_000_000.0
        baseline_stdev = 100_000.0
        threshold_sigma = 2.5
        # today_volume = mean + sigma * stdev → exactly at the boundary.
        today_volume = baseline_mean + threshold_sigma * baseline_stdev
        flag = detect_volume_anomaly(
            today_volume=today_volume,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
            sigma_threshold=threshold_sigma,
        )
        assert flag is not None
        assert flag.name == "volume_anomaly"
        assert flag.magnitude == pytest.approx(threshold_sigma)
        assert flag.severity == "investigate_now"

    def test_does_not_fire_just_below_threshold(self) -> None:
        baseline_mean = 1_000_000.0
        baseline_stdev = 100_000.0
        threshold_sigma = 2.5
        # today_volume one cent below the threshold → silent.
        today_volume = baseline_mean + (threshold_sigma - 0.01) * baseline_stdev
        flag = detect_volume_anomaly(
            today_volume=today_volume,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
            sigma_threshold=threshold_sigma,
        )
        assert flag is None

    def test_producer_emits_investigate_now_uniformly(self) -> None:
        """ALP-544 — producer no longer caps severity by calibration state.

        The producer emits ``investigate_now`` for every fired threshold;
        the publishing-layer cap (tested separately) downgrades based on
        the surrounding block's calibration state.
        """
        baseline_mean = 1_000_000.0
        baseline_stdev = 100_000.0
        threshold_sigma = 2.5
        today_volume = baseline_mean + 3.0 * baseline_stdev  # well above threshold
        flag = detect_volume_anomaly(
            today_volume=today_volume,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
            sigma_threshold=threshold_sigma,
        )
        assert flag is not None
        assert flag.severity == "investigate_now"

    def test_zero_baseline_stdev_does_not_fire(self) -> None:
        """Degenerate baseline cannot produce an anomaly — silent."""
        flag = detect_volume_anomaly(
            today_volume=2_000_000.0,
            baseline_mean=1_000_000.0,
            baseline_stdev=0.0,
            sigma_threshold=2.5,
        )
        assert flag is None


class TestDetectPriceMoveAnomaly:
    def test_fires_at_exactly_atr_multiple_threshold(self) -> None:
        atr = 2.0
        atr_threshold = 1.5
        price_move = atr_threshold * atr  # exactly 1.5 * ATR
        flag = detect_price_move_anomaly(
            price_move=price_move,
            atr=atr,
            atr_multiple_threshold=atr_threshold,
        )
        assert flag is not None
        assert flag.name == "price_move_anomaly"
        assert flag.magnitude == pytest.approx(atr_threshold)
        assert flag.severity == "investigate_now"

    def test_does_not_fire_just_below_threshold(self) -> None:
        atr = 2.0
        atr_threshold = 1.5
        price_move = (atr_threshold - 0.01) * atr
        flag = detect_price_move_anomaly(
            price_move=price_move,
            atr=atr,
            atr_multiple_threshold=atr_threshold,
        )
        assert flag is None

    def test_negative_move_clears_threshold_by_magnitude(self) -> None:
        """A 1.5*ATR move down is the same anomaly as a 1.5*ATR move up."""
        atr = 2.0
        atr_threshold = 1.5
        price_move = -atr_threshold * atr
        flag = detect_price_move_anomaly(
            price_move=price_move,
            atr=atr,
            atr_multiple_threshold=atr_threshold,
        )
        assert flag is not None
        assert flag.magnitude == pytest.approx(atr_threshold)

    def test_producer_emits_investigate_now_uniformly(self) -> None:
        """ALP-544 — producer no longer caps severity by calibration state."""
        atr = 2.0
        atr_threshold = 1.5
        price_move = 2.0 * atr
        flag = detect_price_move_anomaly(
            price_move=price_move,
            atr=atr,
            atr_multiple_threshold=atr_threshold,
        )
        assert flag is not None
        assert flag.severity == "investigate_now"

    def test_zero_atr_does_not_fire(self) -> None:
        """Non-positive ATR collapses the threshold — silent."""
        flag = detect_price_move_anomaly(
            price_move=10.0,
            atr=0.0,
            atr_multiple_threshold=1.5,
        )
        assert flag is None
