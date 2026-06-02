"""Tests for src/alphamind/data_sources/fred/client.py."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class _FakeFredSdk:
    """Minimal stand-in for fredapi.Fred used to drive FredClient tests."""

    def __init__(self, info_response: Any = None, info_error: Exception | None = None) -> None:
        self._info_response = info_response
        self._info_error = info_error

    def get_series_info(self, series_id: str) -> Any:
        if self._info_error is not None:
            raise self._info_error
        return self._info_response


def _make_client(sdk: _FakeFredSdk) -> Any:
    """Build a FredClient with the fakable SDK swapped in."""
    from alphamind.data_sources.fred.client import FredClient

    client = FredClient.__new__(FredClient)
    client._fred = sdk
    # Use a simple limiter that does nothing — verify_connectivity also
    # touches self._rl.acquire().
    from alphamind.data_sources._common import RateLimiter

    rl = RateLimiter()
    rl.set_limit("fred", rate_per_minute=120)
    client._rl = rl
    return client


def test_verify_connectivity_returns_true_on_success() -> None:
    """verify_connectivity() returns True when the SDK responds."""
    sdk = _FakeFredSdk(info_response={"title": "10-Year Treasury"})
    client = _make_client(sdk)
    assert client.verify_connectivity() is True


def test_verify_connectivity_returns_false_on_exception() -> None:
    """verify_connectivity() returns False when the SDK raises a vendor error.

    ``fredapi`` wraps urllib HTTP errors as ``ValueError`` (the original
    ``urllib.error.HTTPError`` lives on ``__context__``); the probe also
    surfaces raw ``URLError`` for DNS / TCP failures. Both are caught.
    """
    sdk = _FakeFredSdk(info_error=ValueError("internal server error"))
    client = _make_client(sdk)
    assert client.verify_connectivity() is False
