"""Tests for the analyst output parser — ALP-295."""

from __future__ import annotations

from typing import Any

import pytest

from alphamind.decision.analyst.models import AnalystOutput
from alphamind.decision.analyst.parser import ParseError, parse_analyst_output


def _normal_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal valid normal-mode payload."""
    base: dict[str, Any] = {
        "invocation_id": "inv-original",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "mode": "normal",
        "recommendations": [],
    }
    base.update(overrides)
    return base


def _watchlist_payload(**overrides: Any) -> dict[str, Any]:
    """Minimal valid watchlist-mode payload."""
    base: dict[str, Any] = {
        "invocation_id": "inv-original",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "mode": "watchlist",
        "watchlist": [],
    }
    base.update(overrides)
    return base


def test_none_payload_raises_parse_error() -> None:
    """None payload raises ParseError with field_path='envelope'."""
    with pytest.raises(ParseError) as exc_info:
        parse_analyst_output(None, invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "envelope"
    assert "structured_output was not populated" in err.message


def test_non_dict_payload_raises_parse_error() -> None:
    """Non-dict payload raises ParseError with field_path='envelope'."""
    with pytest.raises(ParseError) as exc_info:
        parse_analyst_output("not a dict", invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "envelope"
    assert "expected dict payload" in err.message
    assert "str" in err.message


def test_valid_normal_mode_returns_analyst_output() -> None:
    """Valid normal-mode payload returns an AnalystOutput."""
    result = parse_analyst_output(_normal_payload(), invocation_id="inv-test")
    assert isinstance(result, AnalystOutput)
    assert result.mode == "normal"
    assert result.recommendations == ()


def test_valid_watchlist_mode_returns_analyst_output() -> None:
    """Valid watchlist-mode payload returns an AnalystOutput."""
    result = parse_analyst_output(_watchlist_payload(), invocation_id="inv-test")
    assert isinstance(result, AnalystOutput)
    assert result.mode == "watchlist"
    assert result.watchlist == ()


def test_invocation_id_always_wins() -> None:
    """invocation_id from the harness overwrites whatever was in the payload."""
    payload = _normal_payload(invocation_id="from-model")
    result = parse_analyst_output(payload, invocation_id="from-harness")
    assert result.invocation_id == "from-harness"


def test_missing_required_field_raises_parse_error_with_field_path() -> None:
    """Missing required field surfaces as ParseError with the dotted field path."""
    # timestamp is required; omit it to trigger a ValidationError
    payload = {
        "mode": "normal",
        "recommendations": [],
    }
    with pytest.raises(ParseError) as exc_info:
        parse_analyst_output(payload, invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "timestamp"
    assert err.message  # non-empty message


def test_conditional_invariant_violation_raises_parse_error() -> None:
    """mode=normal without recommendations violates the model invariant → ParseError."""
    # mode=normal but recommendations is absent (None via model default)
    payload = {
        "invocation_id": "inv-original",
        "timestamp": "2024-01-15T10:00:00+00:00",
        "mode": "normal",
        # recommendations omitted → defaults to None → model_validator fires
    }
    with pytest.raises(ParseError) as exc_info:
        parse_analyst_output(payload, invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path  # non-empty
    assert "recommendations" in err.message or "normal" in err.message
