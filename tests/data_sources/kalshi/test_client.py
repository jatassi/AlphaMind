"""
Tests for kalshi/client.py.

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alphamind.data_sources.kalshi.client import KalshiClient

BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
LOGIN_URL = f"{BASE_URL}/login"


def _mock_login_response(token: str = "tok-abc") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"token": token}
    resp.raise_for_status.return_value = None
    return resp


def _mock_get_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Login and token caching
# ---------------------------------------------------------------------------


class TestLoginCaching:
    def test_login_called_on_first_request(self) -> None:
        """Client performs POST /login on first request and caches the token."""
        client = KalshiClient(email="user@example.com", password="secret")
        with patch.object(client._http, "post", return_value=_mock_login_response()) as mock_post, \
             patch.object(client._http, "get", return_value=_mock_get_response({"events": []})):
            client.get("/events")
        mock_post.assert_called_once()
        assert LOGIN_URL in mock_post.call_args[0][0]

    def test_login_not_repeated_on_second_request(self) -> None:
        """Token is cached; second request does not trigger another login."""
        client = KalshiClient(email="user@example.com", password="secret")
        with patch.object(client._http, "post", return_value=_mock_login_response()) as mock_post, \
             patch.object(client._http, "get", return_value=_mock_get_response({"events": []})):
            client.get("/events")
            client.get("/events")
        mock_post.assert_called_once()

    def test_auth_header_sent_after_login(self) -> None:
        """Requests carry the Authorization header after login."""
        client = KalshiClient(email="user@example.com", password="secret")
        with patch.object(client._http, "post", return_value=_mock_login_response("tok-xyz")), \
             patch.object(client._http, "get", return_value=_mock_get_response({"events": []})) as mock_get:
            client.get("/events")
        headers = mock_get.call_args[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer tok-xyz"


# ---------------------------------------------------------------------------
# 401 transparent re-auth
# ---------------------------------------------------------------------------


class TestRefreshOn401:
    def test_reauthenticates_on_401_and_retries(self) -> None:
        """On 401, client re-logins and retries the original request."""
        client = KalshiClient(email="user@example.com", password="secret")

        login_responses = [_mock_login_response("tok-1"), _mock_login_response("tok-2")]
        login_iter = iter(login_responses)

        request = httpx.Request("GET", f"{BASE_URL}/events")
        unauthorized = httpx.Response(401, request=request)

        call_count = 0

        def fake_get(url, *, headers=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.HTTPStatusError("401", request=request, response=unauthorized)
            return _mock_get_response({"events": []})

        with patch.object(client._http, "post", side_effect=lambda *a, **kw: next(login_iter)), \
             patch.object(client._http, "get", side_effect=fake_get):
            result = client.get("/events")

        assert result == {"events": []}

    def test_second_login_token_used_after_401(self) -> None:
        """After 401 re-auth, the retry carries the new token."""
        client = KalshiClient(email="user@example.com", password="secret")

        login_responses = [_mock_login_response("old-tok"), _mock_login_response("new-tok")]
        login_iter = iter(login_responses)

        request = httpx.Request("GET", f"{BASE_URL}/events")
        unauthorized = httpx.Response(401, request=request)

        call_count = 0
        captured_headers: list[dict] = []

        def fake_get(url, *, headers=None, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_headers.append(headers or {})
            if call_count == 1:
                raise httpx.HTTPStatusError("401", request=request, response=unauthorized)
            return _mock_get_response({"events": []})

        with patch.object(client._http, "post", side_effect=lambda *a, **kw: next(login_iter)), \
             patch.object(client._http, "get", side_effect=fake_get):
            client.get("/events")

        assert captured_headers[1].get("Authorization") == "Bearer new-tok"


# ---------------------------------------------------------------------------
# Proactive token refresh
# ---------------------------------------------------------------------------


class TestProactiveRefresh:
    def test_refreshes_proactively_when_near_expiry(self) -> None:
        """Token is refreshed before expiry when within the proactive window."""
        import time

        client = KalshiClient(email="user@example.com", password="secret")

        # Pre-seed the token as nearly expired (issued 26 minutes ago → 4 min left < 5 min window)
        client._token = "old-tok"
        client._token_issued_at = time.monotonic() - (26 * 60)

        with patch.object(client._http, "post", return_value=_mock_login_response("fresh-tok")) as mock_post, \
             patch.object(client._http, "get", return_value=_mock_get_response({"events": []})) as mock_get:
            client.get("/events")

        mock_post.assert_called_once()
        headers = mock_get.call_args[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer fresh-tok"


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_successful_login(self) -> None:
        client = KalshiClient(email="user@example.com", password="secret")
        with patch.object(client._http, "post", return_value=_mock_login_response()):
            assert client.verify_connectivity() is True

    def test_returns_false_when_login_fails(self) -> None:
        client = KalshiClient(email="user@example.com", password="secret")
        request = httpx.Request("POST", LOGIN_URL)
        response = httpx.Response(401, request=request)
        with patch.object(client._http, "post") as mock_post:
            mock_post.side_effect = httpx.HTTPStatusError(
                "401 Unauthorized", request=request, response=response
            )
            assert client.verify_connectivity() is False
