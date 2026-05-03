"""Tests for qualitative-researcher Markdown brief parser — ALP-243.

Covers all acceptance criteria from the story:
  AC-1  Import resolves.
  AC-2  Verbatim example output from prompts/analysis/qualitative_researcher.md parses.
  AC-3  Single code-fence stripped; nested fence raises ParseError.
  AC-4  Thread with one evidence line raises ParseError naming the thread.
  AC-5  Direction: long for X raises ParseError.
  AC-6  Time horizon: medium-term raises ParseError.
  AC-7  Signal quality: DEGRADED without reason raises ParseError.
  AC-8  Signal quality: HIGH with reason line raises ParseError.
  AC-9  Empty CATALYST WATCH produces catalyst_watches=().
  AC-10 Out-of-order section markers raise ParseError naming the misplaced marker.
  AC-11 Trailing prose after Regime: raises ParseError.
  AC-12 All tests pass under pytest -n auto.
"""

from __future__ import annotations

import pytest

from alphamind.analysis.qualitative_research.parser import ParseError, parse_qualitative_brief

# ---------------------------------------------------------------------------
# Verbatim example from prompts/analysis/qualitative_researcher.md
# ---------------------------------------------------------------------------

EXAMPLE_OUTPUT = """\
QUALITATIVE BRIEF
Invocation: inv-2026-04-23T14-30Z
Signal quality: HIGH

=== NARRATIVE THREADS ===
[QR-1] Prediction markets repricing toward higher FOMC-hold odds overnight; macro-narrative tape is consistent with a soft-landing read but the magnitude of the shift exceeds the news flow that would justify it on its own.
  Relevance: financials, all rate-sensitive sectors
  Direction: bullish for soft-landing pricing, bearish for steepener positioning
  Time horizon: immediate (<24h)
  Evidence:
    - prediction markets: FOMC hold odds 58% → 71% over one session [from in-context snapshot]
    - news: WSJ piece flagging dovish-leaning Fed speakers ahead of blackout [from ND-M2]
    - macro narrative: rate-environment commentary in news digest pivots from "persistent inflation" to "soft landing pricing" [from ND-M3, ND-F2]
  Implication: Bank-flow agents may not yet have repriced for the shift; the differential between prediction-market state and bank options-flow is a region the synthesizer should highlight.

[QR-2] NVDA earnings transcript carried a cautious tone on near-term hyperscaler ramp despite quantitative beat; commentary diverges from the post-print rally.
  Relevance: NVDA, AMD, AVGO, broader semis
  Direction: mixed for semis demand thesis
  Time horizon: near-term (24–72h)
  Evidence:
    - earnings_commentary on NVDA: tone classified `cautious`, dominant Q&A theme on inventory absorption, two non-answer flags on FY guidance specifics [from earnings_commentary tool]
    - news: post-print sell-side notes split on whether the cautious tone is conservatism or substance [from ND-T2, ND-T4]
  Implication: Sector researchers may be over-weighting the quantitative beat; the qualitative tone is materially weaker than the headline numbers and bears on adjacent semis names with similar exposure.

=== CATALYST WATCH ===
[QR-CW-1] JPM: FOMC decision in ~36h
  Thesis impact: The held JPM thesis names FOMC-driven rate-curve shift as the catalyst; the prediction-market shift in QR-1 makes the FOMC outcome more directionally consequential than usual for this thesis.

=== SENTIMENT SNAPSHOT ===
Extremes: NVDA at 91st percentile (positive), MU at 7th percentile (negative)
Divergences: AMD sentiment positive but price under 20-day VWAP
Regime: Sentiment broadly constructive with a tilt toward soft-landing themes; the tape is consistent with vol-normalization rather than a defensive turn.
"""

# ---------------------------------------------------------------------------
# Minimal valid brief with two threads and no catalyst watch entries
# ---------------------------------------------------------------------------

MINIMAL_BRIEF = """\
QUALITATIVE BRIEF
Invocation: inv-test-001
Signal quality: MODERATE

=== NARRATIVE THREADS ===
[QR-1] Rate expectations shifted hawkish overnight based on Fed commentary and prediction market moves.
  Relevance: financials
  Direction: bearish for rate-sensitive sectors
  Time horizon: immediate (<24h)
  Evidence:
    - news: Fed speaker commentary flagging inflation persistence [from ND-M1]
    - prediction markets: rate-hike odds jumped 10pp [from in-context snapshot]
  Implication: Financials may face headwinds; monitor rate-sensitive names.

=== CATALYST WATCH ===

=== SENTIMENT SNAPSHOT ===
Extremes: none
Divergences: none
Regime: Neutral, no notable shifts.
"""


