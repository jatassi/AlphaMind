"""Tests for adaptive-researcher input bundle assembler — ALP-262."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.analysis._shared import AnomalySeverity, Sector
from alphamind.analysis.adaptive_research.loaders import (
    AdaptiveAnomalyInputs,
    DistillationAnomalyRecord,
    SectorAnomalyRecord,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "inv-ar-001"


_REGIME_LABEL: dict[str, object] = {
    "regime": "vol_expansion",
    "transition_flag": "early-weak",
    "confidence": 0.82,
    "freshness_ts": "2026-05-01T11:55:00Z",
}


def _make_distillation(
    block_id: str = "q1.volume_spike",
    flag_name: str = "volume_spike_flag",
    magnitude: float = 3.5,
    severity: AnomalySeverity = "investigate_now",
    regime_context: str | None = "elevated vol regime",
    freshness_ts: datetime | None = None,
) -> DistillationAnomalyRecord:
    return DistillationAnomalyRecord(
        block_id=block_id,
        flag_name=flag_name,
        magnitude=magnitude,
        severity=severity,
        regime_context=regime_context,
        freshness_ts=freshness_ts or _AS_OF,
    )


def _make_sector(
    anomaly_id: str = "SA-TECH-ANOM-1",
    description: str = "Unusual NVDA volume",
    anomaly_type: str = "volume_spike",
    tickers: tuple[str, ...] = ("NVDA",),
    severity: AnomalySeverity = "investigate_now",
    suggested_question: str = "What catalyst drove the spike?",
    sector: Sector = Sector.TECH_SEMIS,
) -> SectorAnomalyRecord:
    return SectorAnomalyRecord(
        anomaly_id=anomaly_id,
        description=description,
        anomaly_type=anomaly_type,
        tickers=tickers,
        severity=severity,
        suggested_question=suggested_question,
        sector=sector,
    )


def _make_inputs(
    distillation: tuple[DistillationAnomalyRecord, ...] = (),
    sector: tuple[SectorAnomalyRecord, ...] = (),
) -> AdaptiveAnomalyInputs:
    return AdaptiveAnomalyInputs(
        distillation=distillation,
        sector=sector,
        data_freshness=_AS_OF,
    )


# ---------------------------------------------------------------------------
# Acceptance criterion: imports resolve cleanly
# ---------------------------------------------------------------------------


def test_import_resolves() -> None:
    """InputBundle and assemble_input_bundle import cleanly from the module."""
    import dataclasses

    from alphamind.analysis.adaptive_research.input_bundle import (
        InputBundle,
        assemble_input_bundle,
    )

    assert callable(assemble_input_bundle)
    # ALP-474: InputBundle is now a frozen dataclass, not a Pydantic model.
    assert dataclasses.is_dataclass(InputBundle)


# ---------------------------------------------------------------------------
# Acceptance criterion: bundle_text opens with the header line
# ---------------------------------------------------------------------------


def test_bundle_text_opens_with_header() -> None:
    """bundle_text starts with the literal header marker including invocation id and as_of."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(),
    )
    assert bundle.bundle_text.startswith(
        "=== ADAPTIVE RESEARCH INPUT (invocation inv-ar-001, as_of 2026-05-01T12:00:00Z) ==="
    )


# ---------------------------------------------------------------------------
# Acceptance criterion: exactly four ===-prefixed section markers, in order
# ---------------------------------------------------------------------------


def test_four_section_markers_in_order() -> None:
    """bundle_text contains exactly four ===-prefixed section markers in the documented order."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=(_make_distillation(),), sector=(_make_sector(),)),
    )
    markers = [line for line in bundle.bundle_text.splitlines() if line.startswith("=== ")]
    assert len(markers) == 4
    assert markers[0].startswith("=== ADAPTIVE RESEARCH INPUT")
    assert markers[1].startswith("=== VOLATILITY REGIME")
    assert markers[2].startswith("=== DISTILLATION ANOMALY FLAGS")
    assert markers[3].startswith("=== SECTOR-RESEARCHER ANOMALIES")


# ---------------------------------------------------------------------------
# Acceptance criterion: empty streams render (none) under section markers
# ---------------------------------------------------------------------------


def test_empty_distillation_renders_placeholder() -> None:
    """Empty distillation stream renders the (none) placeholder under the section marker."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=()),
    )
    assert "=== DISTILLATION ANOMALY FLAGS (0 flags) ===\n(none)" in bundle.bundle_text


