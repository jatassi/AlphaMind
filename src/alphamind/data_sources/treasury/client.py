"""HTTP wrapper around the Treasury Fiscal Data API.

No authentication required — the API is public.
"""

from __future__ import annotations

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

# Default conservative rate limit — API doesn't publish a hard limit
_DEFAULT_RATE_PER_MINUTE = 60

_BASE_URL = "https://api.fiscaldata.treasury.gov"
_CONNECTIVITY_PATH = "/services/api/fiscal_service/v2/accounting/od/auctions_query"

_rate_limiter = RateLimiter()
_rate_limiter.set_limit("treasury", rate_per_minute=_DEFAULT_RATE_PER_MINUTE)


class TreasuryClient:
    """Client for the Treasury Fiscal Data API."""

    def __init__(self, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url

    def verify_connectivity(self) -> bool:
        """Check reachability of the Treasury Fiscal Data API.

        Returns True when the API responds successfully, False otherwise.
        No auth required.
        """
        try:
            response = httpx.get(
                f"{self._base_url}{_CONNECTIVITY_PATH}",
                params={"page[size]": 1},
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        else:
            return True

    @with_retries(RetryShape.critical)
    def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        """Make a GET request to the API with rate limiting and critical-tier retries."""
        _rate_limiter.acquire("treasury")
        response = httpx.get(
            f"{self._base_url}{path}",
            params=params or {},
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]
