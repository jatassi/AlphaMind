"""
FRED vendor client.

Wraps the ``fredapi`` SDK with:
- :func:`~alphamind.data_sources._common.with_retries` (critical tier)
- :class:`~alphamind.data_sources._common.RateLimiter` (120 req/min)
- :func:`~alphamind.data_sources._common.track_run` integration
"""

from __future__ import annotations

import pandas as pd
from fredapi import Fred

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

# Module-level shared rate limiter instance.
_rate_limiter = RateLimiter()
_rate_limiter.set_limit("fred", rate_per_minute=120)

_CONNECTIVITY_PROBE_SERIES = "DGS10"


class FredClient:
    """
    Thin wrapper around :class:`fredapi.Fred` with rate-limiting and retries.

    Parameters
    ----------
    api_key:
        FRED API key.  When *None* the ``fredapi`` SDK resolves from the
        ``FRED_API_KEY`` environment variable.
    rate_limiter:
        Injectable :class:`~alphamind.data_sources._common.RateLimiter` for
        testing.  Defaults to the module-level shared instance.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._fred = Fred(api_key=api_key)
        self._rl = rate_limiter if rate_limiter is not None else _rate_limiter

    # ------------------------------------------------------------------
    # Connectivity check
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """
        Return *True* if the API key is valid and FRED is reachable.

        Makes a single lightweight probe request (:pydata:`DGS10` series info).
        Suitable for smoke-testing on startup.
        """
        try:
            self._rl.acquire("fred")
            self._fred.get_series_info(_CONNECTIVITY_PROBE_SERIES)
        except Exception:
            return False
        else:
            return True

    # ------------------------------------------------------------------
    # Rate-limited, retried SDK calls
    # ------------------------------------------------------------------

    # FRED's free-tier endpoints intermittently return 502/503 for several
    # minutes during vendor outages; the standard ``critical`` 1s/2s schedule
    # exhausts before the vendor recovers and the 4h-cadence collector misses
    # its freshness window after two consecutive misses. ``vendor_outage_extended``
    # buys 65s of in-line wait per call — well under the 4h cadence.
    @with_retries(RetryShape.vendor_outage_extended)
    def get_series(self, series_id: str, **kwargs: object) -> object:
        """Fetch a time series from FRED with retries and rate limiting."""
        self._rl.acquire("fred")
        return self._fred.get_series(series_id, **kwargs)

    @with_retries(RetryShape.vendor_outage_extended)
    def get_series_info(self, series_id: str) -> pd.Series:
        """Fetch series metadata from FRED with retries and rate limiting."""
        self._rl.acquire("fred")
        return self._fred.get_series_info(series_id)

    @with_retries(RetryShape.vendor_outage_extended)
    def get_series_all_releases(self, series_id: str, **kwargs: object) -> object:
        """
        Fetch all vintages (first release + revisions) for a series.

        Returns a DataFrame with columns: ``date``, ``realtime_start``, ``value``.
        """
        self._rl.acquire("fred")
        return self._fred.get_series_all_releases(series_id, **kwargs)
