"""Tests for ``alphamind.execution.broker_adapter.errors``.

Story ALP-378 — Alpaca-error → adapter rejection mapping per
``broker-adapter.md § Order submission``.
"""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError

from alphamind.execution.broker_adapter import (
    PermanentRejection,
    classify_alpaca_error,
    is_transient,
)


def _make_api_error(status: int, code: int | str, message: str) -> APIError:
    """Build an ``APIError`` whose ``.status_code`` and ``.message`` resolve.

    ``APIError.status_code`` reads ``self._http_error.response.status_code``;
    ``APIError.message`` parses ``self._error`` JSON. We construct a fake
    ``http_error`` carrying a ``response.status_code`` to drive the property.
    The constructor is untyped in ``alpaca-py``; cast through ``Any`` so the
    test stays mypy-clean without a per-call ``# type: ignore``.
    """
    body = json.dumps({"code": code, "message": message})
    fake_http_error = MagicMock()
    fake_http_error.response.status_code = status
    return cast(APIError, cast(Any, APIError)(body, http_error=fake_http_error))


# ---------------------------------------------------------------------------
# Permanent classifications — each documented rejection reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message", "expected_code"),
    [
        # 422 generic validation
        (422, "validation failed: symbol field is required", "validation_failed"),
        (422, "request is invalid", "validation_failed"),
        # 403 buying power / shares
        (403, "insufficient buying power", "insufficient_buying_power"),
        (403, "insufficient_buying_power for order", "insufficient_buying_power"),
        (403, "insufficient shares for sell-side", "insufficient_shares"),
        # 403 options-level
        (403, "options level not approved for this strategy", "options_level_not_approved"),
        (403, "options_level_not_approved", "options_level_not_approved"),
        # 422 contract expired
        (422, "contract expired", "contract_expired"),
        (422, "the option's expiration is expired", "contract_expired"),
        # 403 underlying halted
        (403, "underlying is in regulatory halt", "underlying_halted"),
        # 422 invalid mleg structure
        (422, "invalid legs[0].ratio_qty", "invalid_legs"),
        (422, "legs[2] mismatched expirations", "invalid_legs"),
    ],
)
def test_classify_alpaca_error_returns_documented_code(
    status: int, message: str, expected_code: str
) -> None:
    exc = _make_api_error(status, code=42, message=message)

    rejection = classify_alpaca_error(exc)

    assert rejection is not None
    assert rejection.code == expected_code
    assert rejection.http_status == status
    assert rejection.alpaca_message == message


def test_classify_alpaca_error_falls_back_to_other_permanent_for_unmapped_4xx() -> None:
    exc = _make_api_error(400, code=99, message="some unfamiliar bad-request reason")

    rejection = classify_alpaca_error(exc)

    assert rejection is not None
    assert rejection.code == "other_permanent"
    assert rejection.http_status == 400


def test_classify_alpaca_error_returns_permanent_rejection_dataclass() -> None:
    exc = _make_api_error(403, code=40310000, message="insufficient buying power")
    rejection = classify_alpaca_error(exc)
    assert isinstance(rejection, PermanentRejection)


# ---------------------------------------------------------------------------
# Transient classifications — None for retriable
# ---------------------------------------------------------------------------


def test_5xx_returns_none() -> None:
    exc = _make_api_error(503, code=1, message="service unavailable")
    assert classify_alpaca_error(exc) is None


def test_500_returns_none() -> None:
    exc = _make_api_error(500, code=1, message="internal server error")
    assert classify_alpaca_error(exc) is None


def test_network_error_returns_none() -> None:
    """``httpx.ConnectError`` carries no HTTP status — must classify transient."""
    import httpx

    exc = httpx.ConnectError("name resolution failed")
    assert classify_alpaca_error(exc) is None


def test_timeout_returns_none() -> None:
    """``httpx.TimeoutException`` carries no HTTP status — must classify transient."""
    import httpx

    exc = httpx.ReadTimeout("read timed out")
    assert classify_alpaca_error(exc) is None


def test_raw_oserror_returns_none() -> None:
    """``OSError`` (e.g., DNS / socket failure) has no status — transient."""
    exc = OSError("connection refused")
    assert classify_alpaca_error(exc) is None


# ---------------------------------------------------------------------------
# is_transient parity with classify_alpaca_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _make_api_error(422, code=42, message="validation failed"),
        lambda: _make_api_error(403, code=42, message="insufficient buying power"),
        lambda: _make_api_error(503, code=1, message="bad gateway"),
        lambda: __import__("httpx").ConnectError("dns failure"),
        lambda: OSError("kernel said no"),
    ],
)
def test_is_transient_matches_classify_alpaca_error(exc_factory: Any) -> None:
    exc = exc_factory()
    assert is_transient(exc) == (classify_alpaca_error(exc) is None)
