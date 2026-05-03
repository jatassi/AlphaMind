"""Tests for adaptive-research anomaly-stream loaders — ALP-256.

Each loader is a pure function over typed upstream objects; tests construct
``DistillationOutputs`` and ``SectorBrief`` fixtures in-memory (no DB session).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis._shared import AnomalySeverity, Sector, SignalQuality
from alphamind.analysis.adaptive_research.loaders import (
    AdaptiveAnomalyInputs,
    DistillationAnomalyRecord,
    SectorAnomalyRecord,
    assemble_adaptive_anomaly_inputs,
    extract_distillation_anomalies,
    extract_sector_anomalies,
)
from alphamind.analysis.domain_researchers.models import (
    Anomaly,
    AnomalyType,
    SectorBrief,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _block(
    block_id: str,
    *,
    flags: tuple[AnomalyFlag, ...],
    freshness_ts: datetime,
    regime_context: str | None = None,
    audience: frozenset[OutputAudience] = frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
    payload: Mapping[str, Any] | None = None,
) -> OutputBlock:
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload=payload or {"x": 1},
        anomaly_flags=flags,
        regime_context=regime_context,
    )


def _outputs(blocks: tuple[OutputBlock, ...]) -> DistillationOutputs:
    """Construct a minimal DistillationOutputs for loader tests.

    The loader only reads ``all_blocks``; the other fields are
    set to inert placeholders.
    """
    return DistillationOutputs(
        sector_outputs={},
        correlation_regime_brief=CorrelationRegimeBrief(
            text="", reference_index={}, freshness_min=_AS_OF
        ),
        universal_regime_label={},
        invocation_id="inv-test",
        as_of=_AS_OF,
        total_blocks=len(blocks),
        total_anomalies=sum(len(b.anomaly_flags) for b in blocks),
        bootstrap_block_count=0,
        all_blocks=blocks,
    )


def _sector_brief(
    sector: Sector,
    *,
    anomalies: tuple[Anomaly, ...],
) -> SectorBrief:
    return SectorBrief(
        invocation_id="inv-test",
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(),
        anomalies=anomalies,
        thesis_candidates=(),
    )


# ---------------------------------------------------------------------------
# Tracer bullet: imports + types resolve
# ---------------------------------------------------------------------------


class TestImports:
    def test_all_public_names_importable(self) -> None:
        from alphamind.analysis.adaptive_research import loaders

        for name in (
            "AdaptiveAnomalyInputs",
            "DistillationAnomalyRecord",
            "SectorAnomalyRecord",
            "assemble_adaptive_anomaly_inputs",
            "extract_distillation_anomalies",
            "extract_sector_anomalies",
        ):
            assert hasattr(loaders, name), f"{name} missing from loaders module"

    def test_dunder_all_lists_exact_public_names(self) -> None:
        from alphamind.analysis.adaptive_research import loaders

        assert set(loaders.__all__) == {
            "AdaptiveAnomalyInputs",
            "DistillationAnomalyRecord",
            "SectorAnomalyRecord",
            "assemble_adaptive_anomaly_inputs",
            "extract_distillation_anomalies",
            "extract_sector_anomalies",
        }


# ---------------------------------------------------------------------------
# extract_distillation_anomalies
# ---------------------------------------------------------------------------


class TestExtractDistillationAnomalies:
    def test_empty_all_blocks_returns_empty_tuple(self) -> None:
        outputs = _outputs(())
        assert extract_distillation_anomalies(outputs) == ()

    def test_blocks_with_no_flags_return_empty_tuple(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        outputs = _outputs(
            (
                _block("q1.volume_spike", flags=(), freshness_ts=ts),
                _block("q3.skew", flags=(), freshness_ts=ts),
            )
        )
        assert extract_distillation_anomalies(outputs) == ()

    def test_flatten_in_block_major_then_flag_order(self) -> None:
        """Two blocks with two flags each → 4 records; block-major then flag order."""
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        b1_flags = (
            AnomalyFlag(name="vol_spike", magnitude=3.2, severity="investigate_now"),
            AnomalyFlag(name="vol_persist", magnitude=2.1, severity="investigate_if_persists"),
        )
        b2_flags = (
            AnomalyFlag(name="skew_left", magnitude=1.8, severity="note_for_context"),
            AnomalyFlag(name="skew_persist", magnitude=2.5, severity="investigate_if_persists"),
        )
        outputs = _outputs(
            (
                _block("q1.volume_spike", flags=b1_flags, freshness_ts=ts),
                _block("q3.skew", flags=b2_flags, freshness_ts=ts),
            )
        )
        records = extract_distillation_anomalies(outputs)
        assert len(records) == 4
        assert [(r.block_id, r.flag_name) for r in records] == [
            ("q1.volume_spike", "vol_spike"),
            ("q1.volume_spike", "vol_persist"),
            ("q3.skew", "skew_left"),
            ("q3.skew", "skew_persist"),
        ]

    def test_record_count_equals_total_flag_count(self) -> None:
        """N blocks carrying M total flags → exactly M records of the typed shape."""
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        # 3 blocks: 2 flags, 0 flags, 3 flags → 5 records.
        b1_flags = (
            AnomalyFlag(name="f1", magnitude=1.0, severity="note_for_context"),
            AnomalyFlag(name="f2", magnitude=2.0, severity="investigate_now"),
        )
        b3_flags = (
            AnomalyFlag(name="g1", magnitude=1.0, severity="note_for_context"),
            AnomalyFlag(name="g2", magnitude=2.0, severity="investigate_now"),
            AnomalyFlag(name="g3", magnitude=3.0, severity="investigate_if_persists"),
        )
        outputs = _outputs(
            (
                _block("q1.a", flags=b1_flags, freshness_ts=ts),
                _block("q1.b", flags=(), freshness_ts=ts),
                _block("q1.c", flags=b3_flags, freshness_ts=ts),
            )
        )
        records = extract_distillation_anomalies(outputs)
        assert len(records) == 5
        assert all(isinstance(r, DistillationAnomalyRecord) for r in records)

    def test_preserves_regime_context_when_present_or_none(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        flags = (AnomalyFlag(name="f", magnitude=1.0, severity="note_for_context"),)
        outputs = _outputs(
            (
                _block(
                    "q1.with_regime",
                    flags=flags,
                    freshness_ts=ts,
                    regime_context="vol-expansion",
                ),
                _block(
                    "q1.no_regime",
                    flags=flags,
                    freshness_ts=ts,
                    regime_context=None,
                ),
            )
        )
        records = extract_distillation_anomalies(outputs)
        assert records[0].regime_context == "vol-expansion"
        assert records[1].regime_context is None

    def test_preserves_freshness_ts(self) -> None:
        ts1 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        ts2 = datetime(2026, 5, 1, 11, 30, tzinfo=UTC)
        flags = (AnomalyFlag(name="f", magnitude=1.0, severity="note_for_context"),)
        outputs = _outputs(
            (
                _block("q1.early", flags=flags, freshness_ts=ts1),
                _block("q1.late", flags=flags, freshness_ts=ts2),
            )
        )
        records = extract_distillation_anomalies(outputs)
        assert records[0].freshness_ts == ts1
        assert records[1].freshness_ts == ts2

    def test_preserves_magnitude_and_severity(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        flags = (
            AnomalyFlag(name="a", magnitude=3.14, severity="investigate_now"),
            AnomalyFlag(name="b", magnitude=-2.5, severity="note_for_context"),
        )
        outputs = _outputs((_block("q1.x", flags=flags, freshness_ts=ts),))
        records = extract_distillation_anomalies(outputs)
        assert records[0].magnitude == pytest.approx(3.14)
        assert records[0].severity == "investigate_now"
        assert records[1].magnitude == pytest.approx(-2.5)
        assert records[1].severity == "note_for_context"


# ---------------------------------------------------------------------------
# extract_sector_anomalies
# ---------------------------------------------------------------------------


def _anomaly(
    anomaly_id: str,
    *,
    description: str = "Sample anomaly description.",
    anomaly_type: AnomalyType = AnomalyType.VOLUME,
    tickers: tuple[str, ...] = ("NVDA",),
    severity: AnomalySeverity = "investigate_now",
    suggested_question: str = "What drove this?",
) -> Anomaly:
    return Anomaly(
        anomaly_id=anomaly_id,
        description=description,
        anomaly_type=anomaly_type,
        tickers=tickers,
        severity=severity,
        suggested_question=suggested_question,
    )


class TestExtractSectorAnomalies:
    def test_empty_briefs_returns_empty_tuple(self) -> None:
        assert extract_sector_anomalies(()) == ()

    def test_briefs_with_no_anomalies_return_empty_tuple(self) -> None:
        briefs = (
            _sector_brief(Sector.TECH_SEMIS, anomalies=()),
            _sector_brief(Sector.FINANCIALS, anomalies=()),
        )
        assert extract_sector_anomalies(briefs) == ()

    def test_three_briefs_with_total_n_anomalies_yields_n_records(self) -> None:
        briefs = (
            _sector_brief(
                Sector.TECH_SEMIS,
                anomalies=(_anomaly("SA-TECH-ANOM-1"), _anomaly("SA-TECH-ANOM-2")),
            ),
            _sector_brief(
                Sector.FINANCIALS,
                anomalies=(_anomaly("SA-FIN-ANOM-1", tickers=("JPM",)),),
            ),
            _sector_brief(
                Sector.ENERGY,
                anomalies=(
                    _anomaly("SA-ENERGY-ANOM-1", tickers=("XOM", "CVX")),
                    _anomaly("SA-ENERGY-ANOM-2", tickers=()),
                ),
            ),
        )
        records = extract_sector_anomalies(briefs)
        assert len(records) == 5
        assert all(isinstance(r, SectorAnomalyRecord) for r in records)
        assert [r.anomaly_id for r in records] == [
            "SA-TECH-ANOM-1",
            "SA-TECH-ANOM-2",
            "SA-FIN-ANOM-1",
            "SA-ENERGY-ANOM-1",
            "SA-ENERGY-ANOM-2",
        ]

    def test_preserves_anomaly_id_tickers_severity_suggested_question(self) -> None:
        anomaly = _anomaly(
            "SA-TECH-ANOM-7",
            description="Volume spike with no price move.",
            anomaly_type=AnomalyType.VOLUME,
            tickers=("NVDA", "AMD"),
            severity="investigate_if_persists",
            suggested_question="What news drove the volume spike?",
        )
        records = extract_sector_anomalies(
            (_sector_brief(Sector.TECH_SEMIS, anomalies=(anomaly,)),)
        )
        assert len(records) == 1
        rec = records[0]
        assert rec.anomaly_id == "SA-TECH-ANOM-7"
        assert rec.tickers == ("NVDA", "AMD")
        assert rec.severity == "investigate_if_persists"
        assert rec.suggested_question == "What news drove the volume spike?"
        assert rec.description == "Volume spike with no price move."

    def test_anomaly_type_passes_through_as_string_value(self) -> None:
        for at in AnomalyType:
            anomaly = _anomaly("SA-TECH-ANOM-1", anomaly_type=at)
            records = extract_sector_anomalies(
                (_sector_brief(Sector.TECH_SEMIS, anomalies=(anomaly,)),)
            )
            assert records[0].anomaly_type == at.value
            assert isinstance(records[0].anomaly_type, str)

    def test_sector_field_derived_from_origin_brief(self) -> None:
        briefs = (
            _sector_brief(Sector.TECH_SEMIS, anomalies=(_anomaly("SA-TECH-ANOM-1"),)),
            _sector_brief(
                Sector.FINANCIALS,
                anomalies=(_anomaly("SA-FIN-ANOM-1", tickers=("JPM",)),),
            ),
            _sector_brief(
                Sector.ENERGY,
                anomalies=(_anomaly("SA-ENERGY-ANOM-1", tickers=("XOM",)),),
            ),
        )
        records = extract_sector_anomalies(briefs)
        assert records[0].sector is Sector.TECH_SEMIS
        assert records[1].sector is Sector.FINANCIALS
        assert records[2].sector is Sector.ENERGY

    def test_empty_tickers_preserved(self) -> None:
        """Sector-wide anomalies (empty tickers tuple) pass through unchanged."""
        anomaly = _anomaly("SA-ENERGY-ANOM-1", tickers=())
        records = extract_sector_anomalies((_sector_brief(Sector.ENERGY, anomalies=(anomaly,)),))
        assert records[0].tickers == ()


# ---------------------------------------------------------------------------
# assemble_adaptive_anomaly_inputs
# ---------------------------------------------------------------------------


class TestAssembleAdaptiveAnomalyInputs:
    def test_returns_unmodified_extractor_outputs(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        flags = (AnomalyFlag(name="f", magnitude=1.0, severity="note_for_context"),)
        outputs = _outputs(
            (
                _block("q1.a", flags=flags, freshness_ts=ts),
                _block("q1.b", flags=flags, freshness_ts=ts),
            )
        )
        briefs = (
            _sector_brief(
                Sector.TECH_SEMIS,
                anomalies=(_anomaly("SA-TECH-ANOM-1"), _anomaly("SA-TECH-ANOM-2")),
            ),
        )
        result = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs,
            sector_briefs=briefs,
            as_of=_AS_OF,
        )
        assert result.distillation == extract_distillation_anomalies(outputs)
        assert result.sector == extract_sector_anomalies(briefs)

    def test_data_freshness_is_min_distillation_ts_when_records_present(self) -> None:
        early = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
        mid = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        late = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        flags = (AnomalyFlag(name="f", magnitude=1.0, severity="note_for_context"),)
        outputs = _outputs(
            (
                _block("q1.late", flags=flags, freshness_ts=late),
                _block("q1.early", flags=flags, freshness_ts=early),
                _block("q1.mid", flags=flags, freshness_ts=mid),
            )
        )
        result = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs,
            sector_briefs=(),
            as_of=_AS_OF,
        )
        assert result.data_freshness == early

    def test_data_freshness_falls_through_to_as_of_when_distillation_empty(
        self,
    ) -> None:
        outputs = _outputs(())  # no blocks → no distillation records
        briefs = (_sector_brief(Sector.TECH_SEMIS, anomalies=(_anomaly("SA-TECH-ANOM-1"),)),)
        result = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs,
            sector_briefs=briefs,
            as_of=_AS_OF,
        )
        assert result.data_freshness == _AS_OF
        # Sanity: sector stream is non-empty here, but its presence
        # must not affect data_freshness — sector briefs have no
        # freshness timestamps.
        assert len(result.sector) == 1

    def test_data_freshness_falls_through_when_blocks_have_no_flags(self) -> None:
        """A block with zero flags contributes no distillation records."""
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        outputs = _outputs((_block("q1.a", flags=(), freshness_ts=ts),))
        result = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs,
            sector_briefs=(),
            as_of=_AS_OF,
        )
        assert result.data_freshness == _AS_OF

    def test_returns_adaptive_anomaly_inputs_instance(self) -> None:
        result = assemble_adaptive_anomaly_inputs(
            distillation_outputs=_outputs(()),
            sector_briefs=(),
            as_of=_AS_OF,
        )
        assert isinstance(result, AdaptiveAnomalyInputs)


# ---------------------------------------------------------------------------
# Purity / determinism
# ---------------------------------------------------------------------------


class TestPurity:
    def test_extract_distillation_anomalies_is_deterministic(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        flags = (
            AnomalyFlag(name="a", magnitude=1.0, severity="investigate_now"),
            AnomalyFlag(name="b", magnitude=2.0, severity="note_for_context"),
        )
        outputs = _outputs(
            (
                _block("q1.x", flags=flags, freshness_ts=ts, regime_context="r1"),
                _block("q1.y", flags=flags, freshness_ts=ts),
            )
        )
        first = extract_distillation_anomalies(outputs)
        second = extract_distillation_anomalies(outputs)
        assert first == second

    def test_extract_sector_anomalies_is_deterministic(self) -> None:
        briefs = (
            _sector_brief(
                Sector.TECH_SEMIS,
                anomalies=(_anomaly("SA-TECH-ANOM-1"), _anomaly("SA-TECH-ANOM-2")),
            ),
        )
        first = extract_sector_anomalies(briefs)
        second = extract_sector_anomalies(briefs)
        assert first == second

    def test_assemble_adaptive_anomaly_inputs_is_deterministic(self) -> None:
        ts = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        flags = (AnomalyFlag(name="f", magnitude=1.0, severity="note_for_context"),)
        outputs = _outputs((_block("q1.a", flags=flags, freshness_ts=ts),))
        briefs = (_sector_brief(Sector.TECH_SEMIS, anomalies=(_anomaly("SA-TECH-ANOM-1"),)),)
        first = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs, sector_briefs=briefs, as_of=_AS_OF
        )
        second = assemble_adaptive_anomaly_inputs(
            distillation_outputs=outputs, sector_briefs=briefs, as_of=_AS_OF
        )
        assert first == second
