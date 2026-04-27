"""
HTTP wrapper around FINRA's public CDN.

No API key required — FINRA CDN files are public.  Every request carries a
``User-Agent`` header identifying the application, per FINRA's fair-use policy.

Integrates:
- :func:`~alphamind.data_sources._common.with_retries` (important tier)
- :class:`~alphamind.data_sources._common.RateLimiter` (30 req/min conservative)
"""

from __future__ import annotations

import logging

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

logger = logging.getLogger(__name__)

_BASE_URL = "https://cdn.finra.org"
_DEFAULT_USER_AGENT = "AlphaMind/1.0 (data-collector; contact: ops@alphamind.internal)"
# Probe URL: directory listing of the daily short volume folder (HEAD only).
_CONNECTIVITY_PROBE = "/equity/regsho/daily/"

_rate_limiter = RateLimiter()
_rate_limiter.set_limit("finra", rate_per_minute=30)


class FinraClient:
    """
    HTTP wrapper for the FINRA public CDN.

    Parameters
    ----------
    user_agent:
        ``User-Agent`` header value.  Defaults to a sensible AlphaMind string.
    rate_limiter:
        Injectable :class:`~alphamind.data_sources._common.RateLimiter`.
        Defaults to the module-level shared instance.
    """

    def __init__(
        self,
        *,
        user_agent: str = _DEFAULT_USER_AGENT,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._rl = rate_limiter if rate_limiter is not None else _rate_limiter
        self._http = httpx.Client(timeout=60.0, follow_redirects=True)

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    @with_retries(RetryShape.important)
    def get(self, path: str) -> str:
        """
        Fetch *path* (relative to the FINRA CDN base URL) and return the
        response body as a text string.

        Raises :class:`httpx.HTTPStatusError` on non-2xx responses (including
        404) so callers can distinguish "file not yet published" from
        unexpected errors.
        """
        self._rl.acquire("finra")
        resp = self._http.get(
            f"{_BASE_URL}{path}",
            headers={"User-Agent": self._user_agent},
        )
        resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """
        Return *True* when the FINRA CDN responds to a HEAD request.

        Issues a HEAD (not GET) so it does not download an actual file during
        the connectivity check.
        """
        try:
            resp = self._http.head(
                f"{_BASE_URL}{_CONNECTIVITY_PROBE}",
                headers={"User-Agent": self._user_agent},
            )
            resp.raise_for_status()
        except Exception:
            logger.exception("FINRA CDN connectivity check failed.")
            return False
        else:
            return True
