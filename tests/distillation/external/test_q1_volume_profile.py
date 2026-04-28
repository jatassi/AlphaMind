"""Tests for the volume profile computation — story 08a.

Hand-constructed volume distributions verify the value-area, point-of-control,
and high/low-volume node extraction; a multi-session fixture verifies the
developing vs. settled classification with the named overlap threshold.
"""

from __future__ import annotations

from alphamind.distillation.q1.volume_profile import (
    PROFILE_DEVELOPING,
    PROFILE_SETTLED,
    SETTLED_OVERLAP_THRESHOLD,
    VALUE_AREA_VOLUME_FRACTION,
    PriceLevelVolume,
    classify_session_profile,
    compute_volume_profile,
)


class TestComputeVolumeProfile:
    def test_value_area_captures_seventy_percent_of_volume(self) -> None:
        """Value area accumulates levels by volume descending until ≥ 70% captured."""
        # 10 price levels, volume concentrated on three middle levels.
        levels = [
            PriceLevelVolume(price=100.0, volume=10),
            PriceLevelVolume(price=101.0, volume=20),
            PriceLevelVolume(price=102.0, volume=30),
            PriceLevelVolume(price=103.0, volume=300),  # Will be POC.
            PriceLevelVolume(price=104.0, volume=200),
            PriceLevelVolume(price=105.0, volume=200),
            PriceLevelVolume(price=106.0, volume=30),
            PriceLevelVolume(price=107.0, volume=20),
            PriceLevelVolume(price=108.0, volume=10),
            PriceLevelVolume(price=109.0, volume=5),
        ]
        result = compute_volume_profile(levels)
        # Total volume = 825. 70% target = 577.5. Top three (300+200+200=700)
        # passes the threshold first; the value area must include those three.
        assert 103.0 in result.value_area_prices
        assert 104.0 in result.value_area_prices
        assert 105.0 in result.value_area_prices
        # Captured fraction must be at least the 70% target.
        assert result.value_area_volume_fraction >= VALUE_AREA_VOLUME_FRACTION

    def test_point_of_control_is_max_volume_price(self) -> None:
        levels = [
            PriceLevelVolume(price=100.0, volume=10),
            PriceLevelVolume(price=101.0, volume=20),
            PriceLevelVolume(price=102.0, volume=300),
            PriceLevelVolume(price=103.0, volume=20),
        ]
        result = compute_volume_profile(levels)
        assert result.point_of_control == 102.0

    def test_high_and_low_volume_nodes_are_top_and_bottom_quartiles(self) -> None:
        """High-volume nodes are the top quartile by volume; low are the bottom quartile."""
        levels = [
            PriceLevelVolume(price=100.0, volume=10),
            PriceLevelVolume(price=101.0, volume=20),
            PriceLevelVolume(price=102.0, volume=30),
            PriceLevelVolume(price=103.0, volume=300),
            PriceLevelVolume(price=104.0, volume=200),
            PriceLevelVolume(price=105.0, volume=15),
            PriceLevelVolume(price=106.0, volume=12),
            PriceLevelVolume(price=107.0, volume=8),
        ]
        result = compute_volume_profile(levels)
        # 8 levels — top quartile is 2 levels with the highest volumes.
        assert set(result.high_volume_nodes) == {103.0, 104.0}
        # Low quartile is 2 levels with the lowest volumes.
        assert set(result.low_volume_nodes) == {107.0, 100.0}


class TestClassifySessionProfile:
    def test_substantial_overlap_is_settled(self) -> None:
        """Current value area overlapping the trailing pool above the threshold = settled."""
        # Current value area covers prices 100..105.
        current_va = (100.0, 105.0)
        # Trailing pool covers prices 99..106 - wide overlap, near full.
        pool_va = (99.0, 106.0)
        classification = classify_session_profile(
            current_value_area_range=current_va,
            multi_session_value_area_range=pool_va,
        )
        assert classification == PROFILE_SETTLED

    def test_minimal_overlap_is_developing(self) -> None:
        """Current value area shifting outside the pool = developing."""
        current_va = (110.0, 115.0)
        pool_va = (100.0, 106.0)
        classification = classify_session_profile(
            current_value_area_range=current_va,
            multi_session_value_area_range=pool_va,
        )
        assert classification == PROFILE_DEVELOPING

    def test_overlap_exactly_at_threshold_is_settled(self) -> None:
        """At-threshold overlap is classified settled (inclusive boundary)."""
        # Pool spans 100..110 (10 wide). Construct a current VA whose overlap
        # equals exactly SETTLED_OVERLAP_THRESHOLD * pool width.
        pool_width = 10.0
        overlap = SETTLED_OVERLAP_THRESHOLD * pool_width
        current_va = (100.0, 100.0 + overlap)
        pool_va = (100.0, 110.0)
        classification = classify_session_profile(
            current_value_area_range=current_va,
            multi_session_value_area_range=pool_va,
        )
        assert classification == PROFILE_SETTLED
