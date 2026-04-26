"""
HTTP wrapper around the BLS v2 API (POST-based; no official SDK).

BLS v2 batch limits:
- With API key:    up to 50 series IDs per request
- Without API key: up to 25 series IDs per request

Rate-limit per BLS documentation: 500 queries/day with key.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

_BLS_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
_BATCH_SIZE = 50

# Module-level shared rate limiter; callers may override via dependency injection.
_rate_limiter = RateLimiter()
_rate_limiter.set_limit("bls", rate_per_minute=10)


class BLSClient:
    """
    Thin wrapper around the BLS v2 timeseries endpoint.

    Parameters
    ----------
    api_key:
        BLS v2 registration key.
    rate_limiter:
        Optional :class:`RateLimiter` override; defaults to the module-level
        instance with a 10/min limit.
    """

    def __init__(
        self,
        api_key: str,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._rl = rate_limiter if rate_limiter is not None else _rate_limiter
        self._http = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_timeseries(
        self,
        series_ids: list[str],
        *,
        start_year: str,
        end_year: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch one or more series from the BLS v2 timeseries endpoint.

        Automatically batches requests when ``len(series_ids) > 50``.

        Parameters
        ----------
        series_ids:
            BLS series identifiers, e.g. ``["CES0000000001", "LNS14000000"]``.
        start_year:
            Four-digit start year string, e.g. ``"2024"``.
        end_year:
            Four-digit end year string, e.g. ``"2024"``.

        Returns
        -------
        list[dict]
            Flat list of BLS series result objects (each has ``"seriesID"``
            and ``"data"``).
        """
        results: list[dict[str, Any]] = []
        for i in range(math.ceil(len(series_ids) / _BATCH_SIZE)):
            batch = series_ids[i * _BATCH_SIZE : (i + 1) * _BATCH_SIZE]
            results.extend(self._post_batch(batch, start_year=start_year, end_year=end_year))
        return results

    def verify_connectivity(self) -> bool:
        """
        Verify that the API key works by posting a minimal probe request.

        Returns
        -------
        bool
            ``True`` when the API key is accepted.

        Raises
        ------
        httpx.HTTPStatusError
            On HTTP-level error (4xx / 5xx).
        RuntimeError
            When BLS returns HTTP 200 but ``status != "REQUEST_SUCCEEDED"``
            (e.g., invalid API key).
        """
        self._post_batch(["LNS14000000"], start_year="2024", end_year="2024")
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_batch(
        self,
        series_ids: list[str],
        *,
        start_year: str,
        end_year: str,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "seriesid": series_ids,
            "startyear": start_year,
            "endyear": end_year,
            "registrationkey": self._api_key,
        }
        self._rl.acquire("bls")

        @with_retries(RetryShape.critical)
        def _do_post() -> httpx.Response:
            return self._http.post(_BLS_ENDPOINT, json=payload)

        response = _do_post()
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "REQUEST_SUCCEEDED":
            messages = body.get("message", [])
            raise RuntimeError(f"BLS API error: {messages}")
        return body["Results"]["series"]
