"""
Kalshi REST API client.

Read-only access only. Kalshi's market-data endpoints (``/events``,
``/markets``, ``/exchange/status``) are publicly readable; trading
endpoints require RSA-signature authentication that is out of scope
for this collector. Email + password login was deprecated when the API
moved to ``api.elections.kalshi.com`` in 2024 (the old endpoint now
returns ``API has been moved`` redirects).

Integrates:
- :func:`~alphamind.data_sources._common.with_retries` (optional tier)
- :class:`~alphamind.data_sources._common.RateLimiter` (60 req/min)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

_rate_limiter = RateLimiter()
_rate_limiter.set_limit("kalshi", rate_per_minute=60)


class KalshiClient:
    """
    HTTP wrapper around Kalshi's read-only market-data endpoints.

    Parameters
    ----------
    rate_limiter:
        Injectable rate limiter for testing. Defaults to the module-level
        shared instance.
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._http = httpx.Client(timeout=30.0)
        self._rl = rate_limiter if rate_limiter is not None else _rate_limiter

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    @with_retries(RetryShape.optional)
    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """Issue a rate-limited GET to ``path`` relative to the Kalshi base URL."""
        self._rl.acquire("kalshi")
        resp = self._http.get(f"{_BASE_URL}{path}", params=params or None)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """Return *True* if the exchange status endpoint is reachable."""
        try:
            self.get("/exchange/status")
        except httpx.HTTPError as exc:
            # ``HTTPError`` covers HTTP status errors plus network failures
            # (timeout, refused, DNS). Other exceptions surface naturally
            # so misconfiguration is not hidden as connectivity loss.
            logger.warning("Kalshi connectivity check failed.", exc_info=exc)
            return False
        else:
            return True