def test_empty_sector_renders_placeholder() -> None:
    """Empty sector stream renders the (none) placeholder under the section marker."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(sector=()),
    )
    assert "=== SECTOR-RESEARCHER ANOMALIES (0 anomalies) ===\n(none)" in bundle.bundle_text


# ---------------------------------------------------------------------------
# Acceptance criterion: regime_label missing keys render with default, no raise
# ---------------------------------------------------------------------------


def test_regime_missing_confidence_renders_unknown() -> None:
    """A regime_label missing the confidence key renders "Confidence: unknown" without raising."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    partial_regime: dict[str, object] = {
        "regime": "vol_expansion",
        "transition_flag": "early-weak",
        "freshness_ts": "2026-05-01T11:55:00Z",
    }
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=partial_regime,
        anomaly_inputs=_make_inputs(),
    )
    assert "Confidence: unknown" in bundle.regime_text
    assert "Confidence: unknown" in bundle.bundle_text


def test_regime_completely_empty_renders_all_unknown() -> None:
    """An empty regime_label renders all four canonical fields as ``unknown`` without raising."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label={},
        anomaly_inputs=_make_inputs(),
    )
    assert "Regime: unknown" in bundle.regime_text
    assert "Transition: unknown" in bundle.regime_text
    assert "Confidence: unknown" in bundle.regime_text
    assert "Freshness: unknown" in bundle.regime_text


# ---------------------------------------------------------------------------
# Acceptance criterion: each [SA-*-ANOM-*] reference appears literally in sector_text
# ---------------------------------------------------------------------------


def test_sector_anomaly_ids_appear_literally_under_bracketed_header() -> None:
    """Each input SectorAnomalyRecord's anomaly_id appears literally as a bracketed header."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    sector_records = (
        _make_sector(anomaly_id="SA-TECH-ANOM-1", sector=Sector.TECH_SEMIS, tickers=("NVDA",)),
        _make_sector(
            anomaly_id="SA-FIN-ANOM-2",
            sector=Sector.FINANCIALS,
            tickers=("JPM", "GS"),
            description="Bank funding stress",
            anomaly_type="funding_stress",
            suggested_question="Is repo market dislocating?",
        ),
        _make_sector(
            anomaly_id="SA-ENERGY-ANOM-3",
            sector=Sector.ENERGY,
            tickers=(),
            description="Sector-wide spread compression",
            anomaly_type="spread_compression",
            suggested_question="What macro driver compressed spreads?",
        ),
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(sector=sector_records),
    )
    sector_lines = bundle.sector_text.splitlines()
    bracketed_headers = [line for line in sector_lines if line.startswith("[SA-")]
    assert "[SA-TECH-ANOM-1]" in bracketed_headers[0]
    assert "[SA-FIN-ANOM-2]" in bracketed_headers[1]
    assert "[SA-ENERGY-ANOM-3]" in bracketed_headers[2]
    # And literally appears in the bundle text:
    for rec in sector_records:
        assert f"[{rec.anomaly_id}]" in bundle.bundle_text


def test_sector_anomaly_with_no_tickers_renders_none() -> None:
    """Sector-wide anomalies (empty tickers tuple) render the "Tickers: none" placeholder."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(
            sector=(_make_sector(anomaly_id="SA-ENERGY-ANOM-1", tickers=()),),
        ),
    )
    assert "Tickers: none" in bundle.sector_text


# ---------------------------------------------------------------------------
# Acceptance criterion: determinism — two calls with same inputs return byte-equal bundle_text
# ---------------------------------------------------------------------------


def test_assemble_is_deterministic_byte_equal() -> None:
    """Calling assemble_input_bundle twice with the same inputs returns byte-equal bundle_text."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    distillation_records = (
        _make_distillation(
            block_id="q1.volume_spike",
            flag_name="volume_spike_flag",
            magnitude=3.5,
            severity="investigate_now",
            regime_context="elevated vol regime",
            freshness_ts=_AS_OF,
        ),
        _make_distillation(
            block_id="q3.correlation_break",
            flag_name="corr_break_flag",
            magnitude=0.45,
            severity="investigate_if_persists",
            regime_context=None,
            freshness_ts=_AS_OF,
        ),
    )
    sector_records = (
        _make_sector(anomaly_id="SA-TECH-ANOM-1", sector=Sector.TECH_SEMIS),
        _make_sector(anomaly_id="SA-FIN-ANOM-2", sector=Sector.FINANCIALS, tickers=("JPM",)),
    )
    inputs = _make_inputs(distillation=distillation_records, sector=sector_records)

    first = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=inputs,
    )
    second = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=inputs,
    )
    assert first.bundle_text == second.bundle_text
    # Encoded byte equality:
    assert first.bundle_text.encode("utf-8") == second.bundle_text.encode("utf-8")


