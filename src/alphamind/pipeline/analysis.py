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
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pydantic
from sqlalchemy.orm import Session

from alphamind._kernel.archive_layout import invocation_archive_dir
from alphamind._kernel.atomic_io import atomic_write_text
from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._shared import TokensUsed
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
    as_of: datetime,
    phase: str,
    model: pydantic.BaseModel,
) -> None:
    """Atomically write *model* to ``<archive_root>/<YYYY-MM-DD>/<id>/phase_outputs/<phase>.json``.

    Uses :func:`alphamind._kernel.atomic_io.atomic_write_text` for the
    staging-and-rename guarantee. The ``phase_outputs/`` subdirectory is
    created by ``atomic_write_text`` if absent (it calls
    ``target.parent.mkdir(parents=True, exist_ok=True)``).

    This helper mirrors :func:`alphamind.scheduler.debug_e2e.phase_outputs.write_phase_output`
    but lives here to respect the import-linter layering rule that forbids
    ``alphamind.pipeline`` from importing ``alphamind.scheduler``.  Uses the
    date-partitioned canonical layout (ALP-689 followup).
    """
    target = (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        / "phase_outputs"
        / f"{phase}.json"
    )
    atomic_write_text(target, model.model_dump_json())


# ---------------------------------------------------------------------------
# Replay short-circuit helpers (ALP-694)
# ---------------------------------------------------------------------------


def _read_phase_output[ModelT: pydantic.BaseModel](
    *,
    source_archive_dir: Path,
    phase: str,
    model_cls: type[ModelT],
) -> ModelT:
    """Read ``<source_archive_dir>/phase_outputs/<phase>.json`` and validate.

    Mirrors :func:`alphamind.scheduler.debug_e2e.phase_outputs.read_phase_output`
    but lives here to respect the import-linter layering rule that forbids
    ``alphamind.pipeline`` from importing ``alphamind.scheduler``. The path
    composition is identical so the two helpers stay byte-compatible.
    """
    target = source_archive_dir / "phase_outputs" / f"{phase}.json"
    return model_cls.model_validate_json(target.read_text(encoding="utf-8"))


def _replay_analysis_phase[ModelT: pydantic.BaseModel, ResultT](
    *,
    phase: str,
    model_cls: type[ModelT],
    convert: Callable[[ModelT], ResultT],
    source_archive_dir: Path,
    target_archive_dir: Path,
    diagnostic_subpath: str,
    replayed_from: str,
    progress: ProgressEmitter,
) -> ResultT:
    """Replay one analysis phase from a prior invocation's archive.

    Steps:

    1. Emit ``phase_start(phase)`` so the event stream reflects the
       replayed phase at the same position a fresh run would.
    2. Read ``<source>/phase_outputs/<phase>.json`` via
       :func:`_read_phase_output` and validate against ``model_cls``.
    3. Convert the boundary model to the runner's typed result via
       ``convert`` (typically ``model_cls.to_domain``-bound on the instance).
    4. Recursively copy the per-agent diagnostic directory
       (``<source>/<diagnostic_subpath>``) into the target archive
       (``<target>/<diagnostic_subpath>``) so the new invocation carries
       a complete diagnostic record. ``shutil.copytree`` runs with
       ``dirs_exist_ok=False``: if the target subdirectory already
       exists, the replay path has corrupted state and the runner
       raises ``FileExistsError``.
    5. Emit ``phase_done(phase, replayed_from=<source-invocation-id>)``
       so consumers can distinguish replayed phases from fresh ones in
       the JSONL event log.

    Returns the converted runner result so the pipeline's data flow is
    unchanged from a downstream consumer's perspective.
    """
    progress.phase_start(phase)
    model = _read_phase_output(
        source_archive_dir=source_archive_dir, phase=phase, model_cls=model_cls
    )
    result = convert(model)
    # The phase_output file is the load-bearing replay surface (validated
    # by ALP-693's loader); the diagnostic dir is a best-effort copy that
    # may be absent if the original run crashed between the phase_output
    # write and the harness's diagnostic-dir creation. Guard with is_dir
    # to match the decision-side _replay_decision_phase behavior — skip
    # the copy silently rather than raise FileNotFoundError mid-replay,
    # which would leave the target archive in an inconsistent state.
    source_diag = source_archive_dir / diagnostic_subpath
    if source_diag.is_dir():
        shutil.copytree(
            source_diag,
            target_archive_dir / diagnostic_subpath,
            dirs_exist_ok=False,
        )
    progress.phase_done(phase, replayed_from=replayed_from)
    return result


