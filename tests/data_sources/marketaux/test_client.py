"""
Tests for src/alphamind/data_sources/marketaux/client.py

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from alphamind.data_sources.marketaux.client import MarketauxClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(api_key: str = "test-key") -> MarketauxClient:
    return MarketauxClient(api_key=api_key, rate_limit_per_minute=1)


def _ok_response(data: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# AC: verify_connectivity returns True on 200
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_success(self) -> None:
        client = _make_client()
        payload = {"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}}
        with patch.object(client._http, "get", return_value=_ok_response(payload)):
            assert client.verify_connectivity() is True

    def test_returns_false_on_auth_error(self) -> None:
        client = _make_client()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=resp
        )
        with patch.object(client._http, "get", return_value=resp):
            assert client.verify_connectivity() is False


# ---------------------------------------------------------------------------
# AC: get_news passes api_token and returns parsed JSON
# ---------------------------------------------------------------------------


class TestGetNews:
    def test_returns_articles(self) -> None:
        client = _make_client()
        payload = {
            "data": [
                {
                    "uuid": "abc",
                    "title": "Test headline",
                    "description": "body text",
                    "url": "https://example.com/article",
                    "published_at": "2024-01-15T10:00:00.000000Z",
                    "source": "Reuters",
                    "language": "en",
                    "entities": [
                        {
                            "symbol": "AAPL",
                            "sentiment_score": 0.42,
                        }
                    ],
                    "topics": [],
                }
            ],
            "meta": {"found": 1, "returned": 1, "limit": 3, "page": 1},
        }
        with patch.object(client._http, "get", return_value=_ok_response(payload)) as mock_get:
            articles = client.get_news(symbols=["AAPL"])

        assert len(articles) == 1
        assert articles[0]["uuid"] == "abc"
        # api_token must be passed as a query param
        call_kwargs = mock_get.call_args
        params = call_kwargs[1].get("params", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert params.get("api_token") == "test-key"

    def test_get_news_market_wide_omits_symbols(self) -> None:
        client = _make_client()
        payload = {"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}}
        with patch.object(client._http, "get", return_value=_ok_response(payload)) as mock_get:
            client.get_news(symbols=None, countries="us")

        params = mock_get.call_args[1].get("params", {})
        assert "symbols" not in params
        assert params.get("countries") == "us"


# ---------------------------------------------------------------------------
# AC: rate limiter is applied (acquire called before HTTP)
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_rate_limiter_acquire_called(self) -> None:
        client = _make_client()
        payload = {"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}}
        with (
            patch.object(client._http, "get", return_value=_ok_response(payload)),
            patch.object(client._rate_limiter, "acquire") as mock_acquire,
        ):
            client.get_news(symbols=["AAPL"])

        mock_acquire.assert_called_once_with("marketaux")
