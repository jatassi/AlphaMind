"""
Polygon API client wrapper.

Wraps the ``polygon-api-client`` SDK with:
- ``RateLimiter`` (shared singleton, provider key ``"polygon"``)
- ``with_retries`` applied at call sites in per-domain modules
- ``verify_connectivity()`` smoke-test
"""

from __future__ import annotations

import os

from polygon import AuthError, RESTClient

from alphamind.data_sources._common import RateLimiter

# Module-level shared limiter — all PolygonClient instances share it.
_limiter = RateLimiter()
_limiter.set_limit("polygon", rate_per_minute=100)


class PolygonClient:
    """
    Thin wrapper around ``polygon.RESTClient`` with rate limiting.

    Parameters
    ----------
    api_key:
        Polygon API key.  Defaults to the ``POLYGON_API_KEY`` environment
        variable.  Callers that pass ``_client`` in tests never instantiate
        this class directly, so the env var is only needed in production.
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("POLYGON_API_KEY", "")
        self._rest = RESTClient(api_key=key, retries=0)
        self._limiter = _limiter

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """
        Return ``True`` when the API key is valid, ``False`` on auth failure.

        Raises other exceptions so the caller (e.g. bootstrap orchestrator)
        knows something unexpected happened.
        """
        try:
            self._rest.get_market_status()
        except AuthError:
            return False
        else:
            return True

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def acquire_rate_limit(self) -> None:
        """Block until a token is available for the ``"polygon"`` provider."""
        self._limiter.acquire("polygon")

    # ------------------------------------------------------------------
    # Aggregates (OHLCV bars)
    # ------------------------------------------------------------------

    def get_aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_: str,
        to: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 50_000,
    ) -> list[object]:
        """Return aggregate bars list, consuming one rate-limit token."""
        self.acquire_rate_limit()
        return self._rest.get_aggs(  # type: ignore[no-any-return]
            ticker=ticker,
            multiplier=multiplier,
            timespan=timespan,
            from_=from_,
            to=to,
            adjusted=adjusted,
            sort=sort,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Options snapshots
    # ------------------------------------------------------------------

    def list_snapshot_options_chain(self, underlying: str) -> list[object]:
        """Return full options chain snapshot for *underlying*."""
        self.acquire_rate_limit()
        return list(self._rest.list_snapshot_options_chain(underlying_asset=underlying))

    # ------------------------------------------------------------------
    # Corporate actions
    # ------------------------------------------------------------------

    def list_dividends(self, ticker: str, ex_dividend_date_gte: str | None = None) -> list[object]:
        self.acquire_rate_limit()
        kwargs: dict[str, object] = {"ticker": ticker}
        if ex_dividend_date_gte is not None:
            kwargs["ex_dividend_date_gte"] = ex_dividend_date_gte
        return list(self._rest.list_dividends(**kwargs))

    def list_splits(self, ticker: str, execution_date_gte: str | None = None) -> list[object]:
        self.acquire_rate_limit()
        kwargs: dict[str, object] = {"ticker": ticker}
        if execution_date_gte is not None:
            kwargs["execution_date_gte"] = execution_date_gte
        return list(self._rest.list_splits(**kwargs))

    # ------------------------------------------------------------------
    # Reference
    # ------------------------------------------------------------------

    def get_ticker_details(self, ticker: str) -> object:
        self.acquire_rate_limit()
        return self._rest.get_ticker_details(ticker=ticker)
