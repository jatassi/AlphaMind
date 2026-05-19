"""Pure-compute tests for q3 (ALP-484) — no SQLite, no Session.

ALP-484 propagated the compute/load boundary split (piloted in ALP-467) to
the remaining q3 sub-modules: anomalies, atm_iv_baseline, and
etf_iv_divergence. Each compute core is testable from hand-built frozen
inputs with no ORM or in-memory database. The tests in this module
construct inputs directly and assert on the pure return shape.
"""

from __future__ import annotations

import pytest

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.q3.anomalies_compute import (
    LowOiSnapshotPair,
    LowOiVolumeAnomalyInputs,
    SectorSweepInputs,
    compute_low_oi_volume_anomalies,
    compute_sector_wide_sweeps,
)
from alphamind.distillation.q3.atm_iv_baseline_compute import (
    compute_atm_iv_baseline,
    compute_atm_iv_baselines,
)
from alphamind.distillation.q3.etf_iv_divergence_compute import (
    compute_etf_iv_divergences,
)
from alphamind.distillation.q3.pair_trade import FlowZScore

# ---------------------------------------------------------------------------
# anomalies_compute — low-OI volume anomaly
# ---------------------------------------------------------------------------


def test_low_oi_volume_fires_when_multiple_clears_threshold() -> None:
    """A snapshot with 5x trailing-avg volume + sub-floor OI fires."""
    pair = LowOiSnapshotPair(
        contract_ticker="AAPL240419C150",
        underlying_ticker="AAPL",
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=500,
        open_interest=10,
        prior_volumes=(50, 100, 150),  # avg 100; today 500 → multiple 5x
    )
    inputs = LowOiVolumeAnomalyInputs(snapshots=(pair,))
    anomalies = compute_low_oi_volume_anomalies(
        inputs,
        volume_multiple_threshold=5.0,
        oi_threshold=100,
    )
    assert len(anomalies) == 1
    assert anomalies[0].contract_ticker == "AAPL240419C150"
    assert anomalies[0].volume_multiple == 5.0
    assert anomalies[0].underlying_ticker == "AAPL"


def test_low_oi_volume_skipped_when_oi_at_or_above_floor() -> None:
    """OI at the floor is not below the floor — no anomaly."""
    pair = LowOiSnapshotPair(
        contract_ticker="X",
        underlying_ticker="X",
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=500,
        open_interest=100,  # equal to threshold; gate is strict <
        prior_volumes=(50, 100, 150),
    )
    inputs = LowOiVolumeAnomalyInputs(snapshots=(pair,))
    assert (
        compute_low_oi_volume_anomalies(
            inputs,
            volume_multiple_threshold=5.0,
            oi_threshold=100,
        )
        == []
    )


def test_low_oi_volume_skipped_when_no_prior_history() -> None:
    """No prior history means the multiple is undefined; skip."""
    pair = LowOiSnapshotPair(
        contract_ticker="X",
        underlying_ticker="X",
        snapshot_ts="2026-04-25T00:00:00Z",
        volume_today=500,
        open_interest=10,
        prior_volumes=(),
    )
    inputs = LowOiVolumeAnomalyInputs(snapshots=(pair,))
    assert (
        compute_low_oi_volume_anomalies(
            inputs,
            volume_multiple_threshold=5.0,
            oi_threshold=100,
        )
        == []
    )


# ---------------------------------------------------------------------------
# anomalies_compute — sector-wide sweep
# ---------------------------------------------------------------------------


def test_sector_wide_sweep_fires_with_three_call_names() -> None:
    """Three tech tickers above the call threshold fire a sweep."""
    inputs = SectorSweepInputs(
        sector_membership={
            "AAPL": "tech",
            "MSFT": "tech",
            "GOOG": "tech",
            "XOM": "energy",
        },
        flow_zscores={
            "AAPL": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
            "MSFT": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
            "GOOG": FlowZScore(call_bto_z=1.8, put_bto_z=0.0),
            "XOM": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),  # only one in energy
        },
    )
    sweeps = compute_sector_wide_sweeps(inputs, sigma_threshold=1.5, min_names=3)
    assert len(sweeps) == 1
    assert sweeps[0].sector == "tech"
    assert sweeps[0].direction == "call"
    assert sweeps[0].tickers == ("AAPL", "GOOG", "MSFT")


def test_sector_wide_sweep_skipped_below_min_names() -> None:
    """Two names is not three; no sweep."""
    inputs = SectorSweepInputs(
        sector_membership={"AAPL": "tech", "MSFT": "tech"},
        flow_zscores={
            "AAPL": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
            "MSFT": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
        },
    )
    assert compute_sector_wide_sweeps(inputs, sigma_threshold=1.5, min_names=3) == []


def test_sector_wide_sweep_skips_unmapped_tickers() -> None:
    """Tickers absent from sector_membership are silently skipped."""
    inputs = SectorSweepInputs(
        sector_membership={"AAPL": "tech"},  # MSFT/GOOG missing
        flow_zscores={
            "AAPL": FlowZScore(call_bto_z=2.0, put_bto_z=0.0),
            "MSFT": FlowZScore(call_bto_z=2.5, put_bto_z=0.0),
            "GOOG": FlowZScore(call_bto_z=1.8, put_bto_z=0.0),
        },
    )
    assert compute_sector_wide_sweeps(inputs, sigma_threshold=1.5, min_names=3) == []


# ---------------------------------------------------------------------------
# atm_iv_baseline_compute
# ---------------------------------------------------------------------------


