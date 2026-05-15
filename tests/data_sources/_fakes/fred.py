"""In-memory fake for :class:`FredAPI`.

``get_series`` and ``get_series_info`` return whatever was loaded into
the ``series_data`` / ``series_info`` maps, falling back to a default
when present.  Tests load deterministic ``pd.Series`` data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class FakeFredAPI:
    """Stateful fake implementing :class:`FredAPI`."""

    series_data: dict[str, pd.Series] = field(default_factory=dict)
    series_info: dict[str, pd.Series] = field(default_factory=dict)
    series_all_releases: dict[str, pd.DataFrame] = field(default_factory=dict)

    default_series: pd.Series | None = None
    default_info: pd.Series | None = None

    connectivity: bool = True
    series_error: Exception | None = None
    info_error: Exception | None = None

    # Call-tracking
    get_series_calls: list[dict[str, Any]] = field(default_factory=list)
    get_series_info_calls: list[str] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get_series(self, series_id: str, **kwargs: Any) -> Any:
        self.get_series_calls.append({"series_id": series_id, **kwargs})
        if self.series_error is not None:
            raise self.series_error
        if series_id in self.series_data:
            return self.series_data[series_id]
        if self.default_series is not None:
            return self.default_series
        return pd.Series(dtype=float)

    def get_series_info(self, series_id: str) -> Any:
        self.get_series_info_calls.append(series_id)
        if self.info_error is not None:
            raise self.info_error
        if series_id in self.series_info:
            return self.series_info[series_id]
        if self.default_info is not None:
            return self.default_info
        return pd.Series(
            {
                "id": series_id,
                "title": series_id,
                "frequency_short": "D",
                "units": "Units",
            }
        )

    def get_series_all_releases(self, series_id: str, **kwargs: Any) -> Any:
        return self.series_all_releases.get(series_id, pd.DataFrame())


def make_series_data(dates: list[Any], values: list[float]) -> pd.Series:
    """Build a pandas Series mimicking fredapi.Fred.get_series() output."""
    return pd.Series(
        data=values,
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]),
    )


def make_series_info(
    series_id: str = "DGS10",
    frequency_short: str = "D",
    units: str = "Percent",
    title: str = "10-Year Treasury",
) -> pd.Series:
    """Build a pandas Series mimicking fredapi.Fred.get_series_info() output."""
    return pd.Series(
        {
            "id": series_id,
            "title": title,
            "frequency_short": frequency_short,
            "units": units,
            "observation_start": "2000-01-01",
            "observation_end": "2026-04-26",
        }
    )
