"""Tests for the strategist output parser — ALP-305."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.parser import ParseError, parse_strategist_output

_REPO_ROOT = Path(__file__).parents[3]
_PROMPT_PATH = _REPO_ROOT / "prompts" / "decision" / "strategist.md"

_OUTPUT_BLOCK_RE = re.compile(r"<example_output>.*?<output>(.*?)</output>", re.DOTALL)


def _load_example_output_json() -> dict[str, Any]:
    """Extract and parse the JSON inside <example_output><output>...</output>."""
    text = _PROMPT_PATH.read_text(encoding="utf-8")
    m = _OUTPUT_BLOCK_RE.search(text)
    assert m is not None, "Could not find <output> block inside <example_output>"
    return cast("dict[str, Any]", json.loads(m.group(1).strip()))


def test_none_payload_raises_parse_error() -> None:
    """None payload raises ParseError with field_path='envelope'."""
    with pytest.raises(ParseError) as exc_info:
        parse_strategist_output(None, invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "envelope"
    assert "structured_output was not populated" in err.message


def test_non_dict_payload_raises_parse_error() -> None:
    """Non-dict payload raises ParseError with field_path='envelope'."""
    with pytest.raises(ParseError) as exc_info:
        parse_strategist_output("not a dict", invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "envelope"
    assert "expected dict payload" in err.message
    assert "str" in err.message


def test_round_trip_example_output() -> None:
    """The example_output JSON in the prompt round-trips through the parser."""
    payload = _load_example_output_json()
    result = parse_strategist_output(payload, invocation_id="inv-roundtrip")
    assert isinstance(result, StrategistOutput)
    assert result.mode == "normal"
    assert len(result.position_assessments) == 2
    assert len(result.pending_order_assessments) == 1


def test_invocation_id_always_wins() -> None:
    """invocation_id from the harness overwrites whatever was in the payload."""
    payload = _load_example_output_json()
    payload["invocation_id"] = "from-model"
    result = parse_strategist_output(payload, invocation_id="from-harness")
    assert result.invocation_id == "from-harness"


def test_validation_error_wrapped_as_parse_error() -> None:
    """Invalid payload raises ParseError, not raw ValidationError."""
    payload = _load_example_output_json()
    del payload["timestamp"]
    with pytest.raises(ParseError):
        parse_strategist_output(payload, invocation_id="inv-test")


def test_parse_error_message_includes_field_path() -> None:
    """ParseError for a missing required field names that field in field_path."""
    payload = _load_example_output_json()
    del payload["timestamp"]
    with pytest.raises(ParseError) as exc_info:
        parse_strategist_output(payload, invocation_id="inv-test")
    err = exc_info.value
    assert err.field_path == "timestamp"
    assert err.message
    assert "timestamp" in str(err)