def _replay_domain_researchers(
    *,
    source_archive_dir: Path,
    target_archive_dir: Path,
    replayed_from: str,
    progress: ProgressEmitter,
) -> DomainResearchersOutput:
    """Atomically replay the 3-sector domain-researcher TaskGroup phase.

    Loads three sector ``DomainResearcherResult`` values from the source
    archive's ``phase_outputs/{tech_semis,financials,energy}.json`` files,
    copies each sector's diagnostic directory, emits per-sector
    ``phase_start`` + ``phase_done(..., replayed_from=...)`` events, and
    reassembles a :class:`DomainResearchersOutput` carrying the same
    aggregate token / wall-clock / retry totals the original run produced
    (recovered from the per-sector totals via straight summation, so the
    downstream pipeline sees an identical shape).
    """
    from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

    tech = _replay_analysis_phase(
        phase="tech_semis",
        model_cls=DomainResearcherOutputModel,
        convert=DomainResearcherOutputModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/tech_semis_researcher",
        replayed_from=replayed_from,
        progress=progress,
    )
    fin = _replay_analysis_phase(
        phase="financials",
        model_cls=DomainResearcherOutputModel,
        convert=DomainResearcherOutputModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/financials_researcher",
        replayed_from=replayed_from,
        progress=progress,
    )
    energy = _replay_analysis_phase(
        phase="energy",
        model_cls=DomainResearcherOutputModel,
        convert=DomainResearcherOutputModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/energy_researcher",
        replayed_from=replayed_from,
        progress=progress,
    )
    return DomainResearchersOutput(
        invocation_id=tech.brief.invocation_id,
        as_of=tech.input_bundle.as_of,
        tech_semis=tech,
        financials=fin,
        energy=energy,
        total_tokens_used=TokensUsed(
            input_tokens=tech.tokens_used.input_tokens
            + fin.tokens_used.input_tokens
            + energy.tokens_used.input_tokens,
            output_tokens=tech.tokens_used.output_tokens
            + fin.tokens_used.output_tokens
            + energy.tokens_used.output_tokens,
            cache_read_tokens=tech.tokens_used.cache_read_tokens
            + fin.tokens_used.cache_read_tokens
            + energy.tokens_used.cache_read_tokens,
            cache_write_tokens=tech.tokens_used.cache_write_tokens
            + fin.tokens_used.cache_write_tokens
            + energy.tokens_used.cache_write_tokens,
        ),
        # The three sector runners execute in parallel under a TaskGroup
        # in the fresh-run path; the aggregate wall-clock is the MAX of
        # their durations, not the sum. Summing would inflate replayed
        # invocations' reported wall-clock by ~3x and confuse any latency
        # gate that consumes this field.
        total_wall_clock_seconds=max(
            tech.wall_clock_seconds,
            fin.wall_clock_seconds,
            energy.wall_clock_seconds,
        ),
        total_retry_count=tech.retry_count + fin.retry_count + energy.retry_count,
    )


def _replay_qualitative_researcher(
    *,
    source_archive_dir: Path,
    target_archive_dir: Path,
    replayed_from: str,
    progress: ProgressEmitter,
) -> QualitativeResearcherResult:
    """Replay the qualitative researcher phase."""
    from alphamind.analysis.qualitative_research.models import QualitativeResearcherResultModel

    return _replay_analysis_phase(  # type: ignore[no-any-return]
        phase="qualitative",
        model_cls=QualitativeResearcherResultModel,
        convert=QualitativeResearcherResultModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/qualitative_researcher",
        replayed_from=replayed_from,
        progress=progress,
    )


