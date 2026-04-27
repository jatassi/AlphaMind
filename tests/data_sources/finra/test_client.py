"""Tests for finra/client.py — all HTTP calls are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

_CDN_BASE = "https://cdn.finra.org"
_PROBE_PATH = "/equity/regsho/daily/CNMSshvol"


class TestUserAgentHeader:
    def test_get_sends_user_agent_header(self) -> None:
        """Every GET to the FINRA CDN includes a User-Agent header."""
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
        mock_resp.raise_for_status.return_value = None

        with patch.object(client._http, "get", return_value=mock_resp) as mock_get:
            client.get("/equity/regsho/daily/CNMSshvol20260124.txt")

        call_kwargs = mock_get.call_args.kwargs
        sent_headers = call_kwargs.get("headers") or {}
        assert "User-Agent" in sent_headers
        assert "AlphaMind" in sent_headers["User-Agent"]

    def test_get_returns_text(self) -> None:
        """get() returns the response text."""
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        content = (
            "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20260124|AAPL|100|0|200|CNMS\n"
        )
        mock_resp = MagicMock()
        mock_resp.text = content
        mock_resp.raise_for_status.return_value = None

        with patch.object(client._http, "get", return_value=mock_resp):
            result = client.get("/equity/regsho/daily/CNMSshvol20260124.txt")

        assert result == content


class TestVerifyConnectivity:
    def test_returns_true_on_success(self) -> None:
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None

        with patch.object(client._http, "head", return_value=mock_resp):
            assert client.verify_connectivity() is True

    def test_returns_false_on_http_error(self) -> None:
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        request = httpx.Request("HEAD", f"{_CDN_BASE}/equity/regsho/daily/")
        response = httpx.Response(503, request=request)
        with patch.object(client._http, "head") as mock_head:
            mock_head.side_effect = httpx.HTTPStatusError("503", request=request, response=response)
            assert client.verify_connectivity() is False

    def test_returns_false_on_connection_error(self) -> None:
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        with patch.object(client._http, "head") as mock_head:
            mock_head.side_effect = httpx.ConnectError("refused")
            assert client.verify_connectivity() is False


class TestNotFoundHandling:
    def test_get_raises_http_status_error_on_404(self) -> None:
        """get() propagates HTTPStatusError so callers can detect 404."""
        from alphamind.data_sources.finra.client import FinraClient

        client = FinraClient()
        url = f"{_CDN_BASE}/equity/regsho/daily/CNMSshvol20260101.txt"
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        with patch.object(client._http, "get") as mock_get:
            mock_get.side_effect = httpx.HTTPStatusError("404", request=request, response=response)
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/equity/regsho/daily/CNMSshvol20260101.txt")
