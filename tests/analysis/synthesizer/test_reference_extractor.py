"""Tests for the reference-marker extractor — story 04 (ALP-202)."""

from __future__ import annotations

import pytest

from alphamind.analysis.synthesizer.reference_extractor import (
    ExtractionError,
    extract_references,
)


def test_basic_extraction() -> None:
    """Three SA-TECH-N references parse into three sections, each containing its header."""
    text = (
        "[SA-TECH-1] First finding.\n"
        "  Detail line.\n"
        "[SA-TECH-2] Second finding.\n"
        "  More detail.\n"
        "[SA-TECH-3] Third finding.\n"
    )
    result = extract_references(text)
    assert set(result) == {"SA-TECH-1", "SA-TECH-2", "SA-TECH-3"}
    assert result["SA-TECH-1"].startswith("[SA-TECH-1]")
    assert "First finding." in result["SA-TECH-1"]
    assert "Detail line." in result["SA-TECH-1"]
    assert result["SA-TECH-2"].startswith("[SA-TECH-2]")
    assert "Second finding." in result["SA-TECH-2"]
    assert "More detail." in result["SA-TECH-2"]
    assert result["SA-TECH-3"].startswith("[SA-TECH-3]")
    assert "Third finding." in result["SA-TECH-3"]


def test_subtype_prefixes_preserved() -> None:
    """Sub-typed prefixes (ANOM, TC, CW) become distinct sections from their base forms."""
    text = (
        "[SA-TECH-1] base finding.\n"
        "[SA-TECH-ANOM-1] anomaly.\n"
        "[SA-TECH-TC-2] thesis candidate.\n"
        "[QR-CW-3] catalyst watch.\n"
    )
    result = extract_references(text)
    assert set(result) == {"SA-TECH-1", "SA-TECH-ANOM-1", "SA-TECH-TC-2", "QR-CW-3"}
    assert result["SA-TECH-1"].startswith("[SA-TECH-1]")
    assert "base finding." in result["SA-TECH-1"]
    assert "anomaly." not in result["SA-TECH-1"]
    assert result["SA-TECH-ANOM-1"].startswith("[SA-TECH-ANOM-1]")
    assert "anomaly." in result["SA-TECH-ANOM-1"]
    assert result["SA-TECH-TC-2"].startswith("[SA-TECH-TC-2]")
    assert "thesis candidate." in result["SA-TECH-TC-2"]
    assert result["QR-CW-3"].startswith("[QR-CW-3]")
    assert "catalyst watch." in result["QR-CW-3"]


def test_mixed_prefixes() -> None:
    """A bundle with one of each base prefix extracts all references."""
    text = (
        "[SA-TECH-1] tech finding.\n"
        "[SA-FIN-1] financials finding.\n"
        "[CR-1] correlation finding.\n"
        "[QR-1] qualitative thread.\n"
        "[AR-1] adaptive thread.\n"
    )
    result = extract_references(text)
    assert set(result) == {"SA-TECH-1", "SA-FIN-1", "CR-1", "QR-1", "AR-1"}
    assert "tech finding." in result["SA-TECH-1"]
    assert "financials finding." in result["SA-FIN-1"]
    assert "correlation finding." in result["CR-1"]
    assert "qualitative thread." in result["QR-1"]
    assert "adaptive thread." in result["AR-1"]


def test_empty_bundle() -> None:
    """An empty brief text returns an empty dict, not an error."""
    assert extract_references("") == {}


def test_duplicate_ref_id() -> None:
    """The same SA-TECH-1 appearing twice raises ExtractionError."""
    text = "[SA-TECH-1] first occurrence.\n  detail.\n[SA-TECH-1] second occurrence.\n"
    with pytest.raises(ExtractionError) as excinfo:
        extract_references(text)
    assert "SA-TECH-1" in str(excinfo.value)
    assert excinfo.value.offending_line == "[SA-TECH-1] second occurrence."


def test_unknown_prefix_raises() -> None:
    """A header [ZZ-FOO-1] (prefix not in ReferencePrefix) raises ExtractionError."""
    text = "[ZZ-FOO-1] some content.\n"
    with pytest.raises(ExtractionError) as excinfo:
        extract_references(text)
    assert "ZZ-FOO-1" in str(excinfo.value)
    assert excinfo.value.offending_line == "[ZZ-FOO-1] some content."


def test_malformed_header_raises() -> None:
    """A line starting with [ but not matching the header regex raises ExtractionError."""
    text = "[not-a-ref-id] content.\n"
    with pytest.raises(ExtractionError) as excinfo:
        extract_references(text)
    assert excinfo.value.offending_line == "[not-a-ref-id] content."


def test_preserves_newlines_and_indentation() -> None:
    """Section text retains internal newlines and leading whitespace until the next header."""
    text = (
        "[SA-TECH-1] header line.\n"
        "  indented detail line.\n"
        "    deeper indent.\n"
        "\n"
        "  blank-line-separated paragraph.\n"
        "[SA-TECH-2] next header.\n"
        "  trailing detail.\n"
    )
    result = extract_references(text)
    expected_first = (
        "[SA-TECH-1] header line.\n"
        "  indented detail line.\n"
        "    deeper indent.\n"
        "\n"
        "  blank-line-separated paragraph.\n"
    )
    assert result["SA-TECH-1"] == expected_first
    assert result["SA-TECH-2"] == "[SA-TECH-2] next header.\n  trailing detail.\n"
