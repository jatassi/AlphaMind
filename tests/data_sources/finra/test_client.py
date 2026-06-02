"""Tests for finra/client.py — HTTP layer driven through httpx.MockTransport."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    """Construct a FinraClient whose underlying httpx.Client uses *handler*."""
    from alphamind.data_sources.finra.client import FinraClient

    client = FinraClient()
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=60.0,
        follow_redirects=True,
    )
    return client


class TestUserAgentHeader:
    def test_get_sends_user_agent_header(self) -> None:
        """Every GET to the FINRA CDN includes a User-Agent header."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200, text="Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            )

        client = _client_with(handler)
        client.get("/equity/regsho/daily/CNMSshvol20260124.txt")

        assert len(captured) == 1
        ua = captured[0].headers.get("user-agent", "")
        assert "AlphaMind" in ua

    def test_get_returns_text(self) -> None:
        """get() returns the response text."""
        content = (
            "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20260124|AAPL|100|0|200|CNMS\n"
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=content)

        client = _client_with(handler)
        result = client.get("/equity/regsho/daily/CNMSshvol20260124.txt")
        assert result == content


class TestVerifyConnectivity:
    def test_returns_true_on_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = _client_with(handler)
        assert client.verify_connectivity() is True

    def test_returns_false_on_http_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = _client_with(handler)
        assert client.verify_connectivity() is False

    def test_returns_false_on_connection_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = _client_with(handler)
        assert client.verify_connectivity() is False


class TestNotFoundHandling:
    def test_get_raises_http_status_error_on_404(self) -> None:
        """get() propagates HTTPStatusError so callers can detect 404."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = _client_with(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.get("/equity/regsho/daily/CNMSshvol20260101.txt")
