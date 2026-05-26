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
    overrides: dict[str, Any] | None = None,
) -> _RunnerCallTracker:
    """Monkeypatch all five pipeline runners to return fixtures without I/O.

    ``overrides`` (optional) maps runner attribute name to a custom async
    callable; the override replaces the default stub for that runner so a
    test can capture kwargs threaded into it.
    """
    if tracker is None:
        tracker = _RunnerCallTracker()
    if overrides is None:
        overrides = {}

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

    defaults = {
        "run_external_distillation": _stub_distillation,
        "run_domain_researchers": _stub_domain,
        "run_qualitative_researcher": _stub_qualitative,
        "run_adaptive_researcher": _stub_adaptive,
        "run_synthesizer": _stub_synthesizer,
    }
    for name, default in defaults.items():
        monkeypatch.setattr(composition, name, overrides.get(name, default))
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
        (diag_dir / "response_initial.md").write_text(f"RESPONSE for {phase}", encoding="utf-8")
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
    runner_overrides: dict[str, Any] | None = None,
) -> _RunnerCallTracker:
    """Run the pipeline with stubbed runners, returning the call tracker."""
    tracker = _patch_all_runners(monkeypatch, tracker, runner_overrides)
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


class TestReplayFromSynthesizer:
    """Resume from ``synthesizer`` replays 5 upstream phases; runs only synthesizer."""

    def test_replays_domain_qualitative_and_adaptive_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replay set for synthesizer = {tech_semis, financials, energy, qualitative, adaptive}.

        After the run: domain / qualitative / adaptive runners invoked 0
        times; synthesizer runner invoked once.
        """
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",
        )

        tracker = _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
        )

        assert tracker.distillation == 1
        assert tracker.domain == 0
        assert tracker.qualitative == 0
        assert tracker.adaptive == 0
        assert tracker.synthesizer == 1


class TestReplayFromQualitative:
    """Resume from ``qualitative`` has no SDK upstreams; runner runs unchanged."""

    def test_qualitative_phases_to_replay_is_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per the DAG, qualitative has no SDK upstream phases.

        ``phases_to_replay("qualitative") == frozenset()``. The runner runs
        the full pipeline unchanged: every runner is invoked once, no
        replay short-circuit fires.
        """
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="qualitative",
        )
        # Sanity-check the loader's DAG closure.
        assert resume_context.phases_to_replay == frozenset()

        tracker = _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
        )

        assert tracker.distillation == 1
        assert tracker.domain == 1
        assert tracker.qualitative == 1
        assert tracker.adaptive == 1
        assert tracker.synthesizer == 1


class TestNoResumeContextIsByteIdenticalToBaseline:
    """When ``resume_context`` is None the runner behaves byte-identically."""

    def test_no_resume_context_runs_every_phase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``resume_context=None`` is the production daemon path: every
        runner is invoked once, no extra IO, no extra events."""
        tracker = _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=None,
        )

        assert tracker.distillation == 1
        assert tracker.domain == 1
        assert tracker.qualitative == 1
        assert tracker.adaptive == 1
        assert tracker.synthesizer == 1

    def test_no_resume_context_writes_no_replay_diagnostic_dirs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No diagnostic dirs are copied when there is no replay path active.

        The fresh runners are stubbed (don't write diagnostic dirs); the only
        possible source of ``analysis/<agent>/`` dirs under tmp_path would be
        the replay copytree. Confirm none exist.
        """
        _drive(archive_root=tmp_path, monkeypatch=monkeypatch, resume_context=None)

        analysis_dirs = list((tmp_path / "invocations" / _INVOCATION_ID).rglob("analysis"))
        assert analysis_dirs == [], (
            f"unexpected analysis/ dirs created without replay: {analysis_dirs}"
        )


