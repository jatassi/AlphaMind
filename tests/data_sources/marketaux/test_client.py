"""
Tests for src/alphamind/data_sources/marketaux/client.py

All HTTP calls are driven through httpx.MockTransport — no real network traffic.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from alphamind.data_sources.marketaux.client import MarketauxClient


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    api_key: str = "test-key",
) -> MarketauxClient:
    client = MarketauxClient(api_key=api_key, rate_limit_per_minute=1)
    client._http = httpx.Client(
        base_url="https://api.marketaux.com",
        timeout=30.0,
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# AC: verify_connectivity returns True on 200
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}},
            )

        client = _make_client(handler)
        assert client.verify_connectivity() is True

    def test_returns_false_on_auth_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})

        client = _make_client(handler)
        assert client.verify_connectivity() is False


# ---------------------------------------------------------------------------
# AC: get_news passes api_token and returns parsed JSON
# ---------------------------------------------------------------------------


class TestGetNews:
    def test_returns_articles(self) -> None:
        captured_requests: list[httpx.Request] = []
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

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json=payload)

        client = _make_client(handler)
        articles = client.get_news(symbols=["AAPL"])

        assert len(articles) == 1
        assert articles[0]["uuid"] == "abc"
        # api_token must be passed as a query param
        assert captured_requests[0].url.params.get("api_token") == "test-key"

    def test_get_news_market_wide_omits_symbols(self) -> None:
        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                200,
                json={"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}},
            )

        client = _make_client(handler)
        client.get_news(symbols=None, countries="us")

        params = captured_requests[0].url.params
        assert "symbols" not in params
        assert params.get("countries") == "us"


# ---------------------------------------------------------------------------
# AC: rate limiter is applied (acquire called before HTTP)
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_rate_limiter_acquire_called(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [], "meta": {"found": 0, "returned": 0, "limit": 3, "page": 1}},
            )

        client = _make_client(handler)

        acquired: list[str] = []
        original = client._rate_limiter.acquire

        def tracking(provider: str) -> None:
            acquired.append(provider)
            original(provider)

        client._rate_limiter.acquire = tracking  # type: ignore[method-assign]
        client.get_news(symbols=["AAPL"])
        assert acquired == ["marketaux"]
