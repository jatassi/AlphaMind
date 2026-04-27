"""Tests for src/alphamind/data_sources/fred/series.py."""

from __future__ import annotations


def test_daily_and_monthly_series_lists_exist() -> None:
    """DAILY_SERIES and MONTHLY_SERIES are non-empty lists of strings."""
    from alphamind.data_sources.fred.series import DAILY_SERIES, MONTHLY_SERIES

    assert isinstance(DAILY_SERIES, list)
    assert isinstance(MONTHLY_SERIES, list)
    assert len(DAILY_SERIES) > 0
    assert len(MONTHLY_SERIES) > 0
    assert all(isinstance(s, str) for s in DAILY_SERIES)
    assert all(isinstance(s, str) for s in MONTHLY_SERIES)


def test_required_series_are_present() -> None:
    """All series required by the spec are in DAILY_SERIES or MONTHLY_SERIES."""
    from alphamind.data_sources.fred.series import DAILY_SERIES, MONTHLY_SERIES

    all_series = set(DAILY_SERIES) | set(MONTHLY_SERIES)

    # From api-key-checklist.md + 05b spec
    required = {
        # Yields
        "DGS10",
        "DGS2",
        "T10Y2Y",
        "T10YIE",
        # Breakeven inflation
        "T5YIE",
        # Credit spreads
        "BAMLH0A0HYM2",
        "BAMLC0A0CM",
        # Funding / repo
        "SOFR",
        "RRPONTSYD",
        # Other macro
        "VIXCLS",
        "DTWEXBGS",
        "DCOILWTICO",
        "CPIAUCSL",
        "PCEPI",
        "STLFSI4",
        "DFEDTARU",
    }
    missing = required - all_series
    assert not missing, f"Missing series: {missing}"


def test_no_duplicates_across_lists() -> None:
    """A series should not appear in both DAILY_SERIES and MONTHLY_SERIES."""
    from alphamind.data_sources.fred.series import DAILY_SERIES, MONTHLY_SERIES

    overlap = set(DAILY_SERIES) & set(MONTHLY_SERIES)
    assert not overlap, f"Series in both lists: {overlap}"
