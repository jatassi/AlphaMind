"""Tests for bls/series.py — the configurable series registry."""

from __future__ import annotations

from alphamind.data_sources.bls.series import SERIES


class TestSeriesRegistry:
    def test_contains_total_nonfarm_payrolls(self) -> None:
        assert "CES0000000001" in SERIES

    def test_contains_unemployment_rate(self) -> None:
        assert "LNS14000000" in SERIES

    def test_contains_cpi_u(self) -> None:
        assert "CUSR0000SA0" in SERIES

    def test_each_entry_has_frequency_and_units(self) -> None:
        for series_id, meta in SERIES.items():
            assert "frequency" in meta, f"{series_id} missing 'frequency'"
            assert "units" in meta, f"{series_id} missing 'units'"

    def test_frequency_values_are_valid(self) -> None:
        valid = {"daily", "weekly", "monthly", "quarterly", "annual"}
        for series_id, meta in SERIES.items():
            assert meta["frequency"] in valid, (
                f"{series_id} has invalid frequency {meta['frequency']!r}"
            )