def _replay_adaptive_researcher(
    *,
    source_archive_dir: Path,
    target_archive_dir: Path,
    replayed_from: str,
    progress: ProgressEmitter,
) -> AdaptiveResearcherResult:
    """Replay the adaptive researcher phase."""
    from alphamind.analysis.adaptive_research.models import AdaptiveResearcherResultModel

    return _replay_analysis_phase(  # type: ignore[no-any-return]
        phase="adaptive",
        model_cls=AdaptiveResearcherResultModel,
        convert=AdaptiveResearcherResultModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/adaptive_researcher",
        replayed_from=replayed_from,
        progress=progress,
    )


def _replay_synthesizer(
    *,
    source_archive_dir: Path,
    target_archive_dir: Path,
    replayed_from: str,
    progress: ProgressEmitter,
) -> SynthesizerResult:
    """Replay the synthesizer phase."""
    from alphamind.analysis.synthesizer.models import SynthesizerResultModel

    return _replay_analysis_phase(  # type: ignore[no-any-return]
        phase="synthesizer",
        model_cls=SynthesizerResultModel,
        convert=SynthesizerResultModel.to_domain,
        source_archive_dir=source_archive_dir,
        target_archive_dir=target_archive_dir,
        diagnostic_subpath="analysis/synthesizer",
        replayed_from=replayed_from,
        progress=progress,
    )


