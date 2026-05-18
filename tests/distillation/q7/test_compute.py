"""Pure-compute tests for q7 (ALP-486) — no SQLite, no Session.

ALP-486 propagated the compute/load boundary split (piloted in ALP-467) to
q7. Each compute core is testable from hand-built frozen inputs with no
ORM or in-memory database. The tests in this module construct inputs
directly and assert on the pure return shape — proving the q7 compute
path is genuinely pure and safe to call under
``asyncio.TaskGroup`` + ``asyncio.to_thread``.

Coverage:

- breadth_internals_compute — pct-above-EMA / advance-decline / equal-vs-cap.
- cross_sector_rotation_compute — velocity slow/sharp branches, narrative
  rate/growth/risk-appetite branches.
- intermarket_regime_compute — four sub-blocks (spy_tlt / gld_real_yields /
  oil_xle_beta / vix_spy), divergence flag firing.
- intra_sector_correlation_compute — short/long matrices, pair divergence
  flags, suppression when short matches long.
- lead_lag_compute — overdue lag flag, inversion flag, persisted estimate
  passthrough.
- correlation_regime_change_compute — breakdown blocks, dispersion shift,
  narrative-lag flag.
- assemble — pure :func:`assemble_q7_blocks_from_inputs` over a frozen
  :class:`Q7Inputs`.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import LeadLagPair
from alphamind.distillation.q7._loaders import (
    LeadLagInputs,
    Q7Inputs,
)
from alphamind.distillation.q7.assemble import assemble_q7_blocks_from_inputs
from alphamind.distillation.q7.breadth_internals_compute import (
    compute_breadth_internals_pure,
)
from alphamind.distillation.q7.correlation_regime_change_compute import (
    CorrelationRegimeChangeParameters,
    compute_correlation_regime_change_pure,
)
from alphamind.distillation.q7.cross_sector_rotation_compute import (
    ROTATION_NARRATIVE_RATE_DRIVEN,
    ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN,
    VELOCITY_SHARP,
    VELOCITY_SLOW,
    compute_cross_sector_rotation_pure,
)
from alphamind.distillation.q7.intermarket_regime_compute import (
    compute_intermarket_regime_pure,
)
from alphamind.distillation.q7.intra_sector_correlation_compute import (
    compute_intra_sector_correlation_pure,
)
from alphamind.distillation.q7.lead_lag_compute import (
    compute_lead_lag_pure,
)


def _flat_closes(n: int, base: float = 100.0) -> tuple[float, ...]:
    """``n`` identical closes — zero variance, zero returns."""
    return tuple(base for _ in range(n))


def _walked_closes(log_returns: list[float], base: float = 100.0) -> tuple[float, ...]:
    """Cumulative-product closes driven by ``log_returns``."""
    closes = [base]
    for r in log_returns:
        closes.append(closes[-1] * math.exp(r))
    return tuple(closes)


def _as_of() -> datetime:
    return datetime(2026, 5, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# breadth_internals_compute
# ---------------------------------------------------------------------------


class TestBreadthInternalsCompute:
    def test_payload_fields_present_with_three_emas(self) -> None:
        closes_by_ticker = {
            "AAPL": _walked_closes([0.01] * 200),  # uptrend → above EMAs
            "MSFT": _walked_closes([0.005] * 200),
        }
        broad_market_closes = _walked_closes([0.002] * 200)
        block = compute_breadth_internals_pure(
            closes_by_ticker=closes_by_ticker,
            sector_members={"tech": ("AAPL", "MSFT")},
            universe_tickers=("AAPL", "MSFT"),
            broad_market_closes=broad_market_closes,
            as_of=_as_of(),
        )
        assert block.block_id == "q7.breadth_internals"
        # Three EMA windows plus advance/decline plus equal_vs_cap.
        for key in (
            "pct_above_20d_ema",
            "pct_above_50d_ema",
            "pct_above_200d_ema",
            "advance_decline_per_sector",
            "equal_vs_cap_weight",
        ):
            assert key in block.payload

    def test_audience_routes_to_correlation_brief_and_universal_broadcast(self) -> None:
        block = compute_breadth_internals_pure(
            closes_by_ticker={"X": _walked_closes([0.0] * 200)},
            sector_members={},
            universe_tickers=("X",),
            broad_market_closes=_walked_closes([0.0] * 200),
            as_of=_as_of(),
        )
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

    def test_bootstrap_when_observations_below_required(self) -> None:
        block = compute_breadth_internals_pure(
            closes_by_ticker={"X": _walked_closes([0.0] * 10)},
            sector_members={},
            universe_tickers=("X",),
            broad_market_closes=_walked_closes([0.0] * 10),
            as_of=_as_of(),
        )
        assert block.calibration_state is CalibrationState.ACCUMULATING


# ---------------------------------------------------------------------------
# cross_sector_rotation_compute
# ---------------------------------------------------------------------------


class TestCrossSectorRotationCompute:
    def test_sharp_velocity_when_two_etfs_move_opposite_above_threshold(self) -> None:
        # XLK rallies +5% on the last day; XLF drops -5% on the last day.
        short_closes = {
            "XLK": (100.0, 100.0, 105.0),
            "XLF": (100.0, 100.0, 95.0),
            "XLE": (100.0, 100.0, 100.0),
            "SMH": (100.0, 100.0, 100.0),
            "IWM": (100.0, 100.0, 100.0),
            "SPY": (100.0, 100.0, 100.0),
        }
        long_closes = {k: v for k, v in short_closes.items()}
        block = compute_cross_sector_rotation_pure(
            short_closes_by_etf=short_closes,
            long_closes_by_etf=long_closes,
            short_window_days=20,
            long_window_days=60,
            as_of=_as_of(),
        )
        assert block.payload["velocity_label"] == VELOCITY_SHARP

    def test_slow_velocity_when_all_moves_small(self) -> None:
        short_closes = {
            "XLK": (100.0, 100.5, 101.0),
            "XLF": (100.0, 99.8, 99.5),
            "XLE": (100.0, 100.0, 100.0),
            "SMH": (100.0, 100.0, 100.0),
            "IWM": (100.0, 100.0, 100.0),
            "SPY": (100.0, 100.0, 100.0),
        }
        block = compute_cross_sector_rotation_pure(
            short_closes_by_etf=short_closes,
            long_closes_by_etf=short_closes,
            short_window_days=20,
            long_window_days=60,
            as_of=_as_of(),
        )
        assert block.payload["velocity_label"] == VELOCITY_SLOW

    def test_rate_driven_narrative_when_xlf_xlk_pair_dominates(self) -> None:
        short_closes = {
            "XLF": (100.0, 100.0, 120.0),  # XLF up 20%
            "XLK": (100.0, 100.0, 100.0),  # XLK flat → XLF/XLK ratio change is large
            "XLE": (100.0, 100.0, 100.0),
            "SMH": (100.0, 100.0, 100.0),
            "IWM": (100.0, 100.0, 100.5),  # IWM/SPY barely moves
            "SPY": (100.0, 100.0, 100.0),
        }
        block = compute_cross_sector_rotation_pure(
            short_closes_by_etf=short_closes,
            long_closes_by_etf=short_closes,
            short_window_days=20,
            long_window_days=60,
            as_of=_as_of(),
        )
        assert block.payload["narrative_label"] == ROTATION_NARRATIVE_RATE_DRIVEN

    def test_risk_appetite_narrative_when_iwm_spy_pair_dominates(self) -> None:
        short_closes = {
            "XLF": (100.0, 100.0, 100.5),
            "XLK": (100.0, 100.0, 100.5),
            "XLE": (100.0, 100.0, 100.0),
            "SMH": (100.0, 100.0, 100.0),
            "IWM": (100.0, 100.0, 130.0),  # IWM up 30%
            "SPY": (100.0, 100.0, 100.0),
        }
        block = compute_cross_sector_rotation_pure(
            short_closes_by_etf=short_closes,
            long_closes_by_etf=short_closes,
            short_window_days=20,
            long_window_days=60,
            as_of=_as_of(),
        )
        assert block.payload["narrative_label"] == ROTATION_NARRATIVE_RISK_APPETITE_DRIVEN


# ---------------------------------------------------------------------------
# intermarket_regime_compute
# ---------------------------------------------------------------------------


class TestIntermarketRegimeCompute:
    def test_four_relationships_emit_one_block_each(self) -> None:
        # SPY and TLT positively correlated → inflation environment.
        closes = {
            "SPY": _walked_closes([0.01, -0.01, 0.005, -0.005] * 10),
            "TLT": _walked_closes([0.01, -0.01, 0.005, -0.005] * 10),
            "GLD": _walked_closes([0.005, -0.005, 0.01, -0.01] * 10),
            "XLE": _walked_closes([0.01, -0.005, 0.008, -0.012] * 10),
        }
        macros: dict[str, tuple[float, ...]] = {
            "DFII10": tuple(2.0 + 0.01 * i for i in range(40)),  # rising real yields
            "VIXCLS": tuple(15.0 + 0.5 * ((i % 2) - 0.5) for i in range(40)),
            "DCOILWTICO": tuple(75.0 + 0.01 * i for i in range(40)),
        }
        blocks = compute_intermarket_regime_pure(
            closes_by_ticker=closes,
            macros_by_series=macros,
            window_days=40,
            short_window_days=10,
            as_of=_as_of(),
        )
        block_ids = {b.block_id for b in blocks}
        assert block_ids == {
            "q7.intermarket_regime.spy_tlt",
            "q7.intermarket_regime.gld_real_yields",
            "q7.intermarket_regime.oil_xle_beta",
            "q7.intermarket_regime.vix_spy",
        }
        # Each block addresses both audiences.
        for block in blocks:
            assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            assert OutputAudience.UNIVERSAL_BROADCAST in block.audience

    def test_spy_tlt_positive_correlation_tags_inflation_environment(self) -> None:
        closes = {
            "SPY": _walked_closes([0.01, -0.01, 0.005, -0.005] * 10),
            "TLT": _walked_closes([0.01, -0.01, 0.005, -0.005] * 10),  # perfectly correlated
            "GLD": _walked_closes([0.0] * 40),
            "XLE": _walked_closes([0.0] * 40),
        }
        macros = {
            "DFII10": tuple(2.0 for _ in range(40)),
            "VIXCLS": tuple(15.0 for _ in range(40)),
            "DCOILWTICO": tuple(75.0 for _ in range(40)),
        }
        blocks = compute_intermarket_regime_pure(
            closes_by_ticker=closes,
            macros_by_series=macros,
            window_days=40,
            short_window_days=10,
            as_of=_as_of(),
        )
        spy_tlt = next(b for b in blocks if b.block_id == "q7.intermarket_regime.spy_tlt")
        assert spy_tlt.payload["regime_label"] == "inflation_environment"
        assert spy_tlt.payload["correlation"] == pytest.approx(1.0)

    def test_zero_observations_emit_null_numeric_fields_per_block(self) -> None:
        # Every input series empty — the unavailable case ALP-537 targets.
        # The block carries UNAVAILABLE calibration (ALP-540) AND the
        # numeric payload fields surface as None, not 0.0 — so a downstream
        # consumer can't read a fabricated zero correlation/beta.
        blocks = compute_intermarket_regime_pure(
            closes_by_ticker={
                "SPY": (),
                "TLT": (),
                "GLD": (),
                "XLE": (),
            },
            macros_by_series={
                "DFII10": (),
                "VIXCLS": (),
                "DCOILWTICO": (),
            },
            window_days=60,
            short_window_days=20,
            as_of=_as_of(),
        )
        by_id = {b.block_id: b for b in blocks}

        spy_tlt = by_id["q7.intermarket_regime.spy_tlt"]
        assert spy_tlt.calibration_state is CalibrationState.UNAVAILABLE
        assert spy_tlt.payload["correlation"] is None
        # regime_label is meaningless without a correlation to read it from.
        assert spy_tlt.payload["regime_label"] is None
        assert spy_tlt.anomaly_flags == ()

        gld = by_id["q7.intermarket_regime.gld_real_yields"]
        assert gld.calibration_state is CalibrationState.UNAVAILABLE
        assert gld.payload["correlation"] is None
        assert gld.anomaly_flags == ()

        oil = by_id["q7.intermarket_regime.oil_xle_beta"]
        assert oil.calibration_state is CalibrationState.UNAVAILABLE
        assert oil.payload["long_beta"] is None
        assert oil.payload["short_beta"] is None
        assert oil.payload["beta_drift"] is None
        assert oil.anomaly_flags == ()

        vix = by_id["q7.intermarket_regime.vix_spy"]
        assert vix.calibration_state is CalibrationState.UNAVAILABLE
        assert vix.payload["correlation"] is None
        assert vix.anomaly_flags == ()


# ---------------------------------------------------------------------------
# intra_sector_correlation_compute
# ---------------------------------------------------------------------------


class TestIntraSectorCorrelationCompute:
    def test_perfect_correlation_pair_yields_one_block(self) -> None:
        # AAPL and MSFT identical → correlation 1, no divergence flag fires
        # because every pair correlation is 1 so the deviation distribution
        # collapses.
        returns = [0.01, -0.005, 0.008, -0.012, 0.006] * 4  # 20 returns
        long_returns = {
            "AAPL": tuple(returns),
            "MSFT": tuple(returns),
        }
        block = compute_intra_sector_correlation_pure(
            sector="tech",
            sector_tickers=("AAPL", "MSFT"),
            long_returns_by_ticker=long_returns,
            short_window_days=10,
            long_window_days=20,
            divergence_sigma=1.5,
            as_of=_as_of(),
        )
        short_matrix = block.payload["short_window"]["correlation_matrix"]
        long_matrix = block.payload["long_window"]["correlation_matrix"]
        assert short_matrix["AAPL"]["MSFT"] == pytest.approx(1.0)
        assert long_matrix["AAPL"]["MSFT"] == pytest.approx(1.0)
        assert block.anomaly_flags == ()

    def test_pair_divergence_flag_fires_when_short_flips_sign(self) -> None:
        # Three tickers; A,B,C correlated over long window; in short window B
        # decorrelates from A. The pair-divergence detector compares the
        # short-window pair-correlation to the long-window pair-correlation,
        # normalised by the stdev of the long-window off-diagonal entries.
        long_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8  # 40
        long_b = [r + 0.0001 * (i % 3) for i, r in enumerate(long_a)]
        long_c = [r + 0.00005 * (i % 5) for i, r in enumerate(long_a)]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        short_c = list(short_a)
        long_returns = {
            "A": tuple(long_a + short_a),
            "B": tuple(long_b + short_b),
            "C": tuple(long_c + short_c),
        }
        block = compute_intra_sector_correlation_pure(
            sector="tech",
            sector_tickers=("A", "B", "C"),
            long_returns_by_ticker=long_returns,
            short_window_days=20,
            long_window_days=60,
            divergence_sigma=1.5,
            as_of=_as_of(),
        )
        names = [flag.name for flag in block.anomaly_flags]
        assert any("A:B" in name for name in names), f"expected A:B divergence; got {names}"


# ---------------------------------------------------------------------------
# lead_lag_compute
# ---------------------------------------------------------------------------


class TestLeadLagCompute:
    def test_overdue_flag_fires_when_lead_moves_alone(self) -> None:
        # 30 days of small returns then a 5% lead move on day 30; lag flat.
        baseline = [0.001 * ((i % 2) - 0.5) for i in range(30)]
        lead_returns = [*baseline, 0.05]
        lag_returns = [*baseline, 0.0]
        pair = LeadLagPair(
            pair_key="credit_to_equity",
            lead_ticker="HYG",
            lag_ticker="SPY",
            max_days=5,
        )
        inputs = LeadLagInputs(
            pair=pair,
            lead_returns=tuple(lead_returns),
            lag_returns=tuple(lag_returns),
            persisted=None,
        )
        result = compute_lead_lag_pure(
            pair_inputs=(inputs,),
            overdue_lead_sigma=2.0,
        )
        flag_names = {flag.name for block in result for flag in block.anomaly_flags}
        assert "overdue_lag_flag:credit_to_equity" in flag_names

    def test_overdue_flag_silent_when_lag_tracks_lead(self) -> None:
        # When the lag's recent magnitude matches the lead's recent move,
        # the overdue-lag flag is suppressed. The structural inversion
        # check is orthogonal — symmetric high-z data on both legs can
        # still fire it — so the assertion narrows to overdue.
        baseline = [0.001 * ((i % 2) - 0.5) for i in range(30)]
        lead_returns = [*baseline, 0.05]
        lag_returns = [*baseline, 0.04]  # tracked
        pair = LeadLagPair(
            pair_key="credit_to_equity",
            lead_ticker="HYG",
            lag_ticker="SPY",
            max_days=5,
        )
        inputs = LeadLagInputs(
            pair=pair,
            lead_returns=tuple(lead_returns),
            lag_returns=tuple(lag_returns),
            persisted=None,
        )
        result = compute_lead_lag_pure(
            pair_inputs=(inputs,),
            overdue_lead_sigma=2.0,
        )
        flag_names = {flag.name for block in result for flag in block.anomaly_flags}
        assert "overdue_lag_flag:credit_to_equity" not in flag_names

    def test_persisted_estimate_threads_through_payload(self) -> None:
        pair = LeadLagPair(
            pair_key="semis_to_tech",
            lead_ticker="SMH",
            lag_ticker="XLK",
            max_days=5,
        )
        inputs = LeadLagInputs(
            pair=pair,
            lead_returns=tuple([0.001] * 10),
            lag_returns=tuple([0.001] * 10),
            persisted=(2.5, 100, CalibrationState.CALIBRATED.value),
        )
        blocks = compute_lead_lag_pure(
            pair_inputs=(inputs,),
            overdue_lead_sigma=2.0,
        )
        assert len(blocks) == 1
        payload = blocks[0].payload["pair_lag"]["semis_to_tech"]
        assert payload["lead_lag_days_estimate"] == 2.5
        assert payload["n_pair_events"] == 100
        assert blocks[0].calibration_state is CalibrationState.CALIBRATED


# ---------------------------------------------------------------------------
# correlation_regime_change_compute
# ---------------------------------------------------------------------------


class TestCorrelationRegimeChangeCompute:
    def test_emits_dispersion_block_for_universe_returns(self) -> None:
        params = CorrelationRegimeChangeParameters(
            short_window_days=5,
            long_window_days=10,
            correlation_breakdown_sigma=2.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.05,
            dispersion_window_days=5,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        # 10 days of returns across 3 tickers — the per-day cross-ticker
        # dispersion needs to vary across days so the trailing-window
        # stdev is positive (otherwise the z-score is undefined and the
        # block declines to emit).
        returns_by_ticker = {
            "A": (0.001, -0.002, 0.003, -0.001, 0.002, -0.003, 0.001, -0.002, 0.002, 0.05),
            "B": (0.001, 0.002, -0.001, 0.003, -0.002, 0.001, -0.003, 0.002, -0.001, -0.05),
            "C": (-0.001, 0.001, 0.002, -0.002, 0.001, 0.002, -0.001, 0.003, -0.002, 0.02),
        }
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B", "C"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=True,  # silence flag suppressed
            params=params,
            as_of=_as_of(),
        )
        block_ids = {b.block_id for b in blocks}
        assert "q7.correlation_breakdown.dispersion_shift" in block_ids

    def test_narrative_lag_fires_when_media_silent_and_breakdown_happens(self) -> None:
        # noise_floor=0 here because the inversion fixture below averages to a
        # near-zero long_corr (40 days at +1, 20 days at -1), which the ALP-541
        # guard would otherwise legitimately filter as having no stable
        # baseline. The breakdown math itself is the unit under test.
        params = CorrelationRegimeChangeParameters(
            short_window_days=20,
            long_window_days=60,
            correlation_breakdown_sigma=1.0,  # low gate → breakdown easy to fire
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.0,
            dispersion_window_days=20,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        long_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        long_b = [r + 0.0001 * (i % 3) for i, r in enumerate(long_a)]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        returns_by_ticker = {
            "A": tuple(long_a + short_a),
            "B": tuple(long_b + short_b),
        }
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=False,  # silence
            params=params,
            as_of=_as_of(),
        )
        narrative = next((b for b in blocks if b.block_id == "q7.narrative_lag"), None)
        assert narrative is not None
        flag_names = {flag.name for flag in narrative.anomaly_flags}
        assert "narrative_lag_flag" in flag_names

    def test_narrative_lag_suppressed_when_qualifying_news_present(self) -> None:
        # See sibling test re: noise_floor=0 with the inversion fixture.
        params = CorrelationRegimeChangeParameters(
            short_window_days=20,
            long_window_days=60,
            correlation_breakdown_sigma=1.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.0,
            dispersion_window_days=20,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        long_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        long_b = [r + 0.0001 * (i % 3) for i, r in enumerate(long_a)]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        returns_by_ticker = {
            "A": tuple(long_a + short_a),
            "B": tuple(long_b + short_b),
        }
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=True,  # silence cleared
            params=params,
            as_of=_as_of(),
        )
        narrative = next((b for b in blocks if b.block_id == "q7.narrative_lag"), None)
        if narrative is not None:
            flag_names = {flag.name for flag in narrative.anomaly_flags}
            assert "narrative_lag_flag" not in flag_names


class TestCorrelationBreakdownDataAlignmentGuards:
    """ALP-541: phantom breakdowns from sparse data / near-zero baselines."""

    @staticmethod
    def _breakdown_inducing_returns() -> tuple[list[float], list[float]]:
        """60-day series whose recent 20 days invert the prior 40-day correlation.

        Used by the guard tests to stage a pair that *would* fire a breakdown
        block at sigma 1.0 if not for the data-alignment guards.
        """
        long_a = [0.01, -0.005, 0.008, -0.012, 0.006] * 8
        long_b = [r + 0.0001 * (i % 3) for i, r in enumerate(long_a)]
        short_a = [0.01, -0.02, 0.015, 0.005, -0.01,
                   0.012, -0.018, 0.02, -0.005, 0.008,
                   -0.015, 0.01, -0.005, 0.012, -0.008,
                   0.005, -0.012, 0.018, -0.01, 0.005]  # fmt: skip
        short_b = [-x for x in short_a]
        return long_a + short_a, long_b + short_b

    def test_pair_with_sparse_history_emits_no_breakdown_block(self) -> None:
        """A ticker with only 25% of the long-window observations is filtered.

        Reproduces the CTRA_TSLA phantom: B's series is too short to align
        against A's prior window, so ``_pearson_correlation`` returns 0.0 and
        the sigma-test inflates the deviation. The overlap guard must skip
        this pair entirely rather than publishing a phantom breakdown.
        """
        params = CorrelationRegimeChangeParameters(
            short_window_days=20,
            long_window_days=60,
            correlation_breakdown_sigma=1.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.05,
            dispersion_window_days=20,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        a_full, _ = self._breakdown_inducing_returns()
        # B has only 15 observations — far below 90% of the 60-day window.
        returns_by_ticker = {
            "A": tuple(a_full),
            "B": tuple(a_full[-15:]),
        }
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=True,
            params=params,
            as_of=_as_of(),
        )
        pair_blocks = [
            b
            for b in blocks
            if b.block_id.startswith("q7.correlation_breakdown.")
            and b.block_id != "q7.correlation_breakdown.dispersion_shift"
        ]
        assert pair_blocks == [], (
            f"expected no pair-level breakdowns for a sparse-history pair; "
            f"got {[b.block_id for b in pair_blocks]}"
        )

    def test_pair_with_near_zero_long_correlation_emits_no_breakdown_block(self) -> None:
        """A pair whose long-window correlation magnitude is below the floor is filtered.

        Reproduces the C_GOOGL phantom: the short-window correlation drifts
        to a near-zero magnitude that, against a plausible long-window
        baseline, looks like a 3-sigma shift — but only because the test is
        comparing two essentially-zero values. The noise-floor guard must
        block the sigma-flag when ``|long_corr|`` is below the floor.
        """
        # Construct a pair whose 60-day correlation is essentially zero
        # (A and B are nearly uncorrelated across the window) but whose
        # short-window correlation drifts away from zero enough to look
        # like a 1-sigma shift at sigma 1.0.
        long_a = [0.01, -0.01, 0.012, -0.008, 0.005, -0.013, 0.009, -0.004] * 8
        long_b = [(-1.0 if i % 2 else 1.0) * 0.011 for i in range(len(long_a))]
        returns_by_ticker = {
            "A": tuple(long_a),
            "B": tuple(long_b),
        }
        params = CorrelationRegimeChangeParameters(
            short_window_days=20,
            long_window_days=60,
            correlation_breakdown_sigma=1.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.5,  # aggressive floor so any near-zero baseline trips it
            dispersion_window_days=20,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=True,
            params=params,
            as_of=_as_of(),
        )
        pair_blocks = [
            b
            for b in blocks
            if b.block_id.startswith("q7.correlation_breakdown.")
            and b.block_id != "q7.correlation_breakdown.dispersion_shift"
        ]
        assert pair_blocks == [], (
            f"expected no pair-level breakdowns when |long_corr| is below the floor; "
            f"got {[(b.block_id, b.payload.get('long_correlation')) for b in pair_blocks]}"
        )

    def test_breakdown_block_payload_carries_overlap_observation_count(self) -> None:
        """A breakdown block's payload exposes the overlapping observation count.

        AC #3 — downstream agents auditing a sigma-flag need to see both
        the long-window correlation magnitude (already present) and the
        count of overlapping observations the sigma-test ran on. Without
        it, the synthesizer can't distinguish a high-sigma flag computed
        on full history from one computed on a sparse-overlap edge case
        that squeaked past the guard.

        ``correlation_noise_floor=0.0`` here because the breakdown-inducing
        fixture inverts B's returns in the short window — that drags the
        long-window correlation to ~0, which the noise-floor guard would
        legitimately filter; the payload-shape assertion is orthogonal.
        """
        params = CorrelationRegimeChangeParameters(
            short_window_days=20,
            long_window_days=60,
            correlation_breakdown_sigma=1.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.0,
            dispersion_window_days=20,
            dispersion_sigma=1.5,
            media_silence_hours=12,
        )
        a_full, b_full = self._breakdown_inducing_returns()
        returns_by_ticker = {
            "A": tuple(a_full),
            "B": tuple(b_full),
        }
        blocks = compute_correlation_regime_change_pure(
            universe_tickers=("A", "B"),
            long_returns_by_ticker=returns_by_ticker,
            qualifying_news_present=True,
            params=params,
            as_of=_as_of(),
        )
        pair_blocks = [
            b
            for b in blocks
            if b.block_id.startswith("q7.correlation_breakdown.")
            and b.block_id != "q7.correlation_breakdown.dispersion_shift"
        ]
        assert pair_blocks, "expected at least one breakdown block from the staged pair"
        payload = pair_blocks[0].payload
        assert payload["n_overlapping_observations"] == 60
        assert "long_correlation" in payload


# ---------------------------------------------------------------------------
# assemble — pure assembly path
# ---------------------------------------------------------------------------


class TestAssembleQ7BlocksFromInputs:
    def test_empty_scope_returns_no_blocks(self) -> None:
        inputs = Q7Inputs(
            as_of=_as_of(),
            ticker_scope=(),
            intra_sector_blocks=(),
            cross_sector_blocks=(),
            breadth_blocks=(),
            intermarket_blocks=(),
            lead_lag_blocks=(),
            correlation_regime_change_blocks=(),
            pair_correlations=None,
        )
        assert assemble_q7_blocks_from_inputs(inputs) == []

    def test_flattens_per_sub_blocks_in_order(self) -> None:
        # Hand-build one block per sub via the pure computes and assemble.
        breadth = compute_breadth_internals_pure(
            closes_by_ticker={"X": _walked_closes([0.0] * 200)},
            sector_members={},
            universe_tickers=("X",),
            broad_market_closes=_walked_closes([0.0] * 200),
            as_of=_as_of(),
        )
        inputs = Q7Inputs(
            as_of=_as_of(),
            ticker_scope=("X",),
            intra_sector_blocks=(),
            cross_sector_blocks=(),
            breadth_blocks=(breadth,),
            intermarket_blocks=(),
            lead_lag_blocks=(),
            correlation_regime_change_blocks=(),
            pair_correlations=None,
        )
        blocks = assemble_q7_blocks_from_inputs(inputs)
        assert [b.block_id for b in blocks] == ["q7.breadth_internals"]
