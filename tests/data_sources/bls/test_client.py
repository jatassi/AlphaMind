"""
Tests for bls/client.py.

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from alphamind.data_sources.bls.client import BLSClient


BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


def _make_success_response(series_id: str = "LNS14000000") -> dict:
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": series_id,
                    "data": [
                        {"year": "2024", "period": "M01", "value": "3.7", "footnotes": [{}]},
                        {"year": "2024", "period": "M02", "value": "3.9", "footnotes": [{}]},
                    ],
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# AC: post_timeseries sends correct POST to BLS endpoint
# ---------------------------------------------------------------------------


class TestPostTimeseries:
    def test_posts_to_correct_url(self) -> None:
        client = BLSClient(api_key="testkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: _make_success_response(),
                raise_for_status=lambda: None,
            )
            client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert call_url == BLS_URL

    def test_includes_api_key_in_payload(self) -> None:
        client = BLSClient(api_key="testkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: _make_success_response(),
                raise_for_status=lambda: None,
            )
            client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")
        payload = mock_post.call_args[1]["json"]
        assert payload["registrationkey"] == "testkey"

    def test_sends_series_ids_in_payload(self) -> None:
        client = BLSClient(api_key="testkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: _make_success_response(),
                raise_for_status=lambda: None,
            )
            client.post_timeseries(
                ["CES0000000001", "LNS14000000"],
                start_year="2024",
                end_year="2024",
            )
        payload = mock_post.call_args[1]["json"]
        assert payload["seriesid"] == ["CES0000000001", "LNS14000000"]
        assert payload["startyear"] == "2024"
        assert payload["endyear"] == "2024"

    def test_returns_list_of_series_results(self) -> None:
        client = BLSClient(api_key="testkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: _make_success_response("LNS14000000"),
                raise_for_status=lambda: None,
            )
            result = client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["seriesID"] == "LNS14000000"

    def test_batches_more_than_50_series(self) -> None:
        """With an API key, max 50 series per request; 51 series → 2 calls."""
        series_ids = [f"SERIES{i:04d}" for i in range(51)]
        client = BLSClient(api_key="testkey")

        call_count = 0

        def fake_post(url, *, json, **kwargs):
            nonlocal call_count
            call_count += 1
            batch_ids = json["seriesid"]
            return MagicMock(
                status_code=200,
                json=lambda b=batch_ids: {
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {
                        "series": [
                            {"seriesID": sid, "data": []} for sid in b
                        ]
                    },
                },
                raise_for_status=lambda: None,
            )

        with patch.object(client._http, "post", side_effect=fake_post):
            result = client.post_timeseries(series_ids, start_year="2024", end_year="2024")

        assert call_count == 2
        assert len(result) == 51


# ---------------------------------------------------------------------------
# AC: verify_connectivity returns True on success, raises on failure
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_when_api_key_works(self) -> None:
        client = BLSClient(api_key="validkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {"series": [{"seriesID": "LNS14000000", "data": []}]},
                },
                raise_for_status=lambda: None,
            )
            assert client.verify_connectivity() is True

    def test_raises_on_http_error(self) -> None:
        client = BLSClient(api_key="badkey")
        request = httpx.Request("POST", BLS_URL)
        response = httpx.Response(401, request=request)
        with patch.object(client._http, "post") as mock_post:
            mock_post.side_effect = httpx.HTTPStatusError(
                "401 Unauthorized", request=request, response=response
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.verify_connectivity()

    def test_raises_on_api_error_status(self) -> None:
        """BLS returns HTTP 200 but status=REQUEST_FAILED for bad API key."""
        client = BLSClient(api_key="badkey")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"status": "REQUEST_FAILED", "message": ["Invalid registration key."]},
                raise_for_status=lambda: None,
            )
            with pytest.raises(RuntimeError, match="BLS API"):
                client.verify_connectivity()
