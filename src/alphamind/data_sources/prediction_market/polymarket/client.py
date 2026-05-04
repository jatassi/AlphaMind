"""
Polymarket HTTP client — Gamma API.

No auth required. All endpoints are public read-only. The collector reads
prices and order-book stats directly from Gamma's ``/markets`` response
(``outcomePrices``, ``bestBid``, ``bestAsk``); the CLOB ``/prices``
endpoint requires per-outcome ``token_id`` values rather than the
``conditionId`` Gamma exposes, so that path is intentionally not used.

Gamma API base: https://gamma-api.polymarket.com
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

_GAMMA_BASE = "https://gamma-api.polymarket.com"

_PROVIDER = "polymarket"


class PolymarketClient:
    """
    Thin HTTP wrapper for the Polymarket Gamma and CLOB public APIs.

    Parameters
    ----------
    http_client:
        Injectable httpx.Client or mock (for testing).
    rate_limiter:
        Shared RateLimiter instance.  When *None* an unregistered limiter is
        used (unlimited).
    _sleep:
        Injectable sleep for retry back-off (pass ``lambda s: None`` in tests).
    """

    def __init__(
        self,
        *,
        http_client: Any = None,
        rate_limiter: RateLimiter | None = None,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http_client if http_client is not None else httpx.Client(timeout=30.0)
        self.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()
        # Decorate the raw HTTP helpers once at construction so the retry
        # wrapper is not re-created on every call.
        self._get_with_retry: Callable[..., Any] = with_retries(RetryShape.optional, _sleep=_sleep)(
            self._raw_get
        )

    def _raw_get(self, url: str, **kwargs: Any) -> Any:
        """Issue a plain GET; decorated by _get_with_retry at construction."""
        return self._http.get(url, **kwargs)

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """
        Return True when the Gamma /markets endpoint is reachable.

        Never raises — returns False on any exception.
        """
        try:
            self.rate_limiter.acquire(_PROVIDER)
            resp = self._http.get(f"{_GAMMA_BASE}/markets", params={"limit": 1})
            resp.raise_for_status()
        except Exception:
            return False
        else:
            return True

    # ------------------------------------------------------------------
    # Gamma API
    # ------------------------------------------------------------------

    def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch markets from Gamma ``/markets``.

        Parameters
        ----------
        limit:
            Page size (max 100 per Gamma API docs).
        offset:
            Pagination offset.
        closed:
            When True fetch only closed markets; False for open only;
            None for all.
        """
        self.rate_limiter.acquire(_PROVIDER)

        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if closed is not None:
            params["closed"] = str(closed).lower()

        resp = self._get_with_retry(f"{_GAMMA_BASE}/markets", params=params)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def get_events_by_ids(self, event_ids: list[str]) -> list[dict[str, Any]]:
        """
        Fetch events from Gamma ``/events`` by id.

        ``/events`` accepts repeated ``id=`` query params and returns the
        matching event objects.  Used by the categorizer to read each event's
        ``tags[].label`` after the per-market loop has already collected
        snapshot fields from ``/markets``.

        Returns the concatenated event list across all requested ids.
        Empty input returns an empty list without issuing a request.
        """
        if not event_ids:
            return []

        results: list[dict[str, Any]] = []
        # Gamma /events caps the request size at the same ~100 rows per page
        # as /markets; chunk the id list to stay under any URL-length limit.
        chunk_size = 100
        for start in range(0, len(event_ids), chunk_size):
            chunk = event_ids[start : start + chunk_size]
            self.rate_limiter.acquire(_PROVIDER)
            resp = self._get_with_retry(
                f"{_GAMMA_BASE}/events",
                params=[("id", eid) for eid in chunk],
            )
            resp.raise_for_status()
            page: list[dict[str, Any]] = resp.json()
            results.extend(page)
        return results
