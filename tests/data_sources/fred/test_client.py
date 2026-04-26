"""Tests for src/alphamind/data_sources/fred/client.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


def test_verify_connectivity_returns_true_on_success() -> None:
    """verify_connectivity() returns True when the SDK responds."""
    from alphamind.data_sources.fred.client import FredClient

    mock_fred = MagicMock()
    mock_fred.get_series_info.return_value = MagicMock(title="10-Year Treasury")

    with patch("alphamind.data_sources.fred.client.Fred", return_value=mock_fred):
        client = FredClient(api_key="test-key")
        assert client.verify_connectivity() is True
        mock_fred.get_series_info.assert_called_once_with("DGS10")


def test_verify_connectivity_returns_false_on_exception() -> None:
    """verify_connectivity() returns False when the SDK raises."""
    from alphamind.data_sources.fred.client import FredClient

    mock_fred = MagicMock()
    mock_fred.get_series_info.side_effect = Exception("network error")

    with patch("alphamind.data_sources.fred.client.Fred", return_value=mock_fred):
        client = FredClient(api_key="test-key")
        assert client.verify_connectivity() is False
