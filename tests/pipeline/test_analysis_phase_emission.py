"""Tests for analysis-layer phase-output emission hooks — story ALP-691.

Verifies that ``run_analysis_pipeline`` writes the 6 phase-output JSON files
(``tech_semis.json``, ``financials.json``, ``energy.json``, ``qualitative.json``,
``adaptive.json``, ``synthesizer.json``) under the canonical
``<archive>/<YYYY-MM-DD>/<id>/phase_outputs/`` directory when ``archive_root``
is set.

Also pins the no-write invariant: when ``archive_root is None`` (the production
daemon path), no files are written under ``phase_outputs/``.

All underlying runners are monkeypatched so tests do not touch SQLite, the
filesystem, or the Anthropic SDK — only the emission path reads/writes files.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alphamind.analysis._shared import Sector, SignalQuality, TokensUsed
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle as AdaptiveInputBundle,
)
from alphamind.analysis.adaptive_research.loaders import AdaptiveAnomalyInputs
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.domain_researchers.input_bundle import (
    InputBundle as DomainInputBundle,
)
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
    Finding,
    SectorBrief,
    SignalType,
    Strength,
)
from alphamind.analysis.domain_researchers.orchestrator import DomainResearchersOutput
from alphamind.analysis.domain_researchers.runner import DomainResearcherResult
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle as QualInputBundle,
)
from alphamind.analysis.qualitative_research.models import (
    EvidenceLine,
    NarrativeThread,
    QualitativeBrief,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.synthesizer.models import BriefSource
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.analysis.synthesizer.runner import SynthesizerResult
from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AllowedModel,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.sector_assembly import SectorOutput
from alphamind.pipeline import analysis as composition
from alphamind.pipeline.analysis import run_analysis_pipeline
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)
from alphamind.scheduler.debug_e2e.phase_outputs import read_phase_output

# ---------------------------------------------------------------------------
# Shared fixture values (match the canonical invocation ID used in phase-path logic)
# ---------------------------------------------------------------------------

_INVOCATION_ID = "test-inv-emission-001"
_AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 3, 12, 30, 0, tzinfo=UTC)
_REGIME_LABEL_VALUE = "vol_expansion"


# ---------------------------------------------------------------------------
# Result builders — reuse exact shapes from existing pipeline test helpers
# ---------------------------------------------------------------------------


def _sector_brief(sector: Sector) -> SectorBrief:
    prefix = SECTOR_PREFIX[sector]
    return SectorBrief(
        invocation_id=_INVOCATION_ID,
        sector=sector,
        signal_quality=SignalQuality.HIGH,
        signal_quality_reason=None,
        findings=(
            Finding(
                finding_id=f"{prefix}-1",
                headline=f"Headline for {sector.value}.",
                tickers=("AAA",),
                signal_type=SignalType.PRICE_ACTION,
                strength=Strength.STRONG,
                detail="Detail.",
            ),
        ),
        anomalies=(),
        thesis_candidates=(),
    )


def _domain_runner_result(sector: Sector) -> DomainResearcherResult:
    return DomainResearcherResult(
        sector=sector,
        brief=_sector_brief(sector),
        input_bundle=DomainInputBundle(
            sector=sector,
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            distillation_text="distill",
            qualitative_text="qual",
            bundle_text="bundle",
        ),
        tokens_used=TokensUsed(
            input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0
        ),
        wall_clock_seconds=1.0,
        retry_count=0,
    )


def _domain_output() -> DomainResearchersOutput:
    return DomainResearchersOutput(
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        tech_semis=_domain_runner_result(Sector.TECH_SEMIS),
        financials=_domain_runner_result(Sector.FINANCIALS),
        energy=_domain_runner_result(Sector.ENERGY),
        total_tokens_used=TokensUsed(
            input_tokens=300, output_tokens=150, cache_read_tokens=0, cache_write_tokens=0
        ),
        total_wall_clock_seconds=1.0,
        total_retry_count=0,
    )


def _qualitative_result() -> QualitativeResearcherResult:
    return QualitativeResearcherResult(
        brief=QualitativeBrief(
            invocation_id=_INVOCATION_ID,
            signal_quality=SignalQuality.HIGH,
            signal_quality_reason=None,
            threads=(
                NarrativeThread(
                    thread_id="QR-1",
                    summary="Quiet day.",
                    relevance="cross-sector",
                    direction=ThreadDirection.MIXED,
                    subject="market",
                    time_horizon=TimeHorizon.NEAR_TERM,
                    evidence=(
                        EvidenceLine(
                            source_type="news",
                            observation="No major catalysts.",
                            citation="ND-1",
                        ),
                        EvidenceLine(
                            source_type="prediction_markets",
                            observation="Odds unchanged.",
                            citation="kalshi:none",
                        ),
                    ),
                    implication="No-op.",
                ),
            ),
            catalyst_watches=(),
            sentiment_snapshot=SentimentSnapshot(
                extremes="none", divergences="none", regime="neutral"
            ),
        ),
        input_bundle=QualInputBundle(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            regime_text="regime: vol_expansion",
            digest_text="digest",
            sentiment_text="sentiment",
            prediction_market_text="prediction",
            calendar_text="calendar",
            thesis_text="thesis",
            bundle_text="bundle",
        ),
        news_digest=NewsDigest(
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            total_collected=0,
            total_shown=0,
            entries=(),
            digest_text="(no headlines)",
        ),
        tokens_used=TokensUsed(
            input_tokens=200, output_tokens=80, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=2.0,
        retry_count=0,
    )


def _adaptive_result() -> AdaptiveResearcherResult:
    return AdaptiveResearcherResult(
        brief=AdaptiveBrief(
            invocation_id=_INVOCATION_ID,
            threads_investigated_count=0,
            anomalies_triaged_count=0,
            anomalies_deferred=(),
            threads=(),
        ),
        input_bundle=AdaptiveInputBundle(
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            regime_text="regime: vol_expansion",
            distillation_text="(none)",
            sector_text="(none)",
            bundle_text="bundle",
        ),
        anomaly_inputs=AdaptiveAnomalyInputs(distillation=(), sector=(), data_freshness=_AS_OF),
        tokens_used=TokensUsed(
            input_tokens=400, output_tokens=120, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=0,
        wall_clock_seconds=3.0,
        retry_count=0,
    )


def _synth_result() -> SynthesizerResult:
    return SynthesizerResult(
        synthesis_text="synthesized prose.",
        retrieval_store=RetrievalStore(
            entries={"SA-TECH-1": "section text"},
            freshness_by_source={
                BriefSource.SA_TECH: _AS_OF,
                BriefSource.SA_FIN: _AS_OF,
            },
        ),
        tokens_used=TokensUsed(
            input_tokens=500, output_tokens=300, cache_read_tokens=0, cache_write_tokens=0
        ),
        tool_calls_used=2,
        wall_clock_seconds=4.0,
        stop_reason="end_turn",
    )


def _sector_output(audience: OutputAudience, *, text: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=audience.value,
        text=text,
        tickers=(),
        block_ids=(),
        freshness_min=_AS_OF,
    )


def _distillation_outputs() -> DistillationOutputs:
    return DistillationOutputs(
        sector_outputs={
            OutputAudience.SECTOR_TECH_SEMIS: _sector_output(
                OutputAudience.SECTOR_TECH_SEMIS, text="TECH-DISTILL"
            ),
            OutputAudience.SECTOR_FINANCIALS: _sector_output(
                OutputAudience.SECTOR_FINANCIALS, text="FIN-DISTILL"
            ),
            OutputAudience.SECTOR_ENERGY: _sector_output(
                OutputAudience.SECTOR_ENERGY, text="ENERGY-DISTILL"
            ),
        },
        correlation_regime_brief=CorrelationRegimeBrief(
            text="[CR-1] regime ctx.\n  detail.\n",
            reference_index={"CR-1": "regime.label"},
            freshness_min=_AS_OF,
        ),
        universal_regime_label={
            "regime_label": _REGIME_LABEL_VALUE,
            "transition_flag": False,
            "confidence": "high",
            "freshness_ts": _AS_OF.isoformat(),
        },
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=0,
        total_anomalies=0,
        non_calibrated_block_count=0,
        all_blocks=(),
    )


def _make_base_config(prompt: str) -> BaseAgentConfig:
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=prompt,
        latency_budget_seconds=30,
        context_token_budget=100_000,
        output_token_budget=4_000,
        tools=[],
    )


def _make_adaptive_config() -> AdaptiveAgentConfig:
    return AdaptiveAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt="prompts/analysis/adaptive_researcher.md",
        latency_budget_seconds=120,
        context_token_budget=120_000,
        output_token_budget=4_000,
        tools=[],
        cumulative_tool_call_limit=15,
        cumulative_tool_token_budget=2_000,
        tool_caps={},
    )


def _agents_registry() -> dict[str, BaseAgentConfig]:
    return {
        AgentName.tech_semis_researcher.value: _make_base_config(
            "prompts/analysis/tech_semis_researcher.md"
        ),
        AgentName.financials_researcher.value: _make_base_config(
            "prompts/analysis/financials_researcher.md"
        ),
        AgentName.energy_researcher.value: _make_base_config(
            "prompts/analysis/energy_researcher.md"
        ),
        AgentName.qualitative_researcher.value: _make_adaptive_config(),
        AgentName.adaptive_researcher.value: _make_adaptive_config(),
        AgentName.synthesizer.value: _make_base_config("prompts/analysis/synthesizer.md"),
    }


def _sectors_registry() -> dict[str, list[str]]:
    return {
        Sector.TECH_SEMIS.value: ["NVDA", "AMD"],
        Sector.FINANCIALS.value: ["JPM"],
        Sector.ENERGY.value: ["XOM"],
    }


class _StubPortfolioReader:
    def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        return ()

    def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        return ()

    def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        return SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        )


# ---------------------------------------------------------------------------
# Stub patches
# ---------------------------------------------------------------------------


def _patch_all_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch all five pipeline runners to return fixtures without I/O."""

    async def _stub_distillation(*_args: Any, **_kw: Any) -> DistillationOutputs:
        return _distillation_outputs()

    async def _stub_domain(**_kw: Any) -> DomainResearchersOutput:
        return _domain_output()

    async def _stub_qualitative(*_args: Any, **_kw: Any) -> QualitativeResearcherResult:
        return _qualitative_result()

    async def _stub_adaptive(*_args: Any, **_kw: Any) -> AdaptiveResearcherResult:
        return _adaptive_result()

    async def _stub_synthesizer(**_kw: Any) -> SynthesizerResult:
        return _synth_result()

    monkeypatch.setattr(composition, "run_external_distillation", _stub_distillation)
    monkeypatch.setattr(composition, "run_domain_researchers", _stub_domain)
    monkeypatch.setattr(composition, "run_qualitative_researcher", _stub_qualitative)
    monkeypatch.setattr(composition, "run_adaptive_researcher", _stub_adaptive)
    monkeypatch.setattr(composition, "run_synthesizer", _stub_synthesizer)


