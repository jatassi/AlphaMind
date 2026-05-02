"""Tests for domain-researcher markdown brief parser (ALP-192).

Red→Green TDD: each test is written against the public interface only.
Tests verify behavior, not implementation details.
"""

from __future__ import annotations

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.parser import ParseError, parse_brief

# ---------------------------------------------------------------------------
# Shared fixtures — canonical valid brief text (Tech & Semis)
# ---------------------------------------------------------------------------

_FULL_BRIEF = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-abc-123
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] NVDA momentum breakout above 200-day MA
  Tickers: NVDA, AMD
  Signal type: price_action
  Strength: strong
  Detail: NVDA broke above resistance on 2x average volume. AMD following.

[SA-TECH-2] AI capex cycle sustaining hyperscaler demand
  Tickers: NVDA, INTC, TSM
  Signal type: fundamental
  Strength: moderate
  Detail: Cloud providers maintaining high capex guidance. NVDA bookings strong.

[SA-TECH-3] Options flow indicating institutional accumulation
  Tickers: NVDA
  Signal type: options
  Strength: strong
  Detail: Large call sweeps detected at Dec 600 strike. Unusual put/call inversion.

=== FLAGGED ANOMALIES ===
[SA-TECH-ANOM-1] Unusual divergence between NVDA and AMD price action
  Anomaly type: price_flow_divergence
  Tickers: NVDA, AMD
  Severity: investigate_now
  Suggested question: Is AMD decoupling from AI narrative or mean-reversion setup?

[SA-TECH-ANOM-2] Short interest spike in SMH ETF
  Anomaly type: volume
  Tickers: SMH
  Severity: investigate_if_persists
  Suggested question: Is SMH short spike hedging or directional bet against semis?

=== THESIS CANDIDATES ===
[SA-TECH-TC-1]
  Ticker: NVDA
  Direction: long
  Setup type: momentum
  Catalyst/driver: Breakout above 200-day MA with elevated volume confirms trend
  Time horizon: 24-72h
  Conviction sketch: high with Strong technical setup plus fundamental AI capex support
  Key risk: Macro risk-off event could reverse sector momentum

[SA-TECH-TC-2]
  Ticker: AMD
  Direction: short
  Setup type: mean_reversion
  Catalyst/driver: AMD lagging NVDA breakout suggests relative weakness opportunity
  Time horizon: 4-24h
  Conviction sketch: moderate with Relative weakness pattern but sector tailwind is risk
  Key risk: Sector rotation into semis could lift AMD despite relative weakness
