"""
HTTP wrapper for the Marketaux REST API.

Uses httpx (no official SDK).  Integrates with_retries(important), RateLimiter,
and exposes verify_connectivity().

Marketaux free-tier cap: 100 requests per day.
Rate-limit config: ~0.07 req/min (4/hour) keeps within the daily cap across
the 30-min cron cadence (48 fires/day x up to 2 requests/fire = 96 req/day).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.marketaux.com"
_PROVIDER = "marketaux"


class MarketauxClient:
    """
    Thin HTTP client for the Marketaux /v1/news/all endpoint.

    Parameters
    ----------
    api_key:
        Marketaux API token.
    rate_limit_per_minute:
        Tokens per minute for the shared RateLimiter bucket.  Default matches
        the 100 req/day budget spread across 24 hours (≈ 0.07/min).
    """

    def __init__(
        self,
        api_key: str,
        rate_limit_per_minute: float = 1,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(base_url=_BASE_URL, timeout=30.0)
        self._rate_limiter = RateLimiter()
        # Floor to 1 so the bucket enforces spacing even at sub-minute rates (e.g. 0.07/min).
        rpm_int = max(1, int(rate_limit_per_minute))
        self._rate_limiter.set_limit(_PROVIDER, rate_per_minute=rpm_int)

    def verify_connectivity(self) -> bool:
        """
        Probe the API with a minimal request.

        Returns True when the API key is valid (HTTP 200), False otherwise.
        Does not raise.
        """
        try:
            resp = self._http.get(
                "/v1/news/all",
                params={"api_token": self._api_key, "limit": 1},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # ``HTTPError`` covers ``HTTPStatusError`` (401/403 auth, 4xx/5xx),
            # ``TimeoutException``, and ``RequestError`` (connect / network).
            # Other exception types (TypeError, AttributeError) surface
            # naturally so misconfiguration is not hidden as connectivity loss.
            logger.warning("Marketaux connectivity check failed.", exc_info=exc)
            return False
        else:
            return True

    def get_news(
        self,
        symbols: list[str] | None,
        *,
        countries: str | None = None,
        published_after: str | None = None,
        limit: int = 3,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """
        Fetch news articles from /v1/news/all.

        Parameters
        ----------
        symbols:
            Comma-joined ticker list passed as ``symbols=`` param.  Pass
            ``None`` for a market-wide query (use ``countries`` instead).
        countries:
            ISO country code filter (e.g. ``"us"``).
        published_after:
            ISO 8601 datetime; filters articles published after this time.
        limit:
            Page size (max 3 on free tier).
        page:
            Page number.

        Returns
        -------
        list[dict]
            The ``data`` array from the Marketaux response.
        """
        self._rate_limiter.acquire(_PROVIDER)
        return self._fetch_news(
            symbols=symbols,
            countries=countries,
            published_after=published_after,
            limit=limit,
            page=page,
        )

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def _fetch_news(
        self,
        *,
        symbols: list[str] | None,
        countries: str | None,
        published_after: str | None,
        limit: int,
        page: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "api_token": self._api_key,
            "filter_entities": "true",
            "limit": limit,
            "page": page,
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if countries:
            params["countries"] = countries
        if published_after:
            params["published_after"] = published_after

        resp = self._http.get("/v1/news/all", params=params)
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json().get("data", [])
        return data


# Runtime contract: MarketauxClient must structurally implement MarketauxAPI.
from alphamind.data_sources.marketaux._protocol import MarketauxAPI  # noqa: E402

_: MarketauxAPI = MarketauxClient(api_key="<unused-for-typecheck>")
