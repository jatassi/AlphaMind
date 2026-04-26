"""
Kalshi REST API client.

Handles session-token authentication:
- POST /trade-api/v2/login with email + password to obtain a token.
- Token is cached in memory and expires after 30 minutes.
- Proactively refreshes when within 5 minutes of expiry.
- Transparently re-authenticates on 401 responses.

Integrates:
- :func:`~alphamind.data_sources._common.with_retries` (optional tier)
- :class:`~alphamind.data_sources._common.RateLimiter` (60 req/min)
"""

from __future__ import annotations

import logging
import time

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

logger = logging.getLogger(__name__)

_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes per Kalshi docs
_PROACTIVE_REFRESH_SECONDS = 5 * 60  # refresh 5 minutes before expiry

_rate_limiter = RateLimiter()
_rate_limiter.set_limit("kalshi", rate_per_minute=60)


class KalshiClient:
    """
    HTTP wrapper around Kalshi's REST API.

    Parameters
    ----------
    email:
        Kalshi account email (``KALSHI_EMAIL`` env var in production).
    password:
        Kalshi account password (``KALSHI_PASSWORD`` env var in production).
    rate_limiter:
        Injectable rate limiter for testing.  Defaults to the module-level
        shared instance.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._http = httpx.Client(timeout=30.0)
        self._rl = rate_limiter if rate_limiter is not None else _rate_limiter
        self._token: str | None = None
        self._token_issued_at: float | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _login(self) -> None:
        """Authenticate and cache the session token."""
        resp = self._http.post(
            f"{_BASE_URL}/login",
            json={"email": self._email, "password": self._password},
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        self._token_issued_at = time.monotonic()
        logger.debug("Kalshi login successful; token cached.")

    def _token_needs_refresh(self) -> bool:
        """Return True if the token is absent or within the proactive refresh window."""
        if self._token is None or self._token_issued_at is None:
            return True
        elapsed = time.monotonic() - self._token_issued_at
        return elapsed >= (_TOKEN_TTL_SECONDS - _PROACTIVE_REFRESH_SECONDS)

    def _ensure_token(self) -> str:
        """Return a valid token, refreshing if necessary."""
        if self._token_needs_refresh():
            self._login()
        if self._token is None:
            raise RuntimeError("Kalshi login did not return a token.")
        return self._token

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    @with_retries(RetryShape.optional)
    def get(self, path: str, **params: object) -> dict:
        """
        Issue a rate-limited GET to ``path`` relative to the Kalshi base URL.

        Re-authenticates transparently on a single 401 response.
        """
        self._rl.acquire("kalshi")
        token = self._ensure_token()
        try:
            resp = self._http.get(
                f"{_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or None,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                logger.warning("Kalshi 401; re-authenticating.")
                self._token = None
                token = self._ensure_token()
                resp = self._http.get(
                    f"{_BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params or None,
                )
                resp.raise_for_status()
            else:
                raise
        return resp.json()  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    def verify_connectivity(self) -> bool:
        """
        Return *True* if login succeeds; *False* on any error.

        Suitable for startup smoke-testing.
        """
        try:
            self._login()
            return True
        except Exception:
            logger.exception("Kalshi connectivity check failed.")
            return False
