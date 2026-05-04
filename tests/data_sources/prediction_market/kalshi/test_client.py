"""
Tests for kalshi/client.py.

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from alphamind.data_sources.prediction_market.kalshi.client import KalshiClient

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _mock_get_response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Read-only GET behavior
# ---------------------------------------------------------------------------


class TestReadOnlyAccess:
    def test_get_hits_new_base_url(self) -> None:
        """Client targets api.elections.kalshi.com (post-2024 migration)."""
        client = KalshiClient()
        with patch.object(
            client._http, "get", return_value=_mock_get_response({"events": []})
        ) as mock_get:
            client.get("/events")
        url = mock_get.call_args[0][0]
        assert url == f"{BASE_URL}/events"

    def test_get_does_not_send_authorization_header(self) -> None:
        """Read-only collector path issues unauthenticated GETs."""
        client = KalshiClient()
        with patch.object(
            client._http, "get", return_value=_mock_get_response({"events": []})
        ) as mock_get:
            client.get("/events")
        # The headers kwarg is either omitted or has no Authorization entry
        headers = mock_get.call_args.kwargs.get("headers") or {}
        assert "Authorization" not in headers

    def test_get_propagates_params(self) -> None:
        client = KalshiClient()
        with patch.object(
            client._http, "get", return_value=_mock_get_response({"markets": []})
        ) as mock_get:
            client.get("/markets", series_ticker="KXFED")
        params = mock_get.call_args.kwargs.get("params")
        assert params == {"series_ticker": "KXFED"}


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_on_reachable_status_endpoint(self) -> None:
        client = KalshiClient()
        with patch.object(
            client._http,
            "get",
            return_value=_mock_get_response({"exchange_active": True, "trading_active": True}),
        ):
            assert client.verify_connectivity() is True

    def test_returns_false_when_status_endpoint_errors(self) -> None:
        client = KalshiClient()
        request = httpx.Request("GET", f"{BASE_URL}/exchange/status")
        response = httpx.Response(503, request=request)
        with patch.object(client._http, "get") as mock_get:
            mock_get.side_effect = httpx.HTTPStatusError(
                "503 Service Unavailable", request=request, response=response
            )
            assert client.verify_connectivity() is False
