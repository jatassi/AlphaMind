"""
Finnhub client wrapper.

Wraps the ``finnhub-python`` SDK with RateLimiter integration and a
``verify_connectivity()`` health check.
"""

from __future__ import annotations

import finnhub

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

_PROVIDER = "finnhub"


class FinnhubClient:
    """Thin wrapper around :class:`finnhub.Client` with rate limiting."""

    def __init__(
        self,
        api_key: str,
        *,
        _rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._sdk = finnhub.Client(api_key=api_key)
        self._limiter = _rate_limiter

    @property
    def sdk(self) -> finnhub.Client:
        return self._sdk

    def _acquire(self) -> None:
        if self._limiter is not None:
            self._limiter.acquire(_PROVIDER)

    @with_retries(RetryShape.important, _sleep=lambda _: None)
    def verify_connectivity(self) -> None:
        """Call market_status to confirm the API key is valid."""
        self._acquire()
        self._sdk.market_status(exchange="US")
