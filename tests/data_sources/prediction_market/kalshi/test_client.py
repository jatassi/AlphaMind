"""
Tests for kalshi/client.py.

HTTP is driven through httpx.MockTransport — no real network traffic.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from alphamind.data_sources.prediction_market.kalshi.client import KalshiClient

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> KalshiClient:
    client = KalshiClient()
    client._http = httpx.Client(
        timeout=30.0,
        transport=httpx.MockTransport(handler),
    )
    return client


# ---------------------------------------------------------------------------
# Read-only GET behavior
# ---------------------------------------------------------------------------


class TestReadOnlyAccess:
    def test_get_hits_new_base_url(self) -> None:
        """Client targets api.elections.kalshi.com (post-2024 migration)."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"events": []})

        client = _client_with(handler)
        client.get("/events")
        assert str(captured[0].url).startswith(f"{BASE_URL}/events")

    def test_get_does_not_send_authorization_header(self) -> None:
        """Read-only collector path issues unauthenticated GETs."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"events": []})

        client = _client_with(handler)
        client.get("/events")
        assert "authorization" not in {k.lower() for k in captured[0].headers}

    def test_get_propagates_params(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"markets": []})

        client = _client_with(handler)
        client.get("/markets", series_ticker="KXFED")
        assert captured[0].url.params.get("series_ticker") == "KXFED"


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_reachable_status_endpoint(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

        client = _client_with(handler)
        assert client.verify_connectivity() is True

    def test_returns_false_when_status_endpoint_errors(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = _client_with(handler)
        assert client.verify_connectivity() is False


class TestProtocolContract:
    def test_kalshi_client_implements_kalshi_api(self) -> None:
        from alphamind.data_sources.prediction_market.kalshi._protocol import KalshiAPI

        client: KalshiAPI = KalshiClient()
        assert hasattr(client, "get")
        assert hasattr(client, "verify_connectivity")
