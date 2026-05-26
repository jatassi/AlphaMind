"""Analysis-layer pipeline composition wiring — story ALP-276.

The single ``async`` entry point a per-invocation pipeline calls to run
distillation through synthesizer in one shot. Sequences:

1. :func:`run_external_distillation` — produces :class:`DistillationOutputs`.
2. :func:`run_domain_researchers` (three sectors fan out internally) and
   :func:`run_qualitative_researcher` — independent inputs, run in parallel
   under :class:`asyncio.TaskGroup` per the fail-closed policy in
   ``docs/design/llm-agent-failure-handling.md``.
3. :func:`run_adaptive_researcher` — consumes the typed sector / qualitative
   / correlation-regime briefs and the universal regime label.
4. :func:`run_synthesizer` — reads every upstream brief and produces the
   synthesis text plus the populated :class:`RetrievalStore` the
   decision-layer agents will query.

Per-trigger budget overrides from ``config/run_types/<trigger>.yaml``
(:attr:`ResolvedConfig.agent_overrides`) reach every analysis-layer agent
via :func:`apply_agent_overrides` — the helper folds the override fields
onto each :class:`BaseAgentConfig` via Pydantic ``model_copy(update=...)``
and produces the string-keyed mapping the runners consume.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pydantic
from sqlalchemy.orm import Session

from alphamind._kernel.atomic_io import atomic_write_text
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis.adaptive_research.runner import (
    AdaptiveResearcherResult,
    run_adaptive_researcher,
)
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.domain_researchers.orchestrator import (
    DomainResearchersOutput,
    run_domain_researchers,
)
from alphamind.analysis.qualitative_research.runner import (
    QualitativeResearcherResult,
    run_qualitative_researcher,
)
from alphamind.analysis.synthesizer.runner import (
    SynthesizerResult,
    run_synthesizer,
)
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.orchestrator import (
    DistillationOutputs,
    run_external_distillation,
)
from alphamind.pipeline._shared import apply_agent_overrides
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerPortfolioStateReader,
)

__all__ = [
    "AnalysisPipelineResult",
    "apply_agent_overrides",
    "run_analysis_pipeline",
]


# ---------------------------------------------------------------------------
# Phase-output emission helper (ALP-691)
# ---------------------------------------------------------------------------


def _emit_phase_output(
    *,
    archive_root: Path,
    invocation_id: str,
    phase: str,
    model: pydantic.BaseModel,
) -> None:
    """Atomically write *model* to ``<archive_root>/invocations/<id>/phase_outputs/<phase>.json``.

    Uses :func:`alphamind._kernel.atomic_io.atomic_write_text` for the
    staging-and-rename guarantee. The ``phase_outputs/`` subdirectory is
    created by ``atomic_write_text`` if absent (it calls
    ``target.parent.mkdir(parents=True, exist_ok=True)``).

    This helper mirrors :func:`alphamind.scheduler.debug_e2e.phase_outputs.write_phase_output`
    but lives here to respect the import-linter layering rule that forbids
    ``alphamind.pipeline`` from importing ``alphamind.scheduler``.
    """
    target = archive_root / "invocations" / invocation_id / "phase_outputs" / f"{phase}.json"
    atomic_write_text(target, model.model_dump_json())


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisPipelineResult:
    """Bundled return value of :func:`run_analysis_pipeline`.

    Carries every typed result the composition produced so the
    decision-layer wiring (separate story) can read them directly without
    re-running any stage.
    """

    distillation_outputs: DistillationOutputs
    domain_researchers_output: DomainResearchersOutput
    qualitative_result: QualitativeResearcherResult
    adaptive_result: AdaptiveResearcherResult
    synthesizer_result: SynthesizerResult


# ---------------------------------------------------------------------------
# Composition runner
# ---------------------------------------------------------------------------


def _extract_regime_label(distillation_outputs: DistillationOutputs) -> str:
    """Extract the regime-label string from the universal regime payload.

    The distillation orchestrator always populates ``regime_label`` on
    :attr:`DistillationOutputs.universal_regime_label` with one of the
    four :class:`RegimeLabel` values (see
    :func:`alphamind.distillation.regime.assemble_regime_block`); the
    cast is defensive against a type-erased dict access.
    """
    label = distillation_outputs.universal_regime_label["regime_label"]
    return str(label)


async def run_analysis_pipeline(  # noqa: PLR0913 — composition surface threads typed inputs through every stage plus ALP-497 progress + ALP-691 debug_e2e
    *,
    session: Session,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    distillation_config: DistillationConfig,
    ticker_scope: Sequence[str],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    portfolio_reader: SynthesizerPortfolioStateReader,
    archive_root: Path | None = None,
    provenance_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    debug_e2e: object | None = None,
) -> AnalysisPipelineResult:
    """Run the distillation → analysis-layer composition end-to-end.

    Sequences ``run_external_distillation`` → (``run_domain_researchers``
    + ``run_qualitative_researcher``) parallel → ``run_adaptive_researcher``
    → ``run_synthesizer`` and returns every typed ``*Result`` value.

    ``agents_config`` must already carry per-trigger overrides applied via
    :func:`apply_agent_overrides`; the synthesizer slot is forwarded to
    :func:`run_synthesizer` as its ``agent_config`` argument.

    Per the fail-closed policy in ``docs/design/llm-agent-failure-handling.md``,
    any failure in any stage propagates immediately. The parallel stage
    runs under :class:`asyncio.TaskGroup`, which cancels the in-flight
    sibling when one of the parallel branches raises and re-raises the
    failures inside a ``BaseExceptionGroup``; we unwrap the first child so
    callers see the same exception type they did under ``asyncio.gather``.

    ``progress`` is the per-invocation event sink (story ALP-495). The
    runner emits ``phase_start`` / ``phase_done`` around each phase and
    threads the same emitter into every harness call so the single
    ``agent_request`` / ``agent_response`` emit point inside
    :func:`alphamind.analysis._harness_core.invoke_sdk` records each SDK
    call. Defaults to a no-op emitter for production callers that don't
    set ``--debug-e2e``.
    """
    # Phase-output emission is active when both conditions hold (ALP-691):
    # archive_root is set AND debug_e2e is not None (i.e. a debug-e2e invocation).
    # Production daemon callers leave debug_e2e=None so no files are ever written.
    _emit = archive_root is not None and debug_e2e is not None

    # Project the Pydantic ``DistillationConfig`` boundary type onto its
    # frozen-dataclass mirror (ALP-471) before the orchestrator runs — the
    # orchestrator's compute path consumes the dataclass form.
    progress.phase_start("distillation")
    distillation_outputs = await run_external_distillation(
        session,
        distillation_config.to_domain(),
        ticker_scope,
        as_of,
        invocation_id,
        archive_root=archive_root,
        provenance_root=provenance_root,
    )
    progress.phase_done("distillation")

    progress.phase_start("domain_researchers")
    progress.phase_start("qualitative")
    try:
        async with asyncio.TaskGroup() as tg:
            domain_task = tg.create_task(
                run_domain_researchers(
                    invocation_id=invocation_id,
                    as_of=as_of,
                    distillation_outputs=distillation_outputs,
                    session=session,
                    agents_config=agents_config,
                    sectors_config=sectors_config,
                    archive_root=archive_root,
                    progress=progress,
                    phase="domain_researchers",
                )
            )
            qualitative_task = tg.create_task(
                run_qualitative_researcher(
                    invocation_id,
                    as_of,
                    last_invocation_time,
                    session=session,
                    universal_regime_label=distillation_outputs.universal_regime_label,
                    universe=universe,
                    agents_config=agents_config,
                    archive_root=archive_root,
                    progress=progress,
                    phase="qualitative",
                )
            )
    except BaseExceptionGroup as eg:
        # Preserve the prior ``asyncio.gather`` API: callers see the first
        # failure unchanged. The group is attached as ``__cause__`` so
        # concurrent failures remain visible in diagnostics.
        first = eg.exceptions[0]
        raise first from eg

    domain_researchers_output = domain_task.result()
    qualitative_result = qualitative_task.result()

    # Emit per-sector phase outputs (ALP-691): 3 files for the 3 domain
    # researchers. Emission lands BEFORE the matching ``phase_done`` events
    # so a consumer that subscribes to ``progress.jsonl`` can rely on the
    # file being on disk by the time it sees the event.
    if _emit:
        assert archive_root is not None  # narrowed by _emit guard above
        from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel
        from alphamind.analysis.qualitative_research.models import (
            QualitativeResearcherResultModel,
        )

        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="tech_semis",
            model=DomainResearcherOutputModel.from_domain(domain_researchers_output.tech_semis),
        )
        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="financials",
            model=DomainResearcherOutputModel.from_domain(domain_researchers_output.financials),
        )
        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="energy",
            model=DomainResearcherOutputModel.from_domain(domain_researchers_output.energy),
        )
        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="qualitative",
            model=QualitativeResearcherResultModel.from_domain(qualitative_result),
        )

    progress.phase_done("domain_researchers")
    progress.phase_done("qualitative")

    sector_briefs: tuple[SectorBrief, ...] = (
        domain_researchers_output.tech_semis.brief,
        domain_researchers_output.financials.brief,
        domain_researchers_output.energy.brief,
    )

    progress.phase_start("adaptive")
    adaptive_result = await run_adaptive_researcher(
        invocation_id,
        as_of,
        session=session,
        distillation_outputs=distillation_outputs,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_result.brief,
        correlation_regime_brief=distillation_outputs.correlation_regime_brief,
        universal_regime_label=distillation_outputs.universal_regime_label,
        universe=universe,
        agents_config=agents_config,
        archive_root=archive_root,
        progress=progress,
        phase="adaptive",
    )
    # Emit adaptive phase output (ALP-691). Emission lands BEFORE
    # ``phase_done`` so consumers can rely on file-presence at the event.
    if _emit:
        assert archive_root is not None  # narrowed by _emit guard above
        from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="adaptive",
            model=AdaptiveResearcherResultModel.from_domain(adaptive_result),
        )

    progress.phase_done("adaptive")

    progress.phase_start("synthesizer")
    synthesizer_result = await run_synthesizer(
        regime_label=_extract_regime_label(distillation_outputs),
        sector_briefs=sector_briefs,
        correlation_regime_brief=distillation_outputs.correlation_regime_brief,
        qualitative_brief=qualitative_result.brief,
        adaptive_brief=adaptive_result.brief,
        portfolio_reader=portfolio_reader,
        invocation_id=invocation_id,
        now_utc=as_of,
        archive_root=archive_root,
        agent_config=agents_config.get(AgentName.synthesizer.value),
        progress=progress,
        phase="synthesizer",
    )
    # Emit synthesizer phase output (ALP-691). Emission lands BEFORE
    # ``phase_done`` so consumers can rely on file-presence at the event.
    if _emit:
        assert archive_root is not None  # narrowed by _emit guard above
        from alphamind.analysis.synthesizer.models import SynthesizerResultModel

        _emit_phase_output(
            archive_root=archive_root,
            invocation_id=invocation_id,
            phase="synthesizer",
            model=SynthesizerResultModel.from_domain(synthesizer_result),
        )

    progress.phase_done("synthesizer")

    return AnalysisPipelineResult(
        distillation_outputs=distillation_outputs,
        domain_researchers_output=domain_researchers_output,
        qualitative_result=qualitative_result,
        adaptive_result=adaptive_result,
        synthesizer_result=synthesizer_result,
    )
