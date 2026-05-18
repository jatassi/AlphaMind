"""Tests for the Q1 output-block assembly - story 08a.

Verifies that each indicator group emits an ``OutputBlock`` with:

- the documented ``q1.*`` block_id namespace,
- a sector-scoped ``audience`` derived from ``sector_classification.alphamind_sector``,
- the per-ticker payload convention pinned across 08a-f
  (``payload["per_ticker"]`` keyed by ticker, sorted).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q1.output_blocks import (
    AUDIENCE_BY_SECTOR,
    BLOCK_ID_DIVERGENCE_FLAGS,
    BLOCK_ID_GAP,
    BLOCK_ID_RELATIVE_PERFORMANCE,
    BLOCK_ID_TECHNICALS,
    BLOCK_ID_TREND_STATE,
    BLOCK_ID_VOLUME_PROFILE,
    audience_for_sector,
    build_q1_block,
)

# ---------------------------------------------------------------------------
# Audience mapping — sector_classification.alphamind_sector → OutputAudience
# ---------------------------------------------------------------------------


class TestAudienceForSector:
    def test_tech_maps_to_tech_semis(self) -> None:
        assert audience_for_sector("tech") is OutputAudience.SECTOR_TECH_SEMIS

    def test_semis_maps_to_tech_semis(self) -> None:
        assert audience_for_sector("semis") is OutputAudience.SECTOR_TECH_SEMIS

    def test_financials_maps_to_financials(self) -> None:
        assert audience_for_sector("financials") is OutputAudience.SECTOR_FINANCIALS

    def test_energy_maps_to_energy(self) -> None:
        assert audience_for_sector("energy") is OutputAudience.SECTOR_ENERGY

    def test_unknown_sector_raises(self) -> None:
        with pytest.raises(ValueError, match="alphamind_sector"):
            audience_for_sector("crypto")

    def test_audience_by_sector_carries_only_three_entries(self) -> None:
        """The mapping covers exactly the four sectors mapped to three audiences."""
        assert set(AUDIENCE_BY_SECTOR.keys()) == {"tech", "semis", "financials", "energy"}


# ---------------------------------------------------------------------------
# build_q1_block — per_ticker payload convention
# ---------------------------------------------------------------------------


class TestBuildQ1Block:
    def test_block_id_uses_q1_namespace_and_sector_audience(self) -> None:
        block = build_q1_block(
            block_id=BLOCK_ID_TECHNICALS,
            sector="tech",
            per_ticker={"AAPL": {"rsi": 55.0}},
            freshness_ts=datetime(2026, 4, 27, tzinfo=UTC),
            calibration_state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
        assert isinstance(block, OutputBlock)
        assert block.block_id == BLOCK_ID_TECHNICALS
        assert block.audience == frozenset({OutputAudience.SECTOR_TECH_SEMIS})

    def test_per_ticker_payload_is_sorted(self) -> None:
        """The payload uses the per_ticker convention with deterministic key order."""
        block = build_q1_block(
            block_id=BLOCK_ID_VOLUME_PROFILE,
            sector="tech",
            per_ticker={
                "MSFT": {"poc": 102.0},
                "AAPL": {"poc": 100.0},
            },
            freshness_ts=datetime(2026, 4, 27, tzinfo=UTC),
            calibration_state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
        per_ticker = block.payload["per_ticker"]
        # The dict is materialized via ``dict(sorted(...))`` so its insertion
        # order is the sorted ticker order.
        assert list(per_ticker.keys()) == ["AAPL", "MSFT"]

    def test_anomaly_flags_attach_to_the_block(self) -> None:
        flag = AnomalyFlag(name="volume_anomaly", magnitude=3.0, severity="investigate_now")
        block = build_q1_block(
            block_id=BLOCK_ID_TECHNICALS,
            sector="tech",
            per_ticker={"AAPL": {"rsi": 90.0}},
            freshness_ts=datetime(2026, 4, 27, tzinfo=UTC),
            calibration_state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
            anomaly_flags=(flag,),
        )
        assert block.anomaly_flags == (flag,)

    def test_block_ids_use_documented_q1_namespace(self) -> None:
        """All six block_id constants live under the ``q1.*`` prefix per the story spec."""
        assert BLOCK_ID_TECHNICALS == "q1.technicals"
        assert BLOCK_ID_VOLUME_PROFILE == "q1.volume_profile"
        assert BLOCK_ID_GAP == "q1.gap"
        assert BLOCK_ID_RELATIVE_PERFORMANCE == "q1.relative_performance"
        assert BLOCK_ID_TREND_STATE == "q1.trend_state"
        assert BLOCK_ID_DIVERGENCE_FLAGS == "q1.divergence_flags"

    def test_financials_block_routes_to_financials_audience(self) -> None:
        block = build_q1_block(
            block_id=BLOCK_ID_TECHNICALS,
            sector="financials",
            per_ticker={"JPM": {"rsi": 50.0}},
            freshness_ts=datetime(2026, 4, 27, tzinfo=UTC),
            calibration_state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
        assert block.audience == frozenset({OutputAudience.SECTOR_FINANCIALS})

    def test_bootstrap_block_carries_bootstrap_reason(self) -> None:
        block = build_q1_block(
            block_id=BLOCK_ID_GAP,
            sector="tech",
            per_ticker={"AAPL": {"gap_fill_probability": 0.45}},
            freshness_ts=datetime(2026, 4, 27, tzinfo=UTC),
            calibration_state=CalibrationState.ACCUMULATING,
            bootstrap_reason="gap_fill_min_events: 5 < 30",
        )
        assert block.calibration_state is CalibrationState.ACCUMULATING
        assert block.bootstrap_reason == "gap_fill_min_events: 5 < 30"