async def _run_domain_and_qualitative_phase(  # noqa: PLR0913 — composition helper threads the parallel-phase wiring
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    distillation_outputs: DistillationOutputs,
    session: Session,
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    archive_root: Path | None,
    progress: ProgressEmitter,
    replay_domain: bool,
    replay_qualitative: bool,
    replay_source_dir: Path | None,
    target_archive_dir: Path | None,
    replay_source_id: str,
    emit: bool,
) -> tuple[DomainResearchersOutput, QualitativeResearcherResult]:
    """Run (or replay) the domain-researchers + qualitative phase.

    Both runners are independent of each other but share an
    :class:`asyncio.TaskGroup` for parallel execution in the non-replay
    case. Carving the four cases (both-replay, domain-replay-only,
    qualitative-replay-only, neither) out of :func:`run_analysis_pipeline`
    keeps the top-level runner's branch count under ruff's complexity
    threshold.

    The four cases differ in:

    * whether the TaskGroup is used (only when neither side replays)
    * which phase outputs are emitted (only fresh-run sides emit)
    * which ``phase_start`` / ``phase_done`` events fire (replay side
      goes through :func:`_replay_analysis_phase`; fresh side wraps the
      runner directly)
    """
    if replay_domain and replay_qualitative:
        assert replay_source_dir is not None
        assert target_archive_dir is not None
        # Emit the aggregate ``domain_researchers`` start/done pair around
        # the per-sector events so consumers of progress.jsonl that key on
        # the aggregate event (matching the fresh-run topology at line
        # ~442) see a consistent stream shape on replay.
        progress.phase_start("domain_researchers")
        domain_output = _replay_domain_researchers(
            source_archive_dir=replay_source_dir,
            target_archive_dir=target_archive_dir,
            replayed_from=replay_source_id,
            progress=progress,
        )
        progress.phase_done("domain_researchers", replayed_from=replay_source_id)
        qualitative_result = _replay_qualitative_researcher(
            source_archive_dir=replay_source_dir,
            target_archive_dir=target_archive_dir,
            replayed_from=replay_source_id,
            progress=progress,
        )
        return domain_output, qualitative_result

    if replay_domain:
        assert replay_source_dir is not None
        assert target_archive_dir is not None
        # Aggregate domain_researchers event for stream-shape consistency
        # with the fresh-run path (line ~442).
        progress.phase_start("domain_researchers")
        progress.phase_start("qualitative")
        domain_output = _replay_domain_researchers(
            source_archive_dir=replay_source_dir,
            target_archive_dir=target_archive_dir,
            replayed_from=replay_source_id,
            progress=progress,
        )
        progress.phase_done("domain_researchers", replayed_from=replay_source_id)
        qualitative_result = await run_qualitative_researcher(
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
        if emit:
            assert archive_root is not None
            _emit_qualitative(archive_root, invocation_id, as_of, qualitative_result)
        progress.phase_done("qualitative")
        return domain_output, qualitative_result

    if replay_qualitative:
        assert replay_source_dir is not None
        assert target_archive_dir is not None
        progress.phase_start("domain_researchers")
        domain_output = await run_domain_researchers(
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
        if emit:
            assert archive_root is not None
            _emit_domain_sectors(archive_root, invocation_id, as_of, domain_output)
        progress.phase_done("domain_researchers")
        qualitative_result = _replay_qualitative_researcher(
            source_archive_dir=replay_source_dir,
            target_archive_dir=target_archive_dir,
            replayed_from=replay_source_id,
            progress=progress,
        )
        return domain_output, qualitative_result

    # Neither replayed: original TaskGroup + emission path, byte-identical
    # to pre-ALP-694 behaviour.
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

    domain_output = domain_task.result()
    qualitative_result = qualitative_task.result()

    if emit:
        assert archive_root is not None
        _emit_domain_sectors(archive_root, invocation_id, as_of, domain_output)
        _emit_qualitative(archive_root, invocation_id, as_of, qualitative_result)

    progress.phase_done("domain_researchers")
    progress.phase_done("qualitative")
    return domain_output, qualitative_result


def _emit_domain_sectors(
    archive_root: Path,
    invocation_id: str,
    as_of: datetime,
    domain_output: DomainResearchersOutput,
) -> None:
    """Emit the 3 sector phase-output files (ALP-691)."""
    from alphamind.analysis.domain_researchers.models import DomainResearcherOutputModel

    _emit_phase_output(
        archive_root=archive_root,
        invocation_id=invocation_id,
        as_of=as_of,
        phase="tech_semis",
        model=DomainResearcherOutputModel.from_domain(domain_output.tech_semis),
    )
    _emit_phase_output(
        archive_root=archive_root,
        invocation_id=invocation_id,
        as_of=as_of,
        phase="financials",
        model=DomainResearcherOutputModel.from_domain(domain_output.financials),
    )
    _emit_phase_output(
        archive_root=archive_root,
        invocation_id=invocation_id,
        as_of=as_of,
        phase="energy",
        model=DomainResearcherOutputModel.from_domain(domain_output.energy),
    )


def _emit_qualitative(
    archive_root: Path,
    invocation_id: str,
    as_of: datetime,
    qualitative_result: QualitativeResearcherResult,
) -> None:
    """Emit the qualitative phase-output file (ALP-691)."""
    from alphamind.analysis.qualitative_research.models import QualitativeResearcherResultModel

    _emit_phase_output(
        archive_root=archive_root,
        invocation_id=invocation_id,
        as_of=as_of,
        phase="qualitative",
        model=QualitativeResearcherResultModel.from_domain(qualitative_result),
    )


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

    # Replay short-circuit (ALP-694) — extract ``resume_context`` off the
    # opaque ``debug_e2e`` bundle. The pipeline cannot import
    # ``scheduler.debug_e2e.resume`` (composition-root layering rule, see
    # import-linter contract ``debug-e2e-forbidden-in-production``) so the
    # context is duck-typed: any object exposing a ``resume_context``
    # attribute (real :class:`ResumeContext` or ``None``) works. ``getattr``
    # defaults to ``None`` so callers that pass a bare sentinel (e.g.
    # ``object()``) for emission-only debug-e2e mode still work.
    #
    # ``_replay`` is the precomputed frozen set of SDK phases to replay.
    # Computing it once here keeps each per-phase gate to a single
    # ``in``-set membership check rather than a fresh attribute walk.
    _resume_context = (
        getattr(cast(Any, debug_e2e), "resume_context", None) if debug_e2e is not None else None
    )
    _replay: frozenset[str] = (
        _resume_context.phases_to_replay if _resume_context is not None else frozenset()
    )
    _replay_source_dir: Path | None = (
        _resume_context.source_archive_dir if _resume_context is not None else None
    )
    _replay_source_id: str = (
        _resume_context.source_archive_dir.name if _resume_context is not None else ""
    )
    _target_archive_dir: Path | None = (
        invocation_archive_dir(archive_root=archive_root, as_of=as_of, invocation_id=invocation_id)
        if archive_root is not None
        else None
    )

    # Replay decisions for the parallel phase (ALP-694). Domain researchers
    # are co-emitted by one TaskGroup, so their replay decision must be
    # atomic — the loader's ``phases_to_replay`` set is either all-3-in
    # or none-in; a partial overlap means callers bypassed the loader and
    # is treated as corrupted state (defense-in-depth).
    #
    # Fail-fast BEFORE distillation runs: distillation is expensive and
    # writes to the target archive, so a partial-sector resume context
    # that surfaces after distillation would leave the target archive
    # half-populated and require operator cleanup before any retry.
    _domain_sectors: frozenset[str] = frozenset({"tech_semis", "financials", "energy"})
    _sectors_in_replay = _replay & _domain_sectors
    if 0 < len(_sectors_in_replay) < len(_domain_sectors):
        msg = (
            "phases_to_replay contains a partial domain-researcher set "
            f"{sorted(_sectors_in_replay)}; the 3 sectors must replay atomically "
            "(all-3-in or none-in). The resume loader never produces such a set."
        )
        raise AssertionError(msg)
    _replay_domain = len(_sectors_in_replay) == len(_domain_sectors)
    _replay_qualitative = "qualitative" in _replay

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
        # Persist each produced anomaly flag as a DISTILLATION_ANOMALY_FLAG
        # activity-log row keyed to this invocation (story 04b / ALP-881). The
        # replay harness calls the orchestrator directly and leaves this off.
        emit_anomaly_flags=True,
    )
    progress.phase_done("distillation")

    domain_researchers_output, qualitative_result = await _run_domain_and_qualitative_phase(
        invocation_id=invocation_id,
        as_of=as_of,
        last_invocation_time=last_invocation_time,
        distillation_outputs=distillation_outputs,
        session=session,
        universe=universe,
        agents_config=agents_config,
        sectors_config=sectors_config,
        archive_root=archive_root,
        progress=progress,
        replay_domain=_replay_domain,
        replay_qualitative=_replay_qualitative,
        replay_source_dir=_replay_source_dir,
        target_archive_dir=_target_archive_dir,
        replay_source_id=_replay_source_id,
        emit=_emit,
    )

    sector_briefs: tuple[SectorBrief, ...] = (
        domain_researchers_output.tech_semis.brief,
        domain_researchers_output.financials.brief,
        domain_researchers_output.energy.brief,
    )

    adaptive_result: AdaptiveResearcherResult
    if "adaptive" in _replay:
        assert _replay_source_dir is not None
        assert _target_archive_dir is not None
        adaptive_result = _replay_adaptive_researcher(
            source_archive_dir=_replay_source_dir,
            target_archive_dir=_target_archive_dir,
            replayed_from=_replay_source_id,
            progress=progress,
        )
    else:
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
                as_of=as_of,
                phase="adaptive",
                model=AdaptiveResearcherResultModel.from_domain(adaptive_result),
            )

        progress.phase_done("adaptive")

    synthesizer_result: SynthesizerResult
    if "synthesizer" in _replay:
        assert _replay_source_dir is not None
        assert _target_archive_dir is not None
        synthesizer_result = _replay_synthesizer(
            source_archive_dir=_replay_source_dir,
            target_archive_dir=_target_archive_dir,
            replayed_from=_replay_source_id,
            progress=progress,
        )
    else:
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
                as_of=as_of,
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