"""

_MINIMAL_BRIEF = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-min-1
Signal quality: MODERATE

=== KEY FINDINGS ===
[SA-TECH-1] Single finding
  Tickers: AAPL
  Signal type: sentiment
  Strength: weak
  Detail: Minor sentiment shift observed.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""

_DEGRADED_BRIEF = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-deg-1
Signal quality: DEGRADED
  [If DEGRADED: reason — missing options flow data due to API failure]

=== KEY FINDINGS ===
[SA-TECH-1] Limited data finding
  Tickers: NVDA
  Signal type: technical
  Strength: weak
  Detail: Partial data only.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""


# ---------------------------------------------------------------------------
# ParseError class
# ---------------------------------------------------------------------------


def test_parse_error_is_exception() -> None:
    err = ParseError(field_path="header.sector_label", message="missing")
    assert isinstance(err, Exception)


def test_parse_error_attributes() -> None:
    err = ParseError(field_path="findings[0].signal_type", message="invalid value 'foo'")
    assert err.field_path == "findings[0].signal_type"
    assert err.message == "invalid value 'foo'"


# ---------------------------------------------------------------------------
# Tracer bullet — full valid brief parses to correct SectorBrief
# ---------------------------------------------------------------------------


def test_parse_full_brief_returns_sector_brief() -> None:
    from alphamind.analysis.domain_researchers.models import SectorBrief

    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    assert isinstance(brief, SectorBrief)


def test_parse_full_brief_header_fields() -> None:
    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    assert brief.invocation_id == "inv-abc-123"
    assert brief.sector == Sector.TECH_SEMIS
    from alphamind.analysis.domain_researchers.models import SignalQuality

    assert brief.signal_quality == SignalQuality.HIGH
    assert brief.signal_quality_reason is None


def test_parse_full_brief_findings_count() -> None:
    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    assert len(brief.findings) == 3


def test_parse_full_brief_finding_fields() -> None:
    from alphamind.analysis.domain_researchers.models import SignalType, Strength

    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    f1 = brief.findings[0]
    assert f1.finding_id == "SA-TECH-1"
    assert f1.headline == "NVDA momentum breakout above 200-day MA"
    assert "NVDA" in f1.tickers
    assert "AMD" in f1.tickers
    assert f1.signal_type == SignalType.PRICE_ACTION
    assert f1.strength == Strength.STRONG
    assert "NVDA broke above resistance" in f1.detail or "2x average volume" in f1.detail


def test_parse_full_brief_anomalies_count() -> None:
    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    assert len(brief.anomalies) == 2


def test_parse_full_brief_anomaly_fields() -> None:
    from alphamind.analysis.domain_researchers.models import AnomalyType

    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    a1 = brief.anomalies[0]
    assert a1.anomaly_id == "SA-TECH-ANOM-1"
    assert "divergence" in a1.description.lower()
    assert a1.anomaly_type == AnomalyType.PRICE_FLOW_DIVERGENCE
    assert "NVDA" in a1.tickers
    assert a1.severity == "investigate_now"
    assert "AMD" in a1.suggested_question or "mean-reversion" in a1.suggested_question.lower()


def test_parse_full_brief_thesis_count() -> None:
    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    assert len(brief.thesis_candidates) == 2


def test_parse_full_brief_thesis_fields() -> None:
    from alphamind.analysis.domain_researchers.models import (
        ConvictionSketch,
        Direction,
        SetupType,
    )

    brief = parse_brief(_FULL_BRIEF, Sector.TECH_SEMIS)
    tc1 = brief.thesis_candidates[0]
    assert tc1.thesis_candidate_id == "SA-TECH-TC-1"
    assert tc1.ticker == "NVDA"
    assert tc1.direction == Direction.LONG
    assert tc1.setup_type == SetupType.MOMENTUM
    assert "200-day MA" in tc1.catalyst or "elevated volume" in tc1.catalyst
    assert "24-72h" in tc1.time_horizon_hours
    assert tc1.conviction_sketch == ConvictionSketch.HIGH
    assert any(kw in tc1.conviction_justification for kw in ("Strong technical setup", "AI capex"))
    assert "Macro risk-off" in tc1.key_risk


# ---------------------------------------------------------------------------
# Happy path variants
# ---------------------------------------------------------------------------


def test_parse_minimal_brief_empty_sections() -> None:
    brief = parse_brief(_MINIMAL_BRIEF, Sector.TECH_SEMIS)
    assert len(brief.findings) == 1
    assert len(brief.anomalies) == 0
    assert len(brief.thesis_candidates) == 0


def test_parse_degraded_brief_reason() -> None:
    from alphamind.analysis.domain_researchers.models import SignalQuality

    brief = parse_brief(_DEGRADED_BRIEF, Sector.TECH_SEMIS)
    assert brief.signal_quality == SignalQuality.DEGRADED
    assert brief.signal_quality_reason == "missing options flow data due to API failure"


def test_parse_brief_tolerates_leading_trailing_whitespace() -> None:
    padded = "\n\n  " + _MINIMAL_BRIEF + "\n\n"
    brief = parse_brief(padded, Sector.TECH_SEMIS)
    assert brief.invocation_id == "inv-min-1"


# ---------------------------------------------------------------------------
# Fence stripping
# ---------------------------------------------------------------------------


def test_parse_brief_strips_json_code_fence() -> None:
    fenced = "```json\n" + _MINIMAL_BRIEF + "\n```"
    brief = parse_brief(fenced, Sector.TECH_SEMIS)
    assert brief.invocation_id == "inv-min-1"


def test_parse_brief_strips_text_code_fence() -> None:
    fenced = "```text\n" + _MINIMAL_BRIEF + "\n```"
    brief = parse_brief(fenced, Sector.TECH_SEMIS)
    assert brief.invocation_id == "inv-min-1"


def test_parse_brief_strips_unlabeled_code_fence() -> None:
    fenced = "```\n" + _MINIMAL_BRIEF + "\n```"
    brief = parse_brief(fenced, Sector.TECH_SEMIS)
    assert brief.invocation_id == "inv-min-1"


def test_parse_brief_nested_fences_raise_parse_error() -> None:
    nested = "```\n" + "```json\n" + _MINIMAL_BRIEF + "\n```\n" + "```"
    with pytest.raises(ParseError):
        parse_brief(nested, Sector.TECH_SEMIS)


def test_parse_brief_multiple_top_level_fences_raise_parse_error() -> None:
    multi = "```\n" + _MINIMAL_BRIEF + "\n```\n\n```\nextra block\n```"
    with pytest.raises(ParseError):
        parse_brief(multi, Sector.TECH_SEMIS)


# ---------------------------------------------------------------------------
# Header errors
# ---------------------------------------------------------------------------


def _replace_line(text: str, prefix: str, replacement: str) -> str:
    """Replace the first line starting with prefix with replacement."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = replacement + "\n" if replacement else ""
            break
    return "".join(lines)


