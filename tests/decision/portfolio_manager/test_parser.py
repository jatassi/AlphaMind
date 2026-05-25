"""Tests for portfolio-manager completion-sentinel parser — ALP-326."""

from __future__ import annotations

import pytest

from alphamind.decision.portfolio_manager import PMCompletionRecord
from alphamind.decision.portfolio_manager.parser import ParseError, parse_pm_completion_record

VALID_PAYLOAD = {
    "invocation_id": "inv-001",
    "timestamp": "2026-01-01T00:00:00Z",
    "envelopes_submitted": 2,
    "verdict_summary": {
        "approve": 1,
        "approve_with_modification": 0,
        "reject": 1,
        "override_with_corrective_action": 0,
    },
}


def test_parse_valid_payload() -> None:
    record = parse_pm_completion_record(VALID_PAYLOAD, invocation_id="inv-001")
    assert isinstance(record, PMCompletionRecord)
    assert record.invocation_id == "inv-001"
    assert record.envelopes_submitted == 2
    assert record.verdict_summary.approve == 1
    assert record.verdict_summary.reject == 1


def test_parser_injects_invocation_id() -> None:
    payload = {**VALID_PAYLOAD, "invocation_id": "payload-id"}
    record = parse_pm_completion_record(payload, invocation_id="harness-id")
    assert record.invocation_id == "harness-id"


def test_parser_handles_missing_invocation_id() -> None:
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "invocation_id"}
    record = parse_pm_completion_record(payload, invocation_id="injected-id")
    assert record.invocation_id == "injected-id"


def test_parser_raises_on_none_payload() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_pm_completion_record(None, invocation_id="inv-001")
    assert exc_info.value.field_path == "completion_record"
    assert "not populated" in exc_info.value.message


def test_parser_raises_on_non_dict_payload() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_pm_completion_record("not a dict", invocation_id="inv-001")
    assert exc_info.value.field_path == "completion_record"
    assert "expected dict payload" in exc_info.value.message
    assert "str" in exc_info.value.message


def test_parser_wraps_validation_error() -> None:
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "verdict_summary"}
    with pytest.raises(ParseError) as exc_info:
        parse_pm_completion_record(payload, invocation_id="inv-001")
    assert "verdict_summary" in exc_info.value.field_path


def test_parser_wraps_verdict_sum_invariant_failure() -> None:
    # envelopes_submitted=3, but verdict counts sum to 2 — model validator rejects this
    payload = {
        **VALID_PAYLOAD,
        "envelopes_submitted": 3,
        "verdict_summary": {
            "approve": 1,
            "approve_with_modification": 0,
            "reject": 1,
            "override_with_corrective_action": 0,
        },
    }
    with pytest.raises(ParseError):
        parse_pm_completion_record(payload, invocation_id="inv-001")