class TestSkippedPhasesReturnRoundTripEqualResults:
    """Skipped phases return objects equal to what the runner would have produced.

    The pipeline's downstream consumers (synthesizer's ``sector_briefs``,
    adaptive's ``qualitative_brief``, etc.) cannot tell whether a phase
    was run fresh or hydrated from disk — equality is preserved through
    the ``from_domain(...).to_domain()`` round-trip.
    """

    def test_replayed_qualitative_result_equals_round_trip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """qualitative.json hydrated by the pipeline matches the runner fixture."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="adaptive",  # replays qualitative
        )

        # Capture the hydrated qualitative_brief threaded into the adaptive
        # runner kwargs. _drive sets up the default stubs; we override
        # adaptive after it returns.
        captured: dict[str, Any] = {}

        async def _capture_adaptive(*_args: Any, **kw: Any) -> AdaptiveResearcherResult:
            captured["qualitative_brief"] = kw["qualitative_brief"]
            return _adaptive_result()

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
            runner_overrides={"run_adaptive_researcher": _capture_adaptive},
        )

        expected_brief = _qualitative_result().brief
        assert captured["qualitative_brief"] == expected_brief

    def test_replayed_adaptive_result_equals_round_trip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """adaptive.json hydrated by the pipeline matches the runner fixture."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",  # replays adaptive
        )

        captured: dict[str, Any] = {}

        async def _capture_synth(**kw: Any) -> SynthesizerResult:
            captured["adaptive_brief"] = kw["adaptive_brief"]
            return _synth_result()

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
            runner_overrides={"run_synthesizer": _capture_synth},
        )

        expected_brief = _adaptive_result().brief
        assert captured["adaptive_brief"] == expected_brief

    def test_replayed_domain_sector_briefs_equal_round_trip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The 3 sector briefs hydrated by the pipeline match the runner fixtures."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",  # replays all 3 sectors
        )

        captured: dict[str, Any] = {}

        async def _capture_synth(**kw: Any) -> SynthesizerResult:
            captured["sector_briefs"] = kw["sector_briefs"]
            return _synth_result()

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
            runner_overrides={"run_synthesizer": _capture_synth},
        )

        expected_output = _domain_output()
        expected_briefs = (
            expected_output.tech_semis.brief,
            expected_output.financials.brief,
            expected_output.energy.brief,
        )
        assert captured["sector_briefs"] == expected_briefs


class TestDiagnosticDirCopy:
    """Skipped phases copy the per-agent diagnostic directory recursively."""

    def test_replay_copies_qualitative_diagnostic_dir_with_all_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All 5 files (prompt, user_message, response_initial, metadata, sdk_trace) are copied."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="adaptive",  # replays qualitative
        )

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
        )

        target_qual_dir = (
            tmp_path
            / "invocations"
            / _INVOCATION_ID
            / "analysis"
            / AgentName.qualitative_researcher.value
        )
        assert target_qual_dir.is_dir()
        expected_files = {
            "prompt.md",
            "user_message.md",
            "response_initial.md",
            "metadata.json",
            "sdk_trace.jsonl",
        }
        copied_files = {f.name for f in target_qual_dir.iterdir() if f.is_file()}
        assert copied_files == expected_files
        # File contents preserved byte-for-byte
        assert (target_qual_dir / "prompt.md").read_text() == "PROMPT for qualitative"
        assert (target_qual_dir / "sdk_trace.jsonl").read_text() == (
            '{"event": "trace", "phase": "qualitative"}\n'
        )

    def test_replay_copies_all_three_sector_diagnostic_dirs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Replaying all 3 sectors copies 3 distinct diagnostic directories."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="adaptive",  # replays all 3 sectors
        )

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
        )

        target_analysis_dir = tmp_path / "invocations" / _INVOCATION_ID / "analysis"
        for sector_dir_name in (
            AgentName.tech_semis_researcher.value,
            AgentName.financials_researcher.value,
            AgentName.energy_researcher.value,
        ):
            sector_dir = target_analysis_dir / sector_dir_name
            assert sector_dir.is_dir(), f"{sector_dir_name} not copied"
            assert (sector_dir / "metadata.json").is_file()

    def test_replay_raises_when_target_diagnostic_dir_already_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``dirs_exist_ok=False`` — pre-existing target dir is corrupted state."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        # Pre-create the target adaptive_researcher dir to simulate a stale
        # archive — this should be rejected.
        target_dir = (
            tmp_path
            / "invocations"
            / _INVOCATION_ID
            / "analysis"
            / AgentName.adaptive_researcher.value
        )
        target_dir.mkdir(parents=True)
        (target_dir / "stale.txt").write_text("oops")

        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",  # replays adaptive
        )

        with pytest.raises(FileExistsError):
            _drive(
                archive_root=tmp_path,
                monkeypatch=monkeypatch,
                resume_context=resume_context,
            )


