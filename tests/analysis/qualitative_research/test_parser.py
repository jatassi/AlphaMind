"""Tests for the qualitative-research JSON-payload parser — ALP-288 (C).

Post-migration the SDK delivers the brief as a dict on
``ResultMessage.structured_output``; the parser is now thin coercion +
``QualitativeBrief.model_validate``. The exhaustive shape-discipline tests
that lived here pre-migration covered the regex parser's fence-stripping,
header-literal, marker-scanning, evidence-line, catalyst-watch, and
sentiment-snapshot tolerances — all of which deleted with the parser. The
model's invariants live in ``test_models.py`` against pre-built dicts.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from alphamind.analysis.qualitative_research.parser import ParseError, parse_qualitative_brief

# ---------------------------------------------------------------------------
# Verbatim minimal payload mirroring the prompt's example
# ---------------------------------------------------------------------------

EXAMPLE_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "signal_quality": "high",
    "signal_quality_reason": None,
    "threads": [
        {
            "thread_id": "QR-1",
            "summary": "Prediction markets repricing toward higher FOMC-hold odds overnight.",
            "relevance": "financials, all rate-sensitive sectors",
            "direction": "bullish",
            "subject": "soft-landing pricing",
            "time_horizon": "immediate",
            "evidence": [
                {
                    "source_type": "prediction_markets",
                    "observation": "FOMC hold odds 58% to 71% in one session",
                    "citation": "in-context snapshot",
                },
                {
                    "source_type": "news",
                    "observation": "WSJ piece flagging dovish-leaning Fed speakers",
                    "citation": "ND-M2",
                },
            ],
            "implication": "Bank-flow agents may not yet have repriced.",
        },
    ],
    "catalyst_watches": [
        {
            "catalyst_id": "QR-CW-1",
            "ticker": "JPM",
            "catalyst_name": "FOMC decision",
            "hours_to_event": 36,
            "thesis_impact": "Held JPM thesis names FOMC as the catalyst.",
        },
    ],
    "sentiment_snapshot": {
        "extremes": "NVDA at 91st percentile (positive)",
        "divergences": "none",
        "regime": "broadly constructive",
    },
}


def _brief(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    """ParseError and parse_qualitative_brief are importable with the new shape."""
    assert callable(parse_qualitative_brief)
    err = ParseError("f", "m")
    assert err.field_path == "f"
    assert err.message == "m"


# ---------------------------------------------------------------------------
# Happy path — example payload coerces to QualitativeBrief
# ---------------------------------------------------------------------------


def test_example_payload_coerces() -> None:
    """The verbatim worked example from the prompt round-trips through the parser."""
    brief = parse_qualitative_brief(_brief(), invocation_id="inv-2026-04-23T14-30Z")
    assert brief.signal_quality.value == "high"
    assert brief.signal_quality_reason is None
    assert len(brief.threads) == 1
    assert brief.threads[0].thread_id == "QR-1"
    assert len(brief.catalyst_watches) == 1


def test_round_trip_via_model_dump() -> None:
    """parse → model_dump → parse again gives a structurally-equal QualitativeBrief."""
    first = parse_qualitative_brief(_brief(), invocation_id="inv-2026-04-23T14-30Z")
    payload = first.model_dump(mode="json")
    second = parse_qualitative_brief(payload, invocation_id="inv-2026-04-23T14-30Z")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Invocation-id is canonical: caller overrides whatever the model emits
# ---------------------------------------------------------------------------


def test_invocation_id_caller_supplied_wins() -> None:
    """The brief's emitted invocation_id is overwritten by the caller's value."""
    payload = _brief(invocation_id="model-invented-id")
    brief = parse_qualitative_brief(payload, invocation_id="harness-canonical-id")
    assert brief.invocation_id == "harness-canonical-id"


# ---------------------------------------------------------------------------
# DEGRADED branch enforces signal_quality_reason
# ---------------------------------------------------------------------------


def test_degraded_with_reason_coerces() -> None:
    """signal_quality=degraded with a non-empty reason validates."""
    payload = _brief(signal_quality="degraded", signal_quality_reason="news API partial")
    brief = parse_qualitative_brief(payload, invocation_id="inv-test")
    assert brief.signal_quality.value == "degraded"
    assert brief.signal_quality_reason == "news API partial"


def test_degraded_without_reason_raises() -> None:
    """signal_quality=degraded with reason=None violates the brief invariant."""
    payload = _brief(signal_quality="degraded", signal_quality_reason=None)
    with pytest.raises(ParseError):
        parse_qualitative_brief(payload, invocation_id="inv-test")


def test_high_with_reason_raises() -> None:
    """signal_quality=high with a populated reason violates the brief invariant."""
    payload = _brief(signal_quality="high", signal_quality_reason="should not be set")
    with pytest.raises(ParseError):
        parse_qualitative_brief(payload, invocation_id="inv-test")


# ---------------------------------------------------------------------------
# ParseError surface: None / non-dict / missing-field / invalid-enum
# ---------------------------------------------------------------------------


def test_none_payload_raises_parse_error() -> None:
    """`None` (structured_output not populated) is a parse failure with envelope path."""
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(None, invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"
    assert "structured_output" in exc_info.value.message


def test_non_dict_payload_raises_parse_error() -> None:
    """A list at the top-level is a parse failure, not a Pydantic error."""
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief([], invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"


def test_thread_with_one_evidence_raises() -> None:
    """NarrativeThread invariant: at least 2 evidence lines required."""
    payload = _brief()
    payload["threads"][0]["evidence"] = payload["threads"][0]["evidence"][:1]
    with pytest.raises(ParseError):
        parse_qualitative_brief(payload, invocation_id="inv-test")


def test_invalid_direction_enum_raises() -> None:
    """An enum value outside the closed set fails Pydantic and surfaces as ParseError."""
    payload = _brief()
    payload["threads"][0]["direction"] = "ambivalent"
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(payload, invocation_id="inv-test")
    assert "direction" in exc_info.value.field_path or "direction" in exc_info.value.message


def test_invalid_time_horizon_enum_raises() -> None:
    """`time_horizon` must use the underscored enum value, not the wire-display string."""
    payload = _brief()
    # The legacy text wire form was "immediate (<24h)"; in JSON mode the enum
    # value is just "immediate" and anything else is rejected by the schema.
    payload["threads"][0]["time_horizon"] = "immediate (<24h)"
    with pytest.raises(ParseError):
        parse_qualitative_brief(payload, invocation_id="inv-test")


def test_zero_threads_raises() -> None:
    """At least one narrative thread is required by the brief invariant."""
    payload = _brief(threads=[])
    with pytest.raises(ParseError):
        parse_qualitative_brief(payload, invocation_id="inv-test")
