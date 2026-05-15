"""
Tests for bls/client.py.

HTTP is driven through httpx.MockTransport — no real network traffic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from alphamind.data_sources.bls.client import BLSClient

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> BLSClient:
    client = BLSClient(api_key="testkey")
    client._http = httpx.Client(
        timeout=30.0,
        transport=httpx.MockTransport(handler),
    )
    return client


def _make_success_payload(series_id: str = "LNS14000000") -> dict[str, Any]:
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
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_make_success_payload())

        client = _client_with(handler)
        client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")
        assert str(captured[0].url) == BLS_URL

    def test_includes_api_key_in_payload(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_make_success_payload())

        client = _client_with(handler)
        client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")

        import json as _json

        payload = _json.loads(captured[0].content)
        assert payload["registrationkey"] == "testkey"

    def test_sends_series_ids_in_payload(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=_make_success_payload())

        client = _client_with(handler)
        client.post_timeseries(
            ["CES0000000001", "LNS14000000"],
            start_year="2024",
            end_year="2024",
        )

        import json as _json

        payload = _json.loads(captured[0].content)
        assert payload["seriesid"] == ["CES0000000001", "LNS14000000"]
        assert payload["startyear"] == "2024"
        assert payload["endyear"] == "2024"

    def test_returns_list_of_series_results(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_success_payload("LNS14000000"))

        client = _client_with(handler)
        result = client.post_timeseries(["LNS14000000"], start_year="2024", end_year="2024")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["seriesID"] == "LNS14000000"

    def test_batches_more_than_50_series(self) -> None:
        """With an API key, max 50 series per request; 51 series → 2 calls."""
        series_ids = [f"SERIES{i:04d}" for i in range(51)]

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            import json as _json

            payload = _json.loads(request.content)
            batch_ids = payload["seriesid"]
            return httpx.Response(
                200,
                json={
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {"series": [{"seriesID": sid, "data": []} for sid in batch_ids]},
                },
            )

        client = _client_with(handler)
        result = client.post_timeseries(series_ids, start_year="2024", end_year="2024")

        assert call_count == 2
        assert len(result) == 51


# ---------------------------------------------------------------------------
# AC: verify_connectivity returns True on success, raises on failure
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_when_api_key_works(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {"series": [{"seriesID": "LNS14000000", "data": []}]},
                },
            )

        client = _client_with(handler)
        assert client.verify_connectivity() is True

    def test_raises_on_http_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        client = _client_with(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client.verify_connectivity()

    def test_raises_on_api_error_status(self) -> None:
        """BLS returns HTTP 200 but status=REQUEST_FAILED for bad API key."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "REQUEST_FAILED", "message": ["Invalid registration key."]},
            )

        client = _client_with(handler)
        with pytest.raises(RuntimeError, match="BLS API"):
            client.verify_connectivity()


class TestProtocolContract:
    def test_bls_client_implements_bls_api(self) -> None:
        from alphamind.data_sources.bls._protocol import BLSAPI

        client: BLSAPI = BLSClient(api_key="x")
        assert hasattr(client, "post_timeseries")
        assert hasattr(client, "verify_connectivity")
