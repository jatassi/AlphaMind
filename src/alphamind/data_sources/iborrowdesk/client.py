"""
iBorrowDesk HTTP client.

Wraps ``https://www.iborrowdesk.com/api/ticker/{TICKER}``.

No authentication required. Key behaviors:
- Sends a browser-like User-Agent header (nginx/iBorrowDesk silently drops
  non-browser agents).
- Follows the apex→www 301 redirect automatically.
- Distinguishes three response classes:
    - HTTP 200  → parse and return JSON payload.
    - HTTP 404 with ``{"errors": [{"code": "not_found", ...}]}`` body →
      raise ``IBorrowDeskCoverageError`` (ticker not in coverage; not retryable).
    - HTTP 444 or TCP empty-reply (``RemoteProtocolError``) →
      raise ``IBorrowDeskBlockedError`` (heavy back-off required; caller stops
      the current sweep).
- Integrates ``RateLimiter`` keyed on ``"iborrowdesk"``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter

logger = logging.getLogger(__name__)

_BASE = "https://www.iborrowdesk.com/api/ticker"
_PROVIDER = "iborrowdesk"

# A browser-like User-Agent is required — the server silently drops requests
# that look like bots or scripts.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class IBorrowDeskCoverageError(Exception):
    """Raised when iBorrowDesk returns HTTP 404 with a not_found error body.

    The ticker is not in iBorrowDesk's coverage universe. Not retryable.
    """


class IBorrowDeskBlockedError(Exception):
    """Raised when iBorrowDesk returns HTTP 444 or closes the TCP connection
    without a response (empty-reply block).

    Sustained requests will deepen the block. Caller must halt the current
    sweep and wait for the next scheduled cron fire.
    """


class IBorrowDeskClient:
    """
    Thin HTTP wrapper for the iBorrowDesk per-ticker JSON endpoint.

    Parameters
    ----------
    http_client:
        Injectable httpx.Client or mock (for testing).
    rate_limiter:
        Shared ``RateLimiter`` instance.  When *None* an unregistered limiter
        is used (unlimited).
    """

    follow_redirects: bool = True

    def __init__(
        self,
        *,
        http_client: Any = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.Client(
                timeout=30.0,
                follow_redirects=True,
            )
        self.rate_limiter = rate_limiter if rate_limiter is not None else RateLimiter()

    def fetch_ticker(self, ticker: str) -> dict[str, Any]:
        """
        Fetch borrow cost data for *ticker*.

        Returns
        -------
        dict
            Parsed JSON payload from iBorrowDesk.

        Raises
        ------
        IBorrowDeskCoverageError
            HTTP 404 with ``{"errors": [{"code": "not_found", ...}]}``.
        IBorrowDeskBlockedError
            HTTP 444 or TCP empty-reply.
        httpx.HTTPStatusError
            Any other non-2xx HTTP status.
        """
        self.rate_limiter.acquire(_PROVIDER)

        headers = {"User-Agent": _USER_AGENT}

        try:
            resp = self._http.get(f"{_BASE}/{ticker}", headers=headers)
        except httpx.RemoteProtocolError as exc:
            raise IBorrowDeskBlockedError(
                f"TCP empty-reply from iBorrowDesk for {ticker}: {exc}"
            ) from exc

        if resp.status_code == 444:
            raise IBorrowDeskBlockedError(f"HTTP 444 from iBorrowDesk for {ticker} — block active")

        if resp.status_code == 404:
            try:
                body = resp.json()
            except (json.JSONDecodeError, ValueError):
                # Malformed JSON body on 404 — treat as no error payload and
                # fall through to ``raise_for_status``. ``ValueError`` covers
                # httpx's older builds; ``JSONDecodeError`` is the modern type.
                body = {}
            errors = body.get("errors", [])
            if any(e.get("code") == "not_found" for e in errors):
                raise IBorrowDeskCoverageError(f"Ticker {ticker!r} not in iBorrowDesk coverage")
            resp.raise_for_status()

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def verify_connectivity(self) -> bool:
        """
        Probe iBorrowDesk with a known-coverage ticker (AAPL).

        Returns True on HTTP 200, False on any exception.
        """
        try:
            self.fetch_ticker("AAPL")
        except (
            httpx.HTTPError,
            IBorrowDeskCoverageError,
            IBorrowDeskBlockedError,
        ) as exc:
            # ``HTTPError`` covers HTTP status errors + network failures.
            # The two domain errors signal coverage / block conditions that
            # mean the endpoint is unreachable for our probe. Other exceptions
            # surface naturally so misconfiguration isn't hidden.
            logger.warning("iBorrowDesk connectivity check failed.", exc_info=exc)
            return False
        return True