def _remove_line(text: str, prefix: str) -> str:
    """Remove the first line starting with prefix."""
    lines = text.splitlines(keepends=True)
    return "".join(ln for ln in lines if not ln.strip().startswith(prefix))


def test_missing_sector_brief_line_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "SECTOR BRIEF:")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.sector_label"


def test_missing_invocation_line_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "Invocation:")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.invocation_id"


def test_missing_signal_quality_line_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "Signal quality:")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.signal_quality"


def test_invalid_signal_quality_raises_parse_error() -> None:
    broken = _replace_line(_MINIMAL_BRIEF, "Signal quality:", "Signal quality: UNKNOWN")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.signal_quality"


def test_degraded_without_reason_raises_parse_error() -> None:
    # Create a DEGRADED brief without the reason line
    brief_text = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-deg-no-reason
Signal quality: DEGRADED

=== KEY FINDINGS ===
[SA-TECH-1] Test finding
  Tickers: NVDA
  Signal type: technical
  Strength: weak
  Detail: Test detail text here.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    with pytest.raises(ParseError) as exc_info:
        parse_brief(brief_text, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.signal_quality_reason"


def test_non_degraded_with_reason_line_raises_parse_error() -> None:
    # Non-DEGRADED status should not have a reason line
    brief_text = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-high-1
Signal quality: HIGH
  [If DEGRADED: reason — this should not be here]

=== KEY FINDINGS ===
[SA-TECH-1] Test finding
  Tickers: NVDA
  Signal type: technical
  Strength: weak
  Detail: Test detail here.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    with pytest.raises(ParseError) as exc_info:
        parse_brief(brief_text, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "header.signal_quality_reason"


# ---------------------------------------------------------------------------
# Section presence errors
# ---------------------------------------------------------------------------


def test_missing_key_findings_section_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "=== KEY FINDINGS ===")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "sections.key_findings"


def test_missing_flagged_anomalies_section_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "=== FLAGGED ANOMALIES ===")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "sections.flagged_anomalies"


def test_missing_thesis_candidates_section_raises_parse_error() -> None:
    broken = _remove_line(_MINIMAL_BRIEF, "=== THESIS CANDIDATES ===")
    with pytest.raises(ParseError) as exc_info:
        parse_brief(broken, Sector.TECH_SEMIS)
    assert exc_info.value.field_path == "sections.thesis_candidates"


# ---------------------------------------------------------------------------
# Finding field errors
# ---------------------------------------------------------------------------


def _mutate_brief(template: str, **field_overrides: str | None) -> str:
    """Return *template* with named fields removed (``None``) or replaced.

    Field matching is case-insensitive on the key part before the first colon.
    """
    lines = template.splitlines(keepends=True)
    result = []
    for line in lines:
        stripped = line.strip()
        skip = False
        for key, val in field_overrides.items():
            if stripped.lower().startswith(key.lower() + ":"):
                if val is None:
                    skip = True
                else:
                    indent = line[: len(line) - len(line.lstrip())]
                    line = f"{indent}{key}: {val}\n"
                break
        if not skip:
            result.append(line)
    return "".join(result)