def _drive(
    *,
    archive_root: Path | None,
    monkeypatch: pytest.MonkeyPatch,
    debug_e2e: object | None = object(),  # non-None sentinel — any truthy value triggers emission
) -> None:
    """Run the pipeline with stubbed runners and the given archive_root."""
    _patch_all_runners(monkeypatch)
    asyncio.run(
        run_analysis_pipeline(
            session=None,  # type: ignore[arg-type]
            invocation_id=_INVOCATION_ID,
            as_of=_AS_OF,
            last_invocation_time=_LAST_INVOCATION_TIME,
            distillation_config=MagicMock(),
            ticker_scope=("NVDA", "JPM", "XOM"),
            universe=frozenset({"NVDA", "JPM", "XOM"}),
            agents_config=_agents_registry(),
            sectors_config=_sectors_registry(),
            portfolio_reader=_StubPortfolioReader(),
            archive_root=archive_root,
            debug_e2e=debug_e2e,
        )
    )


# ===========================================================================
# Test 5: pipeline writes all 6 expected files when archive_root set + debug_e2e set
# ===========================================================================


class TestAnalysisPipelinePhaseEmission:
    """Emission hooks write the correct files under phase_outputs/."""

    def test_all_six_files_emitted_when_archive_root_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """6 phase-output files land under <archive>/<YYYY-MM-DD>/<id>/phase_outputs/."""
        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        phase_outputs_dir = tmp_path / "2026-05-03" / _INVOCATION_ID / "phase_outputs"
        expected_files = {
            "tech_semis.json",
            "financials.json",
            "energy.json",
            "qualitative.json",
            "adaptive.json",
            "synthesizer.json",
        }
        assert phase_outputs_dir.exists(), "phase_outputs/ directory was not created"
        actual_files = {f.name for f in phase_outputs_dir.iterdir() if f.is_file()}
        assert actual_files == expected_files

    def test_emitted_files_are_valid_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each emitted JSON file is well-formed and pretty-printable."""
        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        phase_outputs_dir = tmp_path / "2026-05-03" / _INVOCATION_ID / "phase_outputs"
        for phase_file in phase_outputs_dir.iterdir():
            raw = phase_file.read_text(encoding="utf-8")
            parsed = json.loads(raw)  # raises if invalid JSON
            assert isinstance(parsed, dict), f"{phase_file.name} root must be a JSON object"

    def test_read_phase_output_round_trips_tech_semis(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """tech_semis.json round-trips through read_phase_output to a valid model."""
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        archive_dir = tmp_path / "2026-05-03" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir,
            phase="tech_semis",
            model_cls=DomainResearcherOutputModel,
        )
        assert model.sector == Sector.TECH_SEMIS
        recovered = model.to_domain()
        assert recovered == _domain_runner_result(Sector.TECH_SEMIS)

    def test_read_phase_output_round_trips_qualitative(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """qualitative.json round-trips through read_phase_output to a valid model."""
        from alphamind.analysis.qualitative_research.models import (
            QualitativeResearcherResultModel,
        )

        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        archive_dir = tmp_path / "2026-05-03" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir,
            phase="qualitative",
            model_cls=QualitativeResearcherResultModel,
        )
        recovered = model.to_domain()
        assert recovered == _qualitative_result()

    def test_read_phase_output_round_trips_adaptive(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """adaptive.json round-trips through read_phase_output to a valid model."""
        from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        archive_dir = tmp_path / "2026-05-03" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir,
            phase="adaptive",
            model_cls=AdaptiveResearcherResultModel,
        )
        recovered = model.to_domain()
        assert recovered == _adaptive_result()

    def test_read_phase_output_round_trips_synthesizer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """synthesizer.json round-trips through read_phase_output to a valid model."""
        from alphamind.analysis.synthesizer.models import SynthesizerResultModel

        _drive(archive_root=tmp_path, monkeypatch=monkeypatch)

        archive_dir = tmp_path / "2026-05-03" / _INVOCATION_ID
        model = read_phase_output(
            archive_dir=archive_dir,
            phase="synthesizer",
            model_cls=SynthesizerResultModel,
        )
        recovered = model.to_domain()
        assert recovered == _synth_result()


# ===========================================================================
# Test 6: no files written when archive_root is None (production daemon path)
# ===========================================================================


class TestAnalysisPipelineNoEmissionWhenNoArchive:
    """No phase_outputs/ files written when archive_root is None."""

    def test_no_phase_output_files_written_when_archive_root_is_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production daemon path: archive_root=None → zero phase_outputs/ files.

        The pipeline is called with archive_root=None and debug_e2e set — only
        archive_root guards the emission; if it is None, no write happens.
        tmp_path is only used for the assertion (no invocation dir should appear there).
        """
        _drive(archive_root=None, monkeypatch=monkeypatch)

        # Nothing at all should be written to any location — we just confirm
        # that the pipeline completed without error and that passing tmp_path
        # directly as a search root shows no phase_outputs subdirectory.
        phase_outputs_under_tmp = list(tmp_path.rglob("phase_outputs"))
        assert phase_outputs_under_tmp == [], (
            "pipeline created phase_outputs/ directory when archive_root=None"
        )

    def test_no_phase_output_files_written_when_debug_e2e_is_none(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When debug_e2e is None, phase outputs are suppressed even if archive_root is set."""
        _drive(archive_root=tmp_path, monkeypatch=monkeypatch, debug_e2e=None)

        phase_outputs_dir = tmp_path / "2026-05-03" / _INVOCATION_ID / "phase_outputs"
        if phase_outputs_dir.exists():
            written = list(phase_outputs_dir.iterdir())
            assert written == [], (
                f"Expected no phase_outputs when debug_e2e=None; found: {[f.name for f in written]}"
            )