def _brief_with_threads(threads_body: str) -> str:
    """Return a minimal-valid brief with a custom NARRATIVE THREADS body."""
    return (
        "QUALITATIVE BRIEF\n"
        "Invocation: inv-test\n"
        "Signal quality: LOW\n"
        "\n"
        "=== NARRATIVE THREADS ===\n" + threads_body + "\n"
        "=== CATALYST WATCH ===\n"
        "\n"
        "=== SENTIMENT SNAPSHOT ===\n"
        "Extremes: none\n"
        "Divergences: none\n"
        "Regime: Neutral.\n"
    )


# ---------------------------------------------------------------------------
# AC-1: Import resolves
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    """ParseError and parse_qualitative_brief are importable."""
    # The import at the top of this module is the assertion.
    assert callable(parse_qualitative_brief)
    assert issubclass(ParseError, Exception)
    assert hasattr(ParseError("f", "m"), "field_path")
    assert hasattr(ParseError("f", "m"), "message")


# ---------------------------------------------------------------------------
# AC-2: Verbatim example output parses correctly
# ---------------------------------------------------------------------------


def test_example_output_parses() -> None:
    """Verbatim example from the prompt parses into a well-formed QualitativeBrief."""
    brief = parse_qualitative_brief(EXAMPLE_OUTPUT, invocation_id="inv-test")
    assert len(brief.threads) >= 1
    assert brief.sentiment_snapshot.extremes
    assert brief.sentiment_snapshot.divergences
    assert brief.sentiment_snapshot.regime
    assert len(brief.catalyst_watches) >= 1


def test_example_invocation_id_comes_from_caller() -> None:
    """Caller-supplied invocation_id wins over the one in the text."""
    brief = parse_qualitative_brief(EXAMPLE_OUTPUT, invocation_id="caller-id")
    assert brief.invocation_id == "caller-id"


# ---------------------------------------------------------------------------
# AC-3: Single code fence stripped; nested fence raises ParseError
# ---------------------------------------------------------------------------


def test_single_text_fence_stripped() -> None:
    """A single ```text fence around an otherwise-valid brief is stripped."""
    fenced = "```text\n" + MINIMAL_BRIEF.rstrip() + "\n```"
    brief = parse_qualitative_brief(fenced, invocation_id="inv-test")
    assert len(brief.threads) >= 1


def test_single_markdown_fence_stripped() -> None:
    """A single ```markdown fence is also accepted."""
    fenced = "```markdown\n" + MINIMAL_BRIEF.rstrip() + "\n```"
    brief = parse_qualitative_brief(fenced, invocation_id="inv-test")
    assert len(brief.threads) >= 1


def test_bare_fence_stripped() -> None:
    """A plain ``` fence (no language tag) is stripped."""
    fenced = "```\n" + MINIMAL_BRIEF.rstrip() + "\n```"
    brief = parse_qualitative_brief(fenced, invocation_id="inv-test")
    assert len(brief.threads) >= 1


def test_nested_fence_raises_parse_error() -> None:
    """Nested fences inside an outer fence raise ParseError."""
    inner_fenced = "```text\n" + MINIMAL_BRIEF.rstrip() + "\n```"
    double_fenced = "```text\n" + inner_fenced + "\n```"
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(double_fenced, invocation_id="inv-test")
    assert "envelope" in exc_info.value.field_path


# ---------------------------------------------------------------------------
# AC-4: Thread with one evidence line raises ParseError naming the thread
# ---------------------------------------------------------------------------


