"""Tests for relative-performance computation — story 08a.

Covers:

- Rolling ratio of ticker vs. sector ETF and vs. SPY at 5-day and 20-day windows.
- Intra-sector ranking: percentile + leader/laggard quartile classification.
- Relative-strength regime change flag on leader↔laggard transition.
"""

from __future__ import annotations

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.distillation.q1.relative_performance import (
    RS_REGIME_LAGGARD,
    RS_REGIME_LEADER,
    RS_REGIME_MID,
    RelativePerformanceResult,
    compute_relative_performance,
    detect_relative_strength_regime_change,
    rank_intra_sector,
)


class TestComputeRelativePerformance:
    def test_outperforming_sector_etf_yields_positive_excess_return(self) -> None:
        # Ticker rises 10% over 5 days; sector ETF rises 5%; ticker outperforms.
        ticker_5d_return = 0.10
        sector_5d_return = 0.05
        ticker_20d_return = 0.20
        sector_20d_return = 0.10
        spy_5d_return = 0.02
        spy_20d_return = 0.05
        result = compute_relative_performance(
            ticker_5d_return=ticker_5d_return,
            ticker_20d_return=ticker_20d_return,
            sector_5d_return=sector_5d_return,
            sector_20d_return=sector_20d_return,
            spy_5d_return=spy_5d_return,
            spy_20d_return=spy_20d_return,
        )
        assert isinstance(result, RelativePerformanceResult)
        assert result.vs_sector_5d == pytest.approx(0.10 - 0.05)
        assert result.vs_sector_20d == pytest.approx(0.20 - 0.10)
        assert result.vs_spy_5d == pytest.approx(0.10 - 0.02)
        assert result.vs_spy_20d == pytest.approx(0.20 - 0.05)


class TestRankIntraSector:
    def test_top_quartile_is_leader_bottom_is_laggard(self) -> None:
        peer_returns = {
            "AAPL": 0.10,
            "MSFT": 0.08,
            "NVDA": 0.05,
            "GOOG": 0.04,
            "AMZN": 0.02,
            "META": -0.01,
            "TSLA": -0.03,
            "AVGO": -0.05,
        }
        # Top of distribution → leader.
        top = rank_intra_sector(ticker=Symbol("AAPL"), peer_returns=peer_returns)
        assert top.regime_label == RS_REGIME_LEADER
        assert top.percentile >= 75.0

        # Bottom of distribution → laggard.
        bottom = rank_intra_sector(ticker=Symbol("AVGO"), peer_returns=peer_returns)
        assert bottom.regime_label == RS_REGIME_LAGGARD
        assert bottom.percentile <= 25.0

    def test_middle_quartiles_are_mid(self) -> None:
        # 8-name distribution; the median name (NVDA at 0.04) sits in the
        # interquartile range, neither leader nor laggard.
        peer_returns = {
            "AAPL": 0.10,
            "MSFT": 0.08,
            "AVGO": 0.06,
            "NVDA": 0.04,
            "GOOG": 0.03,
            "AMZN": 0.02,
            "META": 0.01,
            "TSLA": 0.0,
        }
        mid = rank_intra_sector(ticker=Symbol("NVDA"), peer_returns=peer_returns)
        assert mid.regime_label == RS_REGIME_MID

    def test_single_member_universe_is_mid(self) -> None:
        """A singleton sector cannot be ranked into quartiles meaningfully."""
        peer_returns = {"AAPL": 0.05}
        result = rank_intra_sector(ticker=Symbol("AAPL"), peer_returns=peer_returns)
        assert result.regime_label == RS_REGIME_MID


class TestDetectRelativeStrengthRegimeChange:
    def test_leader_to_laggard_flags_regime_change(self) -> None:
        flag = detect_relative_strength_regime_change(
            prior_label=RS_REGIME_LEADER,
            current_label=RS_REGIME_LAGGARD,
        )
        assert flag.fired is True
        assert flag.transition == "leader_to_laggard"

    def test_laggard_to_leader_flags_regime_change(self) -> None:
        flag = detect_relative_strength_regime_change(
            prior_label=RS_REGIME_LAGGARD,
            current_label=RS_REGIME_LEADER,
        )
        assert flag.fired is True
        assert flag.transition == "laggard_to_leader"

    def test_mid_to_leader_does_not_flag(self) -> None:
        flag = detect_relative_strength_regime_change(
            prior_label=RS_REGIME_MID,
            current_label=RS_REGIME_LEADER,
        )
        assert flag.fired is False