_FINDING_BLOCK = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-f-1
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-TECH-1] Test finding headline
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: This is a detail.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""


def _make_finding_brief(**field_overrides: str | None) -> str:
    return _mutate_brief(_FINDING_BLOCK, **field_overrides)


def test_finding_missing_tickers_raises_parse_error() -> None:
    broken = _make_finding_brief(Tickers=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_finding_missing_signal_type_raises_parse_error() -> None:
    broken = _make_finding_brief(**{"Signal type": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_finding_invalid_signal_type_raises_parse_error() -> None:
    broken = _make_finding_brief(**{"Signal type": "invalid_type"})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_finding_missing_strength_raises_parse_error() -> None:
    broken = _make_finding_brief(Strength=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_finding_invalid_strength_raises_parse_error() -> None:
    broken = _make_finding_brief(Strength="extreme")
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_finding_missing_detail_raises_parse_error() -> None:
    broken = _make_finding_brief(Detail=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


# ---------------------------------------------------------------------------
# Anomaly field errors
# ---------------------------------------------------------------------------

_ANOMALY_BLOCK = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-a-1
Signal quality: HIGH

=== KEY FINDINGS ===

=== FLAGGED ANOMALIES ===
[SA-TECH-ANOM-1] Unusual spike in NVDA volume
  Anomaly type: volume
  Tickers: NVDA
  Severity: investigate_now
  Suggested question: Is this institutional accumulation or distribution?

=== THESIS CANDIDATES ===
"""


def _make_anomaly_brief(**field_overrides: str | None) -> str:
    return _mutate_brief(_ANOMALY_BLOCK, **field_overrides)


def test_anomaly_missing_anomaly_type_raises_parse_error() -> None:
    broken = _make_anomaly_brief(**{"Anomaly type": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_anomaly_invalid_anomaly_type_raises_parse_error() -> None:
    broken = _make_anomaly_brief(**{"Anomaly type": "bad_type"})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_anomaly_missing_tickers_raises_parse_error() -> None:
    broken = _make_anomaly_brief(Tickers=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_anomaly_missing_severity_raises_parse_error() -> None:
    broken = _make_anomaly_brief(Severity=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_anomaly_invalid_severity_raises_parse_error() -> None:
    broken = _make_anomaly_brief(Severity="high_alert")
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_anomaly_missing_suggested_question_raises_parse_error() -> None:
    broken = _make_anomaly_brief(**{"Suggested question": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


# ---------------------------------------------------------------------------
# Thesis candidate field errors
# ---------------------------------------------------------------------------

_TC_BLOCK = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-tc-1
Signal quality: HIGH

=== KEY FINDINGS ===

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
[SA-TECH-TC-1]
  Ticker: NVDA
  Direction: long
  Setup type: catalyst
  Catalyst/driver: Earnings beat expected to drive momentum
  Time horizon: 24-72h
  Conviction sketch: high with Strong earnings trajectory
  Key risk: Macro deterioration
"""


def _make_tc_brief(**field_overrides: str | None) -> str:
    return _mutate_brief(_TC_BLOCK, **field_overrides)


def test_tc_missing_ticker_raises_parse_error() -> None:
    broken = _make_tc_brief(Ticker=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_direction_raises_parse_error() -> None:
    broken = _make_tc_brief(Direction=None)
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_invalid_direction_raises_parse_error() -> None:
    broken = _make_tc_brief(Direction="sideways")
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_setup_type_raises_parse_error() -> None:
    broken = _make_tc_brief(**{"Setup type": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_catalyst_raises_parse_error() -> None:
    broken = _make_tc_brief(**{"Catalyst/driver": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_time_horizon_raises_parse_error() -> None:
    broken = _make_tc_brief(**{"Time horizon": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_conviction_sketch_raises_parse_error() -> None:
    broken = _make_tc_brief(**{"Conviction sketch": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


def test_tc_missing_key_risk_raises_parse_error() -> None:
    broken = _make_tc_brief(**{"Key risk": None})
    with pytest.raises(ParseError):
        parse_brief(broken, Sector.TECH_SEMIS)


# ---------------------------------------------------------------------------
# Reference ID prefix validation
# ---------------------------------------------------------------------------


def test_wrong_sector_finding_id_raises_parse_error() -> None:
    """SA-FIN-1 in a TECH brief is a ParseError."""
    brief_text = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-wrong-1
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-FIN-1] Wrong sector finding
  Tickers: NVDA
  Signal type: price_action
  Strength: strong
  Detail: This finding has wrong sector prefix.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    with pytest.raises(ParseError):
        parse_brief(brief_text, Sector.TECH_SEMIS)


def test_wrong_sector_anomaly_id_raises_parse_error() -> None:
    brief_text = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-wrong-2
Signal quality: HIGH

=== KEY FINDINGS ===

=== FLAGGED ANOMALIES ===
[SA-FIN-ANOM-1] Wrong sector anomaly
  Anomaly type: volume
  Tickers: NVDA
  Severity: investigate_now
  Suggested question: Why is this wrong sector?

=== THESIS CANDIDATES ===
"""
    with pytest.raises(ParseError):
        parse_brief(brief_text, Sector.TECH_SEMIS)


def test_wrong_sector_tc_id_raises_parse_error() -> None:
    brief_text = """\
SECTOR BRIEF: Tech & Semis
Invocation: inv-wrong-3
Signal quality: HIGH

=== KEY FINDINGS ===

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
[SA-FIN-TC-1]
  Ticker: NVDA
  Direction: long
  Setup type: catalyst
  Catalyst/driver: Wrong sector thesis
  Time horizon: 24-72h
  Conviction sketch: high with Wrong prefix
  Key risk: Test risk
"""
    with pytest.raises(ParseError):
        parse_brief(brief_text, Sector.TECH_SEMIS)


# ---------------------------------------------------------------------------
# Sector label mismatch
# ---------------------------------------------------------------------------


def test_sector_label_mismatch_raises_parse_error() -> None:
    """Brief says 'Financials' but we pass Sector.TECH_SEMIS."""
    brief_text = """\
SECTOR BRIEF: Financials
Invocation: inv-mismatch-1
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-FIN-1] Some financials finding
  Tickers: JPM
  Signal type: fundamental
  Strength: moderate
  Detail: Banks performing well in current rate environment.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    with pytest.raises(ParseError):
        parse_brief(brief_text, Sector.TECH_SEMIS)


def test_correct_sector_label_financials() -> None:
    """Brief with 'Financials' label parses correctly with Sector.FINANCIALS."""
    brief_text = """\
SECTOR BRIEF: Financials
Invocation: inv-fin-1
Signal quality: HIGH

=== KEY FINDINGS ===
[SA-FIN-1] Banking sector strength
  Tickers: JPM, GS
  Signal type: fundamental
  Strength: moderate
  Detail: Net interest margins expanding with current rate environment.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    brief = parse_brief(brief_text, Sector.FINANCIALS)
    assert brief.sector == Sector.FINANCIALS
    assert brief.findings[0].finding_id == "SA-FIN-1"


def test_correct_sector_label_energy() -> None:
    """Brief with 'Energy' label parses correctly with Sector.ENERGY."""
    brief_text = """\
SECTOR BRIEF: Energy
Invocation: inv-enrg-1
Signal quality: LOW

=== KEY FINDINGS ===
[SA-ENERGY-1] Crude oil supply disruption risk
  Tickers: XOM, CVX
  Signal type: cross_asset
  Strength: moderate
  Detail: OPEC+ production cut signals bullish crude outlook.

=== FLAGGED ANOMALIES ===

=== THESIS CANDIDATES ===
"""
    brief = parse_brief(brief_text, Sector.ENERGY)
    assert brief.sector == Sector.ENERGY
    assert brief.findings[0].finding_id == "SA-ENERGY-1"