# ---------------------------------------------------------------------------
# Acceptance criterion: bundle_text equals the documented composition formula.
# The formula is: header + "\n\n" + regime + "\n\n" + dist + "\n\n" + sec + "\n".
# ---------------------------------------------------------------------------


def test_bundle_text_composition_formula() -> None:
    """bundle_text equals header + "\\n\\n" + regime + "\\n\\n" + dist + "\\n\\n" + sec + "\\n"."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(
            distillation=(_make_distillation(),),
            sector=(_make_sector(),),
        ),
    )
    header_text = (
        "=== ADAPTIVE RESEARCH INPUT (invocation inv-ar-001, as_of 2026-05-01T12:00:00Z) ==="
    )
    expected = (
        header_text
        + "\n\n"
        + bundle.regime_text
        + "\n\n"
        + bundle.distillation_text
        + "\n\n"
        + bundle.sector_text
        + "\n"
    )
    assert bundle.bundle_text == expected
    # Single trailing newline (not double):
    assert bundle.bundle_text.endswith("\n")
    assert not bundle.bundle_text.endswith("\n\n")


# ---------------------------------------------------------------------------
# Acceptance criterion: no datetime.now() or other live-clock source
# ---------------------------------------------------------------------------


def test_module_source_invokes_no_live_clock() -> None:
    """No live-clock call (datetime.now/.utcnow/time.time/time.monotonic) in the module source."""
    import inspect

    from alphamind.analysis.adaptive_research import input_bundle

    source = inspect.getsource(input_bundle)
    forbidden = (
        "datetime.now",
        ".now()",
        "datetime.utcnow",
        "utcnow(",
        "time.time(",
        "time.monotonic(",
    )
    for token in forbidden:
        assert token not in source, f"Live-clock token {token!r} present in input_bundle module"


# ---------------------------------------------------------------------------
# Distillation rendering details (documented format from the story)
# ---------------------------------------------------------------------------


def test_distillation_records_indexed_one_based_sequential() -> None:
    """[D-N] indices begin at 1 and increment sequentially in source order."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    records = (
        _make_distillation(block_id="q1.volume_spike"),
        _make_distillation(block_id="q3.correlation_break"),
        _make_distillation(block_id="q6.macro_surprise"),
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=records),
    )
    text = bundle.distillation_text
    d1 = text.index("[D-1]")
    d2 = text.index("[D-2]")
    d3 = text.index("[D-3]")
    assert d1 < d2 < d3
    assert "[D-1] block=q1.volume_spike" in text
    assert "[D-2] block=q3.correlation_break" in text
    assert "[D-3] block=q6.macro_surprise" in text


def test_distillation_magnitude_formatted_two_decimals() -> None:
    """magnitude is formatted as a two-decimal float."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    records = (_make_distillation(magnitude=3.14159),)
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=records),
    )
    assert "magnitude=3.14" in bundle.distillation_text
    assert "magnitude=3.14159" not in bundle.distillation_text


def test_distillation_regime_context_none_renders_none_literal() -> None:
    """A distillation record with regime_context=None renders ``regime_context=none``."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    records = (_make_distillation(regime_context=None),)
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=records),
    )
    assert "regime_context=none" in bundle.distillation_text


def test_distillation_freshness_renders_iso_utc() -> None:
    """The freshness_ts is rendered as an ISO 8601 UTC string."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    freshness = datetime(2026, 5, 1, 9, 30, 15, tzinfo=UTC)
    records = (_make_distillation(freshness_ts=freshness),)
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(distillation=records),
    )
    assert "freshness=2026-05-01T09:30:15Z" in bundle.distillation_text


def test_sector_records_preserve_input_order() -> None:
    """Sector records render in the order they appear in the input tuple, not sorted."""
    from alphamind.analysis.adaptive_research.input_bundle import assemble_input_bundle

    sector_records = (
        _make_sector(anomaly_id="SA-FIN-ANOM-9", sector=Sector.FINANCIALS),
        _make_sector(anomaly_id="SA-TECH-ANOM-1", sector=Sector.TECH_SEMIS),
        _make_sector(anomaly_id="SA-ENERGY-ANOM-5", sector=Sector.ENERGY),
    )
    bundle = assemble_input_bundle(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        regime_label=_REGIME_LABEL,
        anomaly_inputs=_make_inputs(sector=sector_records),
    )
    text = bundle.sector_text
    pos_first = text.index("[SA-FIN-ANOM-9]")
    pos_second = text.index("[SA-TECH-ANOM-1]")
    pos_third = text.index("[SA-ENERGY-ANOM-5]")
    assert pos_first < pos_second < pos_third