class TestReplayedFromEventField:
    """Skipped phases emit ``phase_done(..., replayed_from=<source-id>)``."""

    def test_phase_done_carries_replayed_from(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``replayed_from`` kwarg surfaces on ``phase_done`` event payload."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",  # replays 5 phases
        )
        progress = _RecordingProgress()

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
            progress=progress,
        )

        replayed_phases = {
            phase
            for kind, phase, payload in progress.events
            if kind == "phase_done" and payload.get("replayed_from") == _SOURCE_INVOCATION_ID
        }
        # ``domain_researchers`` is the aggregate event wrapping the
        # per-sector tech_semis/financials/energy events; it carries
        # ``replayed_from`` too so consumers keying on the aggregate (the
        # fresh-run topology) see a consistent stream shape.
        assert replayed_phases == {
            "domain_researchers",
            "tech_semis",
            "financials",
            "energy",
            "qualitative",
            "adaptive",
        }

    def test_phase_start_emitted_for_each_replayed_phase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each replayed phase emits its own ``phase_start`` before ``phase_done``."""
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)
        resume_context = _make_resume_context(
            source_archive_dir=source_dir,
            resume_phase="synthesizer",
        )
        progress = _RecordingProgress()

        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=resume_context,
            progress=progress,
        )

        starts = [phase for kind, phase, _ in progress.events if kind == "phase_start"]
        # Expect distillation + 5 replayed SDK phases + synthesizer fresh
        assert "tech_semis" in starts
        assert "financials" in starts
        assert "energy" in starts
        assert "qualitative" in starts
        assert "adaptive" in starts
        assert "synthesizer" in starts

    def test_phase_done_without_resume_context_omits_replayed_from(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Production daemon path: no ``replayed_from`` kwarg on any event."""
        progress = _RecordingProgress()
        _drive(
            archive_root=tmp_path,
            monkeypatch=monkeypatch,
            resume_context=None,
            progress=progress,
        )

        for kind, _phase, payload in progress.events:
            if kind == "phase_done":
                assert "replayed_from" not in payload


class TestPartialDomainReplayAssertion:
    """Partial overlap of ``phases_to_replay`` with the 3 sectors is rejected."""

    def test_only_tech_semis_in_phases_to_replay_raises_assertion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crafted ``phases_to_replay`` with one sector triggers AssertionError.

        The real loader never produces this set (the 3 sectors are
        co-emitted by one TaskGroup), but a defense-in-depth assertion
        in the runner catches direct callers that bypass the loader.
        """
        source_dir = _seed_source_archive(tmp_path, _SOURCE_INVOCATION_ID)

        # Build a hand-crafted resume context with a partial domain set.
        @dataclass(frozen=True)
        class _CraftedResumeContext:
            source_archive_dir: Path
            resume_phase: str
            phases_to_replay: frozenset[str]

        crafted = _CraftedResumeContext(
            source_archive_dir=source_dir,
            resume_phase="adaptive",
            phases_to_replay=frozenset({"tech_semis"}),  # partial!
        )

        with pytest.raises(AssertionError, match="partial domain-researcher set"):
            _drive(
                archive_root=tmp_path,
                monkeypatch=monkeypatch,
                resume_context=crafted,
            )


class TestNoHarnessImportsResumeContext:
    """The replay seam is the composition runner only — no harness leak."""

    def test_no_harness_file_imports_resume_context(self) -> None:
        """``git grep -l ResumeContext src/alphamind/analysis/`` returns nothing."""
        import subprocess

        result = subprocess.run(
            ["git", "grep", "-l", "ResumeContext", "src/alphamind/analysis/"],
            capture_output=True,
            text=True,
        )
        # `git grep -l` returns 1 with empty output when nothing matches; 0 with matches.
        assert result.returncode == 1, (
            f"Harness files imported ResumeContext (replay seam leaked): {result.stdout}"
        )
        assert result.stdout.strip() == ""


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
