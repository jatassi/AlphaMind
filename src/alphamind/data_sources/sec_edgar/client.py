"""
HTTP client for SEC EDGAR — story 05h.

SEC requires an identifying ``User-Agent`` header on every request.
Format: ``<application name> <admin email>``

Rate limit: 10 req/sec (600/min). The :class:`RateLimiter` from ``_common``
enforces this; the limit is registered at instantiation time so the limiter
is ready before the first ``get()`` call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

_CONNECTIVITY_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&type=8-K&dateb=&owner=include&count=1&search_text="
)
_PROVIDER = "sec_edgar"
_RATE_LIMIT_PER_MINUTE = 600  # 10/sec


class SecEdgarClient:
    """
    Thin HTTP wrapper around SEC EDGAR.

    Parameters
    ----------
    user_agent:
        Value for the ``User-Agent`` header required by SEC.
        Format: ``AlphaMind <ops_email>``.
    _transport:
        Injectable httpx transport for testing (default: real network).
    _sleep:
        Injectable sleep function for retry back-off (default: :func:`time.sleep`).
    _rate_limiter:
        Injectable :class:`RateLimiter` (default: shared singleton).
    """

    def __init__(
        self,
        user_agent: str,
        *,
        _transport: httpx.BaseTransport | None = None,
        _sleep: Callable[[float], None] = time.sleep,
        _rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._limiter = _rate_limiter or RateLimiter()
        self._limiter.set_limit(_PROVIDER, rate_per_minute=_RATE_LIMIT_PER_MINUTE)

        kwargs: dict[str, Any] = {
            "headers": {"User-Agent": user_agent},
            "follow_redirects": True,
            "timeout": 30.0,
        }
        if _transport is not None:
            kwargs["transport"] = _transport
        self._http = httpx.Client(**kwargs)

        # Build the retry wrapper once; recreating it per-call is wasteful.
        self._retried_get: Callable[..., httpx.Response] = with_retries(
            RetryShape.important, _sleep=_sleep
        )(self._raw_get)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """
        Perform a rate-limited, retried GET request.

        Retries follow the ``important`` shape (2 attempts, exponential
        back-off) for transient errors (429, 5xx, timeouts).
        """
        self._limiter.acquire(_PROVIDER)
        return self._retried_get(url, **kwargs)

    def verify_connectivity(self) -> bool:
        """
        Confirm that SEC EDGAR is reachable.

        Returns
        -------
        bool
            ``True`` on a successful response.

        Raises
        ------
        httpx.HTTPStatusError
            If the server returns an error status.
        httpx.ConnectError
            If a network-level connection failure occurs.
        """
        resp = self._http.get(_CONNECTIVITY_URL)
        resp.raise_for_status()
        return True

    def _raw_get(self, url: str, **kwargs: Any) -> httpx.Response:
        resp = self._http.get(url, **kwargs)
        resp.raise_for_status()
        return resp