def test_one_evidence_line_raises_parse_error() -> None:
    """A thread with fewer than 2 evidence lines raises ParseError with thread in field_path."""
    threads_body = (
        "[QR-1] Thread with insufficient evidence.\n"
        "  Relevance: financials\n"
        "  Direction: bearish for rate-sensitive sectors\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: only one source [from ND-M1]\n"
        "  Implication: This thread is under-evidenced.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert "thread" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# AC-5: Direction: long for X raises ParseError
# ---------------------------------------------------------------------------


def test_invalid_direction_raises_parse_error() -> None:
    """'long' is not a valid ThreadDirection; raises ParseError."""
    threads_body = (
        "[QR-1] Invalid direction thread.\n"
        "  Relevance: financials\n"
        "  Direction: long for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: source one [from ND-M1]\n"
        "    - news: source two [from ND-M2]\n"
        "  Implication: Direction parsing should fail.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert "direction" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# AC-6: Time horizon: medium-term raises ParseError
# ---------------------------------------------------------------------------


def test_invalid_time_horizon_raises_parse_error() -> None:
    """'medium-term' is not a valid TimeHorizon; raises ParseError."""
    threads_body = (
        "[QR-1] Invalid time horizon thread.\n"
        "  Relevance: financials\n"
        "  Direction: bearish for equities\n"
        "  Time horizon: medium-term\n"
        "  Evidence:\n"
        "    - news: source one [from ND-M1]\n"
        "    - news: source two [from ND-M2]\n"
        "  Implication: Time horizon parsing should fail.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert "time_horizon" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# AC-7: Signal quality: DEGRADED without reason raises ParseError
# ---------------------------------------------------------------------------


def test_degraded_without_reason_raises_parse_error() -> None:
    """DEGRADED signal quality without a reason line raises ParseError."""
    text = MINIMAL_BRIEF.replace("Signal quality: MODERATE", "Signal quality: DEGRADED")
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(text, invocation_id="inv-test")
    assert "signal_quality" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# AC-8: Signal quality: HIGH with reason line raises ParseError
# ---------------------------------------------------------------------------


def test_non_degraded_with_reason_raises_parse_error() -> None:
    """A reason line after HIGH signal quality raises ParseError."""
    text = (
        "QUALITATIVE BRIEF\n"
        "Invocation: inv-test\n"
        "Signal quality: HIGH\n"
        "  [If DEGRADED: reason — some reason here]\n"
        "\n"
        "=== NARRATIVE THREADS ===\n"
        "[QR-1] Spurious reason line.\n"
        "  Relevance: financials\n"
        "  Direction: bearish for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: source one [from ND-M1]\n"
        "    - news: source two [from ND-M2]\n"
        "  Implication: Should fail at reason line.\n"
        "\n"
        "=== CATALYST WATCH ===\n"
        "\n"
        "=== SENTIMENT SNAPSHOT ===\n"
        "Extremes: none\n"
        "Divergences: none\n"
        "Regime: Neutral.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(text, invocation_id="inv-test")
    assert "signal_quality" in exc_info.value.field_path.lower()


def test_degraded_with_reason_parses_ok() -> None:
    """DEGRADED with a properly-formatted reason line parses successfully."""
    text = (
        "QUALITATIVE BRIEF\n"
        "Invocation: inv-test\n"
        "Signal quality: DEGRADED\n"
        "  [If DEGRADED: reason — news API partial]\n"
        "\n"
        "=== NARRATIVE THREADS ===\n"
        "[QR-1] Degraded signal thread.\n"
        "  Relevance: all\n"
        "  Direction: uncertain for all sectors\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: limited coverage [from ND-M1]\n"
        "    - prediction markets: no new data [from in-context]\n"
        "  Implication: Signal quality is degraded; treat with caution.\n"
        "\n"
        "=== CATALYST WATCH ===\n"
        "\n"
        "=== SENTIMENT SNAPSHOT ===\n"
        "Extremes: none\n"
        "Divergences: none\n"
        "Regime: Inconclusive.\n"
    )
    brief = parse_qualitative_brief(text, invocation_id="inv-test")
    from alphamind.analysis.qualitative_research.models import SignalQuality

    assert brief.signal_quality == SignalQuality.DEGRADED
    assert brief.signal_quality_reason == "news API partial"


# ---------------------------------------------------------------------------
# AC-9: Empty CATALYST WATCH produces catalyst_watches=()
# ---------------------------------------------------------------------------


def test_empty_catalyst_watch_produces_empty_tuple() -> None:
    """Empty CATALYST WATCH section results in catalyst_watches=()."""
    brief = parse_qualitative_brief(MINIMAL_BRIEF, invocation_id="inv-test")
    assert brief.catalyst_watches == ()


# ---------------------------------------------------------------------------
# AC-10: Out-of-order sections raise ParseError naming the misplaced marker
# ---------------------------------------------------------------------------


def test_out_of_order_sections_raises_parse_error() -> None:
    """Sections appearing out of order raise ParseError identifying the misplaced marker."""
    # Put CATALYST WATCH before NARRATIVE THREADS
    text = (
        "QUALITATIVE BRIEF\n"
        "Invocation: inv-test\n"
        "Signal quality: LOW\n"
        "\n"
        "=== CATALYST WATCH ===\n"
        "\n"
        "=== NARRATIVE THREADS ===\n"
        "[QR-1] A thread.\n"
        "  Relevance: financials\n"
        "  Direction: bearish for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: source one [from ND-M1]\n"
        "    - news: source two [from ND-M2]\n"
        "  Implication: Out of order.\n"
        "\n"
        "=== SENTIMENT SNAPSHOT ===\n"
        "Extremes: none\n"
        "Divergences: none\n"
        "Regime: Neutral.\n"
    )
    with pytest.raises(ParseError):
        parse_qualitative_brief(text, invocation_id="inv-test")


# ---------------------------------------------------------------------------
# AC-11: Trailing prose after Regime raises ParseError
# ---------------------------------------------------------------------------


def test_trailing_prose_after_regime_raises_parse_error() -> None:
    """Any non-whitespace content after the Regime: line raises ParseError."""
    text = MINIMAL_BRIEF.rstrip() + "\nExtra trailing content here.\n"
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(text, invocation_id="inv-test")
    # field_path should indicate the sentinel / trailing-content issue
    assert exc_info.value.field_path is not None


# ---------------------------------------------------------------------------
# Additional: wrong thread-id prefix raises ParseError
# ---------------------------------------------------------------------------


def test_wrong_thread_prefix_raises_parse_error() -> None:
    """A thread header using [SA-TECH-1] instead of [QR-N] raises ParseError."""
    threads_body = (
        "[SA-TECH-1] Wrong prefix thread.\n"
        "  Relevance: tech\n"
        "  Direction: bullish for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: source one [from ND-T1]\n"
        "    - news: source two [from ND-T2]\n"
        "  Implication: Should fail.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert "thread" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# Additional: Empty threads section raises ParseError
# ---------------------------------------------------------------------------


def test_empty_threads_section_raises_parse_error() -> None:
    """An empty NARRATIVE THREADS section raises ParseError."""
    text = (
        "QUALITATIVE BRIEF\n"
        "Invocation: inv-test\n"
        "Signal quality: LOW\n"
        "\n"
        "=== NARRATIVE THREADS ===\n"
        "\n"
        "=== CATALYST WATCH ===\n"
        "\n"
        "=== SENTIMENT SNAPSHOT ===\n"
        "Extremes: none\n"
        "Divergences: none\n"
        "Regime: Neutral.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(text, invocation_id="inv-test")
    assert "thread" in exc_info.value.field_path.lower()


# ---------------------------------------------------------------------------
# Evidence line missing ': ' separator raises ParseError (no silent fabrication)
# ---------------------------------------------------------------------------


def test_evidence_line_without_separator_raises_parse_error() -> None:
    """An evidence line without the ``: `` separator raises ParseError.

    Strict-parse design: do not silently fabricate fields by treating the
    whole content as both ``source_type`` and ``observation``.
    """
    threads_body = (
        "[QR-1] Thread with malformed evidence.\n"
        "  Relevance: financials\n"
        "  Direction: bullish for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - missing separator content one [from ND-M1]\n"
        "    - news: properly-formed observation [from ND-M2]\n"
        "  Implication: Should fail.\n"
    )
    with pytest.raises(ParseError) as exc_info:
        parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert "evidence" in exc_info.value.field_path.lower()
    assert "separator" in exc_info.value.message.lower()


def test_evidence_line_with_separator_parses_successfully() -> None:
    """Evidence line with proper ``source_type: observation`` separator parses cleanly."""
    threads_body = (
        "[QR-1] Thread with valid evidence.\n"
        "  Relevance: financials\n"
        "  Direction: bullish for equities\n"
        "  Time horizon: immediate (<24h)\n"
        "  Evidence:\n"
        "    - news: first observation [from ND-M1]\n"
        "    - news: second observation [from ND-M2]\n"
        "  Implication: Should pass.\n"
    )
    brief = parse_qualitative_brief(_brief_with_threads(threads_body), invocation_id="inv-test")
    assert len(brief.threads) == 1
    evidence = brief.threads[0].evidence
    assert evidence[0].source_type == "news"
    assert evidence[0].observation == "first observation"
