"""Tests for the Treasury Fiscal Data API client."""

from __future__ import annotations

from unittest.mock import patch

import httpx


class TestVerifyConnectivity:
    """verify_connectivity() hits the endpoint and confirms reachability."""

    def test_returns_true_on_success(self) -> None:
        """verify_connectivity returns True when the API responds 200."""
        from alphamind.data_sources.treasury.client import TreasuryClient

        request = httpx.Request("GET", "https://api.fiscaldata.treasury.gov/")
        response = httpx.Response(200, request=request)

        with patch("alphamind.data_sources.treasury.client.httpx.get", return_value=response):
            client = TreasuryClient()
            assert client.verify_connectivity() is True

    def test_returns_false_on_network_error(self) -> None:
        """verify_connectivity returns False when network is unreachable."""
        from alphamind.data_sources.treasury.client import TreasuryClient

        with patch(
            "alphamind.data_sources.treasury.client.httpx.get",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            client = TreasuryClient()
            assert client.verify_connectivity() is False

    def test_returns_false_on_http_error(self) -> None:
        """verify_connectivity returns False on non-2xx response."""
        from alphamind.data_sources.treasury.client import TreasuryClient

        request = httpx.Request("GET", "https://api.fiscaldata.treasury.gov/")
        response = httpx.Response(500, request=request)

        with patch("alphamind.data_sources.treasury.client.httpx.get", return_value=response):
            client = TreasuryClient()
            assert client.verify_connectivity() is False
