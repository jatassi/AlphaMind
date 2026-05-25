"""Adaptive-researcher runner — story 06 (ALP-264).

Composes the anomaly-stream loaders (story 03a), the input-bundle assembler
(story 04b), the harness (story 05), and the tool registry (story 04a) into a
single call surface used by the higher-level pipeline orchestrator.

The runner is ``async`` because the harness is ``async``.  Dependencies are
injected via the private ``_run_adaptive_researcher`` function; the public
``run_adaptive_researcher`` wires the production defaults and delegates.

Per the parent issue's "fail-closed propagation" invariant: any harness
failure aborts the adaptive-researcher invocation; the runner does NOT catch
and degrade.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._sdk_subprocess import invoke_adaptive_researcher_in_subprocess
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.adaptive_research.harness import (  # noqa: F401 — kept for tests that inject the in-process harness
    HarnessSuccess,
    invoke_adaptive_researcher,
)
from alphamind.analysis.adaptive_research.input_bundle import (
    InputBundle,
    assemble_input_bundle,
)
from alphamind.analysis.adaptive_research.loaders import (
    AdaptiveAnomalyInputs,
    assemble_adaptive_anomaly_inputs,
)
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs

__all__ = [
    "AdaptiveResearcherResult",
    "run_adaptive_researcher",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdaptiveResearcherResult:
    """Runner return type — carries the brief and all invocation metadata."""

    brief: AdaptiveBrief
    input_bundle: InputBundle
    anomaly_inputs: AdaptiveAnomalyInputs
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int


# ---------------------------------------------------------------------------
# Dependency bundle (injectable callables grouped for the DI seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """Collects the three injectable callables so ``_run_adaptive_researcher``
    stays under the linter's argument-count threshold."""

    anomaly_assembler: Callable[..., AdaptiveAnomalyInputs]
    bundle_assembler: Callable[..., InputBundle]
    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]


# ---------------------------------------------------------------------------
# Private runner (dependency-injected)
# ---------------------------------------------------------------------------


async def _run_adaptive_researcher(  # noqa: PLR0913 — signature dictated by ALP-264 spec
    *,
    invocation_id: str,
    as_of: datetime,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    deps: _Deps,
    archive_root: Path | None = None,
) -> AdaptiveResearcherResult:
    """Run the adaptive researcher with injected dependencies.

    This is the testable core.  ``run_adaptive_researcher`` delegates here
    with production callables wrapped in a :class:`_Deps` bundle.

    Mirrors qualitative-research's runner: lookup agent_config, load inputs,
    assemble bundle, invoke harness, return result with composed metadata.

    Per the parent issue's fail-closed invariant: HarnessFailure subclasses
    propagate up unchanged. The runner does NOT catch and degrade.
    """
    wall_start = time.monotonic()

    agent_name = AgentName.adaptive_researcher.value
    if agent_name not in agents_config:
        raise ValueError(
            f"Agent name {agent_name!r} is not in agents_config — config drift detected"
        )
    agent_config = agents_config[agent_name]

    anomaly_inputs: AdaptiveAnomalyInputs = deps.anomaly_assembler(
        distillation_outputs=distillation_outputs,
        sector_briefs=sector_briefs,
        as_of=as_of,
    )
    logger.info(
        "anomaly inputs assembled (%d distillation, %d sector)",
        len(anomaly_inputs.distillation),
        len(anomaly_inputs.sector),
    )

    bundle: InputBundle = deps.bundle_assembler(
        invocation_id=invocation_id,
        as_of=as_of,
        regime_label=universal_regime_label,
        anomaly_inputs=anomaly_inputs,
    )
    logger.info("input bundle assembled (length=%d)", len(bundle.bundle_text))

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade.  The pipeline-level orchestrator handles fail-closed semantics.
    harness_result: HarnessSuccess = await deps.harness_fn(
        agent_config=agent_config,
        user_message=bundle.bundle_text,
        invocation_id=invocation_id,
        universe=universe,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        archive_root=archive_root,
    )
    logger.info(
        "harness invoked (retry_count=%d, tokens=%s, tool_calls=%d)",
        harness_result.retry_count,
        harness_result.tokens_used,
        harness_result.tool_calls_used,
    )

    wall_elapsed = time.monotonic() - wall_start
    return AdaptiveResearcherResult(
        brief=harness_result.brief,
        input_bundle=bundle,
        anomaly_inputs=anomaly_inputs,
        tokens_used=harness_result.tokens_used,
        tool_calls_used=harness_result.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
        retry_count=harness_result.retry_count,
    )


# ---------------------------------------------------------------------------
# Public entry point (production defaults injected)
# ---------------------------------------------------------------------------


async def run_adaptive_researcher(  # noqa: PLR0913 — prescribed signature; validator requires three upstream briefs; ALP-497 adds progress/phase kwargs
    invocation_id: str,
    as_of: datetime,
    *,
    session: Session,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "adaptive",
) -> AdaptiveResearcherResult:
    """Invoke the adaptive researcher and return a validated result.

    Wires production defaults: ``anomaly_assembler`` is
    :func:`assemble_adaptive_anomaly_inputs`; ``bundle_assembler`` is
    :func:`assemble_input_bundle`; ``harness_fn`` is
    :func:`invoke_adaptive_researcher_in_subprocess` (the subprocess-
    isolated wrapper added in ALP-650 — the worker opens its own
    ``DATABASE_PATH`` session rather than receiving one across the
    process boundary).

    Any :class:`~alphamind.analysis.adaptive_research.harness.HarnessFailure`
    raised by the harness propagates up unchanged.
    """

    # ``session`` is retained on the public signature for caller-API stability
    # (the in-process harness used to consume it). Post-ALP-650 the subprocess
    # wrapper opens its own ``DATABASE_PATH`` session in the worker, so the
    # parent-supplied session is unused on the production path. The in-tree
    # callers (``pipeline/analysis.py``, ``tests/...``) still construct one,
    # so removing the kwarg would be a breaking change.
    del session  # unused after ALP-650 subprocess migration; kept for API stability

    async def _harness_fn(
        *,
        agent_config: BaseAgentConfig,
        user_message: str,
        invocation_id: str,
        universe: frozenset[str],
        sector_briefs: tuple[SectorBrief, ...],
        qualitative_brief: QualitativeBrief,
        correlation_regime_brief: CorrelationRegimeBrief,
        archive_root: Path | None,
    ) -> HarnessSuccess:
        return await invoke_adaptive_researcher_in_subprocess(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            universe=universe,
            sector_briefs=sector_briefs,
            qualitative_brief=qualitative_brief,
            correlation_regime_brief=correlation_regime_brief,
            archive_root=archive_root,
            progress=progress,
            phase=phase,
        )

    return await _run_adaptive_researcher(
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_outputs=distillation_outputs,
        sector_briefs=sector_briefs,
        qualitative_brief=qualitative_brief,
        correlation_regime_brief=correlation_regime_brief,
        universal_regime_label=universal_regime_label,
        universe=universe,
        agents_config=agents_config,
        deps=_Deps(
            anomaly_assembler=assemble_adaptive_anomaly_inputs,
            bundle_assembler=assemble_input_bundle,
            harness_fn=_harness_fn,
        ),
        archive_root=archive_root,
    )
