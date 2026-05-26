"""Tests for analysis-layer replay short-circuit — story ALP-694.

When ``context.debug_e2e.resume_context`` is populated and a phase is in
``resume_context.phases_to_replay``, ``run_analysis_pipeline`` skips the
agent's runner, loads the typed result from
``<source-archive>/phase_outputs/<phase>.json``, copies the per-agent
diagnostic directory into the new invocation's archive, and emits
``phase_start`` + ``phase_done(..., replayed_from=<source-id>)`` on the
progress emitter.

Coverage:

* replay-from-``adaptive`` (replays 4 phases: domain x 3 + qualitative)
* replay-from-``synthesizer`` (replays 5 phases: domain x 3 + qualitative + adaptive)
* replay-from-``qualitative`` (no SDK upstreams → no-op replay path; runner
  runs unchanged)
* ``resume_context is None`` byte-identical baseline behaviour
* 3-domain-researcher atomic-replay invariant — a mixed
  ``phases_to_replay`` containing only some sectors raises
* skipped-phase typed-result equality vs ``from_domain(...).to_domain()`` round-trip
* diagnostic-dir recursive copy carries every file
* ``replayed_from`` field appears on ``phase_done`` event payload

All underlying runners are monkeypatched so tests do not touch SQLite,
the filesystem, or the Anthropic SDK — only the replay short-circuit
reads/writes files we set up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
from alphamind.analysis.adaptive_research.models import (
    AdaptiveBrief,
    AdaptiveResearcherResultModel,
)
from alphamind.analysis.adaptive_research.runner import AdaptiveResearcherResult
from alphamind.analysis.domain_researchers.input_bundle import (
    InputBundle as DomainInputBundle,
)
from alphamind.analysis.domain_researchers.models import (
    SECTOR_PREFIX,
    DomainResearcherOutputModel,
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
    QualitativeResearcherResultModel,
    SentimentSnapshot,
    ThreadDirection,
    TimeHorizon,
)
from alphamind.analysis.qualitative_research.news_digest import NewsDigest
from alphamind.analysis.qualitative_research.runner import QualitativeResearcherResult
from alphamind.analysis.synthesizer.models import BriefSource, SynthesizerResultModel
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
from alphamind.scheduler.debug_e2e.phase_outputs import write_phase_output

# ---------------------------------------------------------------------------
# Shared fixture values
# ---------------------------------------------------------------------------

_INVOCATION_ID = "test-inv-resume-target"
_SOURCE_INVOCATION_ID = "test-inv-resume-source"
_AS_OF = datetime(2026, 5, 3, 14, 30, 0, tzinfo=UTC)
_LAST_INVOCATION_TIME = datetime(2026, 5, 3, 12, 30, 0, tzinfo=UTC)
_REGIME_LABEL_VALUE = "vol_expansion"


# ---------------------------------------------------------------------------
# Result builders — mirror tests/pipeline/test_analysis_phase_emission.py shapes
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
# Recording progress emitter — captures every phase_start / phase_done call
# so tests can assert event order + kwarg payloads.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingProgress:
    """Captures phase_start / phase_done / agent_request / agent_response calls."""

    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def phase_start(self, phase: str) -> None:
        self.events.append(("phase_start", phase, {}))

    def phase_done(self, phase: str, **fields: Any) -> None:
        self.events.append(("phase_done", phase, dict(fields)))

    def agent_request(self, **fields: Any) -> None:
        self.events.append(("agent_request", "", dict(fields)))

    def agent_response(self, **fields: Any) -> None:
        self.events.append(("agent_response", "", dict(fields)))


# ---------------------------------------------------------------------------
# Stub patches — track which runners are invoked
# ---------------------------------------------------------------------------


@dataclass
class _RunnerCallTracker:
    """Tracks which pipeline runners were invoked during a single run."""

    distillation: int = 0
    domain: int = 0
    qualitative: int = 0
    adaptive: int = 0
    synthesizer: int = 0


def _patch_all_runners(
    monkeypatch: pytest.MonkeyPatch,
    tracker: _RunnerCallTracker | None = None,
) -> _RunnerCallTracker:
    """Monkeypatch all five pipeline runners to return fixtures without I/O."""
    if tracker is None:
        tracker = _RunnerCallTracker()

    async def _stub_distillation(*_args: Any, **_kw: Any) -> DistillationOutputs:
        tracker.distillation += 1
        return _distillation_outputs()

    async def _stub_domain(**_kw: Any) -> DomainResearchersOutput:
        tracker.domain += 1
        return _domain_output()

    async def _stub_qualitative(*_args: Any, **_kw: Any) -> QualitativeResearcherResult:
        tracker.qualitative += 1
        return _qualitative_result()

    async def _stub_adaptive(*_args: Any, **_kw: Any) -> AdaptiveResearcherResult:
        tracker.adaptive += 1
        return _adaptive_result()

    async def _stub_synthesizer(**_kw: Any) -> SynthesizerResult:
        tracker.synthesizer += 1
        return _synth_result()

    monkeypatch.setattr(composition, "run_external_distillation", _stub_distillation)
    monkeypatch.setattr(composition, "run_domain_researchers", _stub_domain)
    monkeypatch.setattr(composition, "run_qualitative_researcher", _stub_qualitative)
    monkeypatch.setattr(composition, "run_adaptive_researcher", _stub_adaptive)
    monkeypatch.setattr(composition, "run_synthesizer", _stub_synthesizer)
    return tracker


# ---------------------------------------------------------------------------
# Source-archive setup — write the 6 phase-output JSON files + diagnostic dirs
# so the replay short-circuit has something to read.
# ---------------------------------------------------------------------------


# Map phase name → per-agent diagnostic-directory name (under <archive>/invocations/<id>/analysis/)
_DIAG_DIR_NAME: dict[str, str] = {
    "tech_semis": AgentName.tech_semis_researcher.value,
    "financials": AgentName.financials_researcher.value,
    "energy": AgentName.energy_researcher.value,
    "qualitative": AgentName.qualitative_researcher.value,
    "adaptive": AgentName.adaptive_researcher.value,
    "synthesizer": AgentName.synthesizer.value,
}


def _seed_source_archive(archive_root: Path, source_invocation_id: str) -> Path:
    """Populate <archive>/invocations/<source-id>/ with phase outputs + diagnostic dirs.

    Writes the 6 phase-output JSON files via the canonical
    :func:`write_phase_output` (which the loader/reader path goes through),
    and creates per-agent diagnostic directories under
    ``analysis/<agent>/`` carrying the canonical 5 file shape
    (``prompt.md``, ``user_message.md``, ``response_initial.md``,
    ``metadata.json``, ``sdk_trace.jsonl``).

    Returns the source archive directory.
    """
    source_dir = archive_root / "invocations" / source_invocation_id

    domain_output = _domain_output()
    write_phase_output(
        archive_dir=source_dir,
        phase="tech_semis",
        model=DomainResearcherOutputModel.from_domain(domain_output.tech_semis),
    )
    write_phase_output(
        archive_dir=source_dir,
        phase="financials",
        model=DomainResearcherOutputModel.from_domain(domain_output.financials),
    )
    write_phase_output(
        archive_dir=source_dir,
        phase="energy",
        model=DomainResearcherOutputModel.from_domain(domain_output.energy),
    )
    write_phase_output(
        archive_dir=source_dir,
        phase="qualitative",
        model=QualitativeResearcherResultModel.from_domain(_qualitative_result()),
    )
    write_phase_output(
        archive_dir=source_dir,
        phase="adaptive",
        model=AdaptiveResearcherResultModel.from_domain(_adaptive_result()),
    )
    write_phase_output(
        archive_dir=source_dir,
        phase="synthesizer",
        model=SynthesizerResultModel.from_domain(_synth_result()),
    )

    # Per-agent diagnostic dirs. The 6 phases map to 6 agent directories.
    for phase, agent_dir_name in _DIAG_DIR_NAME.items():
        diag_dir = source_dir / "analysis" / agent_dir_name
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "prompt.md").write_text(f"PROMPT for {phase}", encoding="utf-8")
        (diag_dir / "user_message.md").write_text(f"USER MSG for {phase}", encoding="utf-8")
        (diag_dir / "response_initial.md").write_text(
            f"RESPONSE for {phase}", encoding="utf-8"
        )
        (diag_dir / "metadata.json").write_text(
            f'{{"phase": "{phase}", "model": "sonnet"}}', encoding="utf-8"
        )
        (diag_dir / "sdk_trace.jsonl").write_text(
            f'{{"event": "trace", "phase": "{phase}"}}\n', encoding="utf-8"
        )

    return source_dir


# ---------------------------------------------------------------------------
# Resume-context fixture builder — uses a real ResumeContext via load_resume_context
# (the test rig owns the source archive; the pipeline reads context off
# the opaque debug_e2e bundle).
# ---------------------------------------------------------------------------


def _make_debug_e2e(
    *,
    archive_root: Path | None,
    resume_context: Any | None,
) -> Any:
    """Return a duck-typed debug-e2e object exposing only ``resume_context``.

    The pipeline reads ``debug_e2e.resume_context`` via attribute access; it
    never imports the real ``DebugE2ESettings`` (per import-linter contract).
    A simple namespace mirroring just the relevant attribute is enough.
    """
    if archive_root is None and resume_context is None:
        return None
    return MagicMock(resume_context=resume_context)


def _drive(
    *,
    archive_root: Path | None,
    monkeypatch: pytest.MonkeyPatch,
    resume_context: Any | None = None,
    progress: Any | None = None,
    tracker: _RunnerCallTracker | None = None,
) -> _RunnerCallTracker:
    """Run the pipeline with stubbed runners, returning the call tracker."""
    tracker = _patch_all_runners(monkeypatch, tracker)
    if progress is None:
        from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER

        progress = NOOP_PROGRESS_EMITTER
    debug_e2e = _make_debug_e2e(archive_root=archive_root, resume_context=resume_context)
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
            progress=progress,
            debug_e2e=debug_e2e,
        )
    )
    return tracker


# ===========================================================================
# Test 1 — tracer bullet: replay-from-adaptive skips 4 phases, runs distillation
# + adaptive + synthesizer.
# ===========================================================================


class TestReplayFromAdaptive:
    """Resume from ``adaptive`` replays 3 sectors + qualitative; runs adaptive + synthesizer."""

    def test_replays_domain_and_qualitative_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 4 upstream phases of ``adaptive`` are loaded from disk, not run.

        Replay set for adaptive = {tech_semis, financials, energy, qualitative}.
        After the run: domain runner + qualitative runner each invoked 0
        times; adaptive runner + synthesizer runner each invoked once;
        distillation always runs (deterministic, never replayed).
        """
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="adaptive",
        )

        tracker = _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
        )

        assert tracker.distillation == 1
        assert tracker.domain == 0
        assert tracker.qualitative == 0
        assert tracker.adaptive == 1
        assert tracker.synthesizer == 1


# ---------------------------------------------------------------------------
# Helper: build a real ResumeContext via the loader so the test exercises
# the same code path the orchestrator does.
# ---------------------------------------------------------------------------


def _make_resume_context(*, source_archive_dir: Path, resume_phase: str) -> Any:
    """Build a real ``ResumeContext`` for the test rig.

    Uses the actual loader path so the test exercises the same data shape
    the orchestrator constructs at CLI time. The loader walks
    ``<archive_root>/invocations/<id>`` so we recover the archive root /
    invocation id from the source path.
    """
    from alphamind.scheduler.debug_e2e.resume import load_resume_context

    archive_root = source_archive_dir.parent.parent
    invocation_id = source_archive_dir.name
    return load_resume_context(
        archive_root=archive_root,
        invocation_id=invocation_id,
        phase=resume_phase,
    )
