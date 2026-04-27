"""
Tests for src/alphamind/data_sources/sec_edgar/client.py — story 05h.

All HTTP calls are mocked via httpx.MockTransport / respx or unittest.mock.
No real network calls are made.
"""

from __future__ import annotations

import httpx
import pytest

from alphamind.data_sources.sec_edgar.client import SecEdgarClient

# ---------------------------------------------------------------------------
# AC: User-Agent header sent on every request
# ---------------------------------------------------------------------------


class TestUserAgentHeader:
    def test_get_sends_user_agent_header(self) -> None:
        """Every GET request must include the configured User-Agent header."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(
            user_agent="AlphaMind ops@example.com",
            _transport=transport,
        )
        client.get("https://efts.sec.gov/LATEST/search-index?q=test")

        assert len(captured) == 1
        assert captured[0].headers["user-agent"] == "AlphaMind ops@example.com"

    def test_user_agent_present_on_second_request(self) -> None:
        """User-Agent persists across multiple requests."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(
            user_agent="AlphaMind ops@example.com",
            _transport=transport,
        )
        client.get("https://efts.sec.gov/LATEST/search-index?q=a")
        client.get("https://efts.sec.gov/LATEST/search-index?q=b")

        assert len(captured) == 2
        for req in captured:
            assert req.headers["user-agent"] == "AlphaMind ops@example.com"


# ---------------------------------------------------------------------------
# AC: verify_connectivity() succeeds / fails correctly
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_200(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(user_agent="AlphaMind t@e.com", _transport=transport)
        assert client.verify_connectivity() is True

    def test_raises_on_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(user_agent="AlphaMind t@e.com", _transport=transport)
        with pytest.raises(httpx.ConnectError):
            client.verify_connectivity()

    def test_raises_on_500(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(user_agent="AlphaMind t@e.com", _transport=transport)
        with pytest.raises(httpx.HTTPStatusError):
            client.verify_connectivity()


# ---------------------------------------------------------------------------
# AC: with_retries(important) — retries on 429 / 5xx, not on 4xx
# ---------------------------------------------------------------------------


class TestRetryBehaviour:
    def test_retries_on_429_then_succeeds(self) -> None:
        """Client retries once on 429, then succeeds on second attempt."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, text="ok")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(
            user_agent="AlphaMind t@e.com",
            _transport=transport,
            _sleep=lambda _: None,  # no actual sleep in tests
        )
        resp = client.get("https://efts.sec.gov/LATEST/search-index?q=test")
        assert resp.status_code == 200
        assert call_count == 2

    def test_does_not_retry_on_404(self) -> None:
        """404 is not retryable — should raise immediately."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(404, text="not found")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(
            user_agent="AlphaMind t@e.com",
            _transport=transport,
            _sleep=lambda _: None,
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://efts.sec.gov/LATEST/search-index?q=test")
        assert call_count == 1

    def test_exhausts_retries_and_raises(self) -> None:
        """After max attempts, raises the last exception."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(503, text="unavailable")

        transport = httpx.MockTransport(handler)
        client = SecEdgarClient(
            user_agent="AlphaMind t@e.com",
            _transport=transport,
            _sleep=lambda _: None,
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.get("https://efts.sec.gov/LATEST/search-index?q=test")
        # important shape = 2 attempts
        assert call_count == 2
