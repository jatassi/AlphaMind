"""Tests that FINRA collectors are registered in scheduler and bootstrap."""

from __future__ import annotations


class TestSchedulerRegistration:
    def test_finra_short_volume_in_collectors(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert "finra.short_volume" in COLLECTORS

    def test_finra_short_interest_in_collectors(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert "finra.short_interest" in COLLECTORS

    def test_finra_short_volume_is_callable(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert callable(COLLECTORS["finra.short_volume"])

    def test_finra_short_interest_is_callable(self) -> None:
        from alphamind.collector.scheduler import COLLECTORS

        assert callable(COLLECTORS["finra.short_interest"])


class TestBootstrapRegistration:
    def test_finra_in_known_vendors(self) -> None:
        from alphamind.collector.bootstrap import _KNOWN_VENDORS

        assert "finra" in _KNOWN_VENDORS

    def test_run_all_accepts_finra_vendor(self) -> None:
        """run_all(only_vendor='finra') does not raise ValueError."""
        from unittest.mock import patch

        from alphamind.collector.bootstrap import run_all

        with (
            patch(
                "alphamind.collector.bootstrap.bootstrap_short_volume",
                return_value=None,
            ) as mock_vol,
            patch(
                "alphamind.collector.bootstrap.bootstrap_short_interest",
                return_value=None,
            ) as mock_si,
        ):
            run_all(only_vendor="finra")

        mock_vol.assert_called_once()
        mock_si.assert_called_once()
