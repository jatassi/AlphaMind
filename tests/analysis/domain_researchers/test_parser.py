"""Tests for the domain-researcher JSON-payload parser — ALP-288 (D).

Post-migration the SDK delivers each sector brief as a dict on
``ResultMessage.structured_output``; the parser is now thin coercion +
``SectorBrief.model_validate``. The exhaustive shape-discipline tests that
lived here pre-migration covered the regex parser's fence-stripping,
section-header, conviction-sketch tolerance, and per-sector wire-format
checks — all of which deleted with the parser. The model's invariants
live in ``test_models.py`` against pre-built dicts.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.parser import ParseError, parse_brief

# ---------------------------------------------------------------------------
# Verbatim minimal payload mirroring the prompt's example
# ---------------------------------------------------------------------------

EXAMPLE_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "sector": "tech_semis",
    "signal_quality": "high",
    "signal_quality_reason": None,
    "findings": [
        {
            "finding_id": "SA-TECH-1",
            "headline": "NVDA momentum breakout above 200-day MA",
            "tickers": ["NVDA", "AMD"],
            "signal_type": "price_action",
            "strength": "strong",
            "detail": "NVDA broke above resistance on 2x average volume.",
        },
        {
            "finding_id": "SA-TECH-2",
            "headline": "AI capex cycle sustaining hyperscaler demand",
            "tickers": ["NVDA", "INTC", "TSM"],
            "signal_type": "fundamental",
            "strength": "moderate",
            "detail": "Cloud providers maintaining high capex guidance.",
        },
    ],
    "anomalies": [
        {
            "anomaly_id": "SA-TECH-ANOM-1",
            "description": "Unusual divergence between NVDA and AMD price action",
            "anomaly_type": "price_flow_divergence",
            "tickers": ["NVDA", "AMD"],
            "severity": "investigate_now",
            "suggested_question": "Is AMD decoupling from AI narrative?",
        },
    ],
    "thesis_candidates": [
        {
            "thesis_candidate_id": "SA-TECH-TC-1",
            "ticker": "NVDA",
            "direction": "long",
            "setup_type": "momentum",
            "catalyst": "Breakout above 200-day MA with elevated volume confirms trend",
            "time_horizon_hours": "24-72h",
            "conviction_sketch": "high",
            "conviction_justification": "Strong technical setup plus fundamental AI capex support",
            "key_risk": "Macro risk-off event could reverse sector momentum",
        },
    ],
}


def _brief(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    """ParseError and parse_brief are importable with the new shape."""
    assert callable(parse_brief)
    err = ParseError("f", "m")
    assert err.field_path == "f"
    assert err.message == "m"


# ---------------------------------------------------------------------------
# Happy path — example payload coerces to SectorBrief
# ---------------------------------------------------------------------------


def test_example_payload_coerces() -> None:
    """The verbatim worked example coerces to a valid SectorBrief."""
    brief = parse_brief(_brief(), Sector.TECH_SEMIS, invocation_id="inv-2026-04-23T14-30Z")
    assert brief.sector == Sector.TECH_SEMIS
    assert brief.signal_quality.value == "high"
    assert len(brief.findings) == 2
    assert len(brief.anomalies) == 1
    assert len(brief.thesis_candidates) == 1


def test_round_trip_via_model_dump() -> None:
    """parse → model_dump → parse again gives a structurally-equal SectorBrief."""
    first = parse_brief(_brief(), Sector.TECH_SEMIS, invocation_id="inv-2026-04-23T14-30Z")
    payload = first.model_dump(mode="json")
    second = parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-2026-04-23T14-30Z")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Caller-supplied sector and invocation_id always win
# ---------------------------------------------------------------------------


def test_sector_caller_supplied_wins() -> None:
    """The harness's sector argument overrides whatever the model emits."""
    payload = _brief(sector="financials")  # model lied about its sector
    brief = parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")
    assert brief.sector == Sector.TECH_SEMIS


def test_invocation_id_caller_supplied_wins() -> None:
    """Non-empty invocation_id parameter overrides the model's emission."""
    payload = _brief(invocation_id="model-invented")
    brief = parse_brief(payload, Sector.TECH_SEMIS, invocation_id="harness-canonical")
    assert brief.invocation_id == "harness-canonical"


def test_invocation_id_default_keeps_model_value() -> None:
    """Default ``invocation_id=""`` keeps the model's emitted value (back-compat)."""
    payload = _brief(invocation_id="model-emitted")
    brief = parse_brief(payload, Sector.TECH_SEMIS)
    assert brief.invocation_id == "model-emitted"


# ---------------------------------------------------------------------------
# DEGRADED branch enforces signal_quality_reason
# ---------------------------------------------------------------------------


def test_degraded_with_reason_coerces() -> None:
    """signal_quality=degraded with a non-empty reason validates."""
    payload = _brief(signal_quality="degraded", signal_quality_reason="EIA crude data missing")
    brief = parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")
    assert brief.signal_quality.value == "degraded"
    assert brief.signal_quality_reason == "EIA crude data missing"


def test_degraded_without_reason_raises() -> None:
    payload = _brief(signal_quality="degraded", signal_quality_reason=None)
    with pytest.raises(ParseError):
        parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")


def test_high_with_reason_raises() -> None:
    payload = _brief(signal_quality="high", signal_quality_reason="should not be set")
    with pytest.raises(ParseError):
        parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")


# ---------------------------------------------------------------------------
# ParseError surface: None / non-dict / invalid-enum / missing-field
# ---------------------------------------------------------------------------


def test_none_payload_raises_parse_error() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_brief(None, Sector.TECH_SEMIS, invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"
    assert "structured_output" in exc_info.value.message


def test_non_dict_payload_raises_parse_error() -> None:
    with pytest.raises(ParseError) as exc_info:
        parse_brief([], Sector.TECH_SEMIS, invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"


def test_invalid_signal_type_enum_raises() -> None:
    payload = _brief()
    payload["findings"][0]["signal_type"] = "intraday_alpha"
    with pytest.raises(ParseError) as exc_info:
        parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")
    assert "signal_type" in exc_info.value.field_path or "signal_type" in exc_info.value.message


def test_finding_with_empty_tickers_raises() -> None:
    """Finding._tickers_not_empty model invariant surfaces as ParseError."""
    payload = _brief()
    payload["findings"][0]["tickers"] = []
    with pytest.raises(ParseError):
        parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")


def test_finding_id_not_matching_pattern_raises() -> None:
    """``QR-1`` does not match the SectorBrief finding-id pattern."""
    payload = _brief()
    payload["findings"][0]["finding_id"] = "QR-1"
    with pytest.raises(ParseError):
        parse_brief(payload, Sector.TECH_SEMIS, invocation_id="inv-test")