def test_atm_iv_baseline_calibrated_with_long_history() -> None:
    """A history above the min-observations threshold is CALIBRATED."""
    history = [0.20 + 0.001 * i for i in range(60)]  # 60 increasing observations
    result = compute_atm_iv_baseline(history, window_days=252, min_observations=60)
    assert result.rank.state is CalibrationState.CALIBRATED
    assert result.upsert is not None
    assert result.upsert.n_observations == 60
    assert result.upsert.window_days == 252
    # Latest IV is the largest — percentile should be 100%.
    assert result.rank.value is not None
    assert result.rank.value["iv_rank_percentile"] == 100.0


def test_atm_iv_baseline_bootstrap_below_min_observations() -> None:
    """Below-threshold n still produces a BOOTSTRAP rank with mean/stdev."""
    history = [0.20, 0.21, 0.22]
    result = compute_atm_iv_baseline(history, window_days=252, min_observations=60)
    assert result.rank.state is CalibrationState.ACCUMULATING
    assert result.upsert is not None  # writes happen even in BOOTSTRAP
    assert result.upsert.state is CalibrationState.ACCUMULATING
    assert result.rank.bootstrap_reason == "atm_iv_min_observations: 3 < 60"


def test_atm_iv_baseline_unavailable_with_empty_history() -> None:
    """Empty history is UNAVAILABLE and produces no upsert payload."""
    result = compute_atm_iv_baseline([], window_days=252, min_observations=60)
    assert result.rank.state is CalibrationState.UNAVAILABLE
    assert result.rank.value is None
    assert result.upsert is None


def test_atm_iv_baseline_zero_variance_history_yields_null_iv_rank_percentile() -> None:
    """A flat history can't rank the latest IV — surface ``None`` percentile."""
    history = [0.20] * 60  # identical observations across the window
    result = compute_atm_iv_baseline(history, window_days=252, min_observations=60)
    assert result.rank.state is CalibrationState.CALIBRATED
    assert result.rank.value is not None
    assert result.rank.value["iv_rank_percentile"] is None


def test_atm_iv_baselines_per_ticker_pure_dispatch() -> None:
    """The per-ticker dispatcher fans out the per-ticker pure compute."""
    history_by_ticker = {
        "AAPL": [0.20 + 0.001 * i for i in range(60)],
        "MSFT": [0.30, 0.31],  # too few
        "GOOG": [],  # empty
    }
    results = compute_atm_iv_baselines(history_by_ticker, window_days=252, min_observations=60)
    assert results["AAPL"].rank.state is CalibrationState.CALIBRATED
    assert results["MSFT"].rank.state is CalibrationState.ACCUMULATING
    assert results["GOOG"].rank.state is CalibrationState.UNAVAILABLE
    assert results["AAPL"].upsert is not None
    assert results["GOOG"].upsert is None


# ---------------------------------------------------------------------------
# etf_iv_divergence_compute
# ---------------------------------------------------------------------------


def test_etf_iv_divergence_etf_leading_when_spread_widens() -> None:
    """A widening spread (+z above threshold) tags ``etf_leading_names``."""
    sectors: dict[str, dict[str, float | str]] = {
        "tech": {
            "etf_ticker": "XLK",
            "etf_iv": 0.30,
            "single_name_aggregate_iv": 0.20,  # spread = 0.10
            "spread_baseline_mean": 0.0,
            "spread_baseline_stdev": 0.05,  # z = 2.0
        }
    }
    divergences = compute_etf_iv_divergences(sectors=sectors, sigma_threshold=1.0)
    assert len(divergences) == 1
    assert divergences[0].sector == "tech"
    assert divergences[0].direction == "etf_leading_names"
    assert divergences[0].spread_zscore == pytest.approx(2.0)


def test_etf_iv_divergence_names_leading_when_spread_narrows() -> None:
    """A narrowing spread (-z) tags ``names_leading_etf``."""
    sectors: dict[str, dict[str, float | str]] = {
        "energy": {
            "etf_ticker": "XLE",
            "etf_iv": 0.20,
            "single_name_aggregate_iv": 0.30,  # spread = -0.10
            "spread_baseline_mean": 0.0,
            "spread_baseline_stdev": 0.05,  # z = -2.0
        }
    }
    divergences = compute_etf_iv_divergences(sectors=sectors, sigma_threshold=1.0)
    assert len(divergences) == 1
    assert divergences[0].direction == "names_leading_etf"


def test_etf_iv_divergence_skipped_when_z_below_threshold() -> None:
    """abs(z) < sigma_threshold means no divergence fires."""
    sectors: dict[str, dict[str, float | str]] = {
        "tech": {
            "etf_ticker": "XLK",
            "etf_iv": 0.21,
            "single_name_aggregate_iv": 0.20,  # spread = 0.01
            "spread_baseline_mean": 0.0,
            "spread_baseline_stdev": 0.05,  # z = 0.2
        }
    }
    assert compute_etf_iv_divergences(sectors=sectors, sigma_threshold=1.0) == []


def test_etf_iv_divergence_skipped_when_baseline_stdev_zero() -> None:
    """Non-positive baseline stdev means z is undefined — skip."""
    sectors: dict[str, dict[str, float | str]] = {
        "tech": {
            "etf_ticker": "XLK",
            "etf_iv": 0.30,
            "single_name_aggregate_iv": 0.20,
            "spread_baseline_mean": 0.0,
            "spread_baseline_stdev": 0.0,  # invalid
        }
    }
    assert compute_etf_iv_divergences(sectors=sectors, sigma_threshold=1.0) == []
