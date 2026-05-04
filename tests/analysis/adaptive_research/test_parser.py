"""Tests for the adaptive-research JSON-payload parser — ALP-288 (B).

Post-migration the SDK delivers the brief as a dict on
``ResultMessage.structured_output``; the parser is now thin coercion +
normalization + ``AdaptiveBrief.model_validate``. The exhaustive shape-
discipline tests that lived here pre-migration covered the regex parser's
fence-stripping, header-literal, marker-scanning, sector-alias, and
counts-line tolerances — all of which deleted with the parser. The model's
invariants (assessment branch enforcement, sequential indexing, etc.) live
in ``test_models.py`` against pre-built dicts.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from alphamind.analysis.adaptive_research.parser import ParseError, parse_adaptive_brief

# ---------------------------------------------------------------------------
# Verbatim minimal SIGNAL-thread payload mirroring the prompt's example
# ---------------------------------------------------------------------------

EXAMPLE_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "threads_investigated_count": 2,
    "anomalies_triaged_count": 3,
    "anomalies_deferred": ["Distillation: gold-yield correlation flip, persistence 2 sessions"],
    "threads": [
        {
            "thread_id": "AR-1",
            "trigger": "[SA-TECH-ANOM-1]",
            "question": "What drove NVDA volume?",
            "tickers": ["NVDA"],
            "sector": "tech_semis",
            "tools_used": ["news_search", "options_flow"],
            "findings": ["news_search returned pre-earnings notes"],
            "assessment": "signal",
            "confidence": "moderate",
            "implication": "Pre-earnings repositioning.",
            "strengthens": ["SA-TECH-2"],
            "weakens": [],
        },
        {
            "thread_id": "AR-2",
            "trigger": "[SA-ENERGY-ANOM-1]",
            "question": "Sector-wide shift?",
            "tickers": ["VLO", "MPC"],
            "sector": "energy",
            "tools_used": ["news_search", "macro_data"],
            "findings": ["no outage headlines", "crack spread widened"],
            "assessment": "inconclusive",
            "confidence": "low",
            "missing": "Per-name capacity-utilization data.",
        },
    ],
}


def _signal_thread(**overrides: Any) -> dict[str, Any]:
    base = {
        "thread_id": "AR-1",
        "trigger": "[SA-TECH-ANOM-1]",
        "question": "What drove the spike?",
        "tickers": ["NVDA"],
        "sector": "tech_semis",
        "tools_used": ["news_search"],
        "findings": ["news_search returned notes"],
        "assessment": "signal",
        "confidence": "moderate",
        "implication": "Pre-earnings repositioning.",
        "strengthens": ["SA-TECH-1"],
        "weakens": [],
    }
    base.update(overrides)
    return base


def _brief(thread: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "invocation_id": "inv-test",
        "threads_investigated_count": 1,
        "anomalies_triaged_count": 1,
        "anomalies_deferred": [],
        "threads": [thread],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    """ParseError and parse_adaptive_brief are importable with the new shape."""
    assert callable(parse_adaptive_brief)
    err = ParseError("f", "m")
    assert err.field_path == "f"
    assert err.message == "m"


# ---------------------------------------------------------------------------
# Happy path — example payload coerces to AdaptiveBrief
# ---------------------------------------------------------------------------


def test_example_payload_coerces() -> None:
    """The verbatim worked example from the prompt round-trips through the parser."""
    brief = parse_adaptive_brief(
        copy.deepcopy(EXAMPLE_PAYLOAD), invocation_id="inv-2026-04-23T14-30Z"
    )
    assert len(brief.threads) == 2
    assert {t.assessment.value for t in brief.threads} == {"signal", "inconclusive"}
    signal, inconclusive = brief.threads
    assert signal.strengthens == ("SA-TECH-2",)
    assert signal.weakens == ()
    assert inconclusive.missing is not None


def test_round_trip_via_model_dump() -> None:
    """parse → model_dump → parse again gives a structurally-equal AdaptiveBrief."""
    first = parse_adaptive_brief(
        copy.deepcopy(EXAMPLE_PAYLOAD), invocation_id="inv-2026-04-23T14-30Z"
    )
    payload = first.model_dump(mode="json")
    second = parse_adaptive_brief(payload, invocation_id="inv-2026-04-23T14-30Z")
    assert first.model_dump() == second.model_dump()


# ---------------------------------------------------------------------------
# Invocation-id is canonical: caller overrides whatever the model emits
# ---------------------------------------------------------------------------


def test_invocation_id_caller_supplied_wins() -> None:
    """The brief's emitted invocation_id is overwritten by the caller's value."""
    payload = _brief(_signal_thread(), invocation_id="model-invented-id")
    brief = parse_adaptive_brief(payload, invocation_id="harness-canonical-id")
    assert brief.invocation_id == "harness-canonical-id"


# ---------------------------------------------------------------------------
# Pre-validate normalization: tools_used parens stripped + de-duplicated
# ---------------------------------------------------------------------------


def test_tools_used_parens_commentary_stripped_and_deduped() -> None:
    """`ticker_deep_pull (COP), news_search, ticker_deep_pull (MPC)` collapses to two entries."""
    thread = _signal_thread(
        tools_used=["ticker_deep_pull (COP)", "news_search", "ticker_deep_pull (MPC)"]
    )
    brief = parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert brief.threads[0].tools_used == ("ticker_deep_pull", "news_search")


# ---------------------------------------------------------------------------
# Pre-validate normalization: bracketed reference IDs extracted from refs
# ---------------------------------------------------------------------------


def test_strengthens_with_bracketed_id_strips_brackets() -> None:
    """`["[SA-TECH-1]"]` parses to `("SA-TECH-1",)` — bracket wire form is stripped."""
    thread = _signal_thread(strengthens=["[SA-TECH-1]"], weakens=[])
    brief = parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1",)


def test_strengthens_with_trailing_commentary_extracts_id() -> None:
    """`["[SA-TECH-1] (rationale)"]` parses to `("SA-TECH-1",)` — free-text discarded."""
    thread = _signal_thread(strengthens=["[SA-TECH-1] (the move shares a macro driver)"])
    brief = parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1",)


def test_strengthens_bare_id_passes_through() -> None:
    """`["SA-TECH-1"]` (no brackets) passes through unchanged."""
    thread = _signal_thread(strengthens=["SA-TECH-1"])
    brief = parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1",)


def test_strengthens_multiple_bracketed_refs_in_one_string_split() -> None:
    """`["[SA-TECH-1] (note A) and [SA-TECH-2] (note B)"]` splits into two refs."""
    thread = _signal_thread(strengthens=["[SA-TECH-1] (note A) and [SA-TECH-2] (note B)"])
    brief = parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert brief.threads[0].strengthens == ("SA-TECH-1", "SA-TECH-2")


# ---------------------------------------------------------------------------
# ParseError surface: None / non-dict / missing-field / invalid-enum
# ---------------------------------------------------------------------------


def test_none_payload_raises_parse_error() -> None:
    """`None` (structured_output not populated) is a parse failure with envelope path."""
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(None, invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"
    assert "structured_output" in exc_info.value.message


def test_non_dict_payload_raises_parse_error() -> None:
    """A list at the top-level is a parse failure, not a Pydantic error."""
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief([], invocation_id="inv-test")
    assert exc_info.value.field_path == "envelope"


def test_signal_thread_missing_strengthens_raises_with_field_path() -> None:
    """Conditional-field invariant violation surfaces as ParseError naming the field."""
    bad_thread = _signal_thread()
    bad_thread.pop("strengthens")
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(_brief(bad_thread), invocation_id="inv-test")
    # Field path threads through Pydantic's loc tuple as `threads.0.strengthens`
    # (or similar) so the harness's first-error retry message can name the field.
    assert "strengthens" in exc_info.value.field_path or "strengthens" in exc_info.value.message


def test_invalid_assessment_enum_raises_parse_error() -> None:
    """An enum value outside the closed set fails Pydantic and surfaces as ParseError."""
    thread = _signal_thread(assessment="maybe")
    with pytest.raises(ParseError) as exc_info:
        parse_adaptive_brief(_brief(thread), invocation_id="inv-test")
    assert "assessment" in exc_info.value.field_path or "assessment" in exc_info.value.message


def test_threads_count_mismatch_raises_parse_error() -> None:
    """Brief invariants (model_validator) surface as ParseError too."""
    payload = _brief(_signal_thread(), threads_investigated_count=5)
    with pytest.raises(ParseError):
        parse_adaptive_brief(payload, invocation_id="inv-test")
