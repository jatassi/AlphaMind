"""Tests for the Treasury Fiscal Data API client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from alphamind.data_sources.treasury.client import TreasuryClient


class TestVerifyConnectivity:
    """verify_connectivity() hits the endpoint and confirms reachability."""

    def test_returns_true_on_success(self) -> None:
        """verify_connectivity returns True when the API responds 200."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("alphamind.data_sources.treasury.client.httpx.get", return_value=mock_response):
            client = TreasuryClient()
            assert client.verify_connectivity() is True

    def test_returns_false_on_network_error(self) -> None:
        """verify_connectivity returns False when network is unreachable."""
        with patch(
            "alphamind.data_sources.treasury.client.httpx.get",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            client = TreasuryClient()
            assert client.verify_connectivity() is False

    def test_returns_false_on_http_error(self) -> None:
        """verify_connectivity returns False on non-2xx response."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )

        with patch("alphamind.data_sources.treasury.client.httpx.get", return_value=mock_response):
            client = TreasuryClient()
            assert client.verify_connectivity() is False
