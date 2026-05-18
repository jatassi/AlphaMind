"""Domain-researcher runner — story 10 (ALP-191).

Composes the qualitative input loader (story 06), the input-bundle assembler
(story 08), and the LLM harness (story 07) into a single call surface used by
the parallel orchestrator (story 11).

The runner is ``async`` because the harness is ``async``.  Dependencies are
injected via the private ``_run_domain_researcher`` function; the public
``run_domain_researcher`` wires the production defaults and delegates to it.
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
from alphamind.analysis._sdk_subprocess import invoke_domain_researcher_in_subprocess
from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.domain_researchers.harness import (  # noqa: F401  -- kept for tests that inject the in-process harness
    HarnessSuccess,
    invoke_domain_researcher,
)
from alphamind.analysis.domain_researchers.input_bundle import InputBundle, assemble_input_bundle
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.domain_researchers.qualitative_input import (
    SectorQualitativeInput,
    load_sector_qualitative_input,
)
from alphamind.config.models.agents import AgentName, BaseAgentConfig

__all__ = [
    "DomainResearcherResult",
    "run_domain_researcher",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sector → agent-name mapping
# ---------------------------------------------------------------------------

_AGENT_NAME_BY_SECTOR: Mapping[Sector, str] = {
    Sector.TECH_SEMIS: AgentName.tech_semis_researcher.value,
    Sector.FINANCIALS: AgentName.financials_researcher.value,
    Sector.ENERGY: AgentName.energy_researcher.value,
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainResearcherResult:
    """The runner's return type — carries the brief and all invocation metadata."""

    sector: Sector
    brief: SectorBrief
    input_bundle: InputBundle
    tokens_used: TokensUsed
    wall_clock_seconds: float
    retry_count: int


# ---------------------------------------------------------------------------
# Dependency bundle (injectable callables grouped for the DI seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """Collects the three injectable callables so ``_run_domain_researcher`` stays
    under the linter's argument-count threshold."""

    qualitative_loader: Callable[..., SectorQualitativeInput]
    bundle_assembler: Callable[..., InputBundle]
    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_sectors_config(sectors_config: Mapping[str, list[str]]) -> None:
    """Assert *sectors_config* has an entry for every registered sector.

    Raises :exc:`KeyError` immediately on a missing entry — surfaces
    config gaps before the harness call.  Returns no value: this is a
    side-effect-only precondition check.
    """
    for s in Sector:
        # Indexing raises KeyError on miss — that's the intended signal.
        _ = sectors_config[s.value]


# ---------------------------------------------------------------------------
# Private runner (dependency-injected)
# ---------------------------------------------------------------------------


async def _run_domain_researcher(  # noqa: PLR0913 — internal helper threading runner state plus ALP-497 progress/phase
    *,
    sector: Sector,
    invocation_id: str,
    as_of: datetime,
    distillation_output_text: str,
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    deps: _Deps,
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "domain_researchers",
) -> DomainResearcherResult:
    """Run a domain researcher with injected dependencies.

    This is the testable core.  ``run_domain_researcher`` delegates here with
    production callables wrapped in a :class:`_Deps` bundle.

    Parameters
    ----------
    sector:
        Which sector to research.
    invocation_id:
        Stable identifier for this pipeline invocation.
    as_of:
        Timestamp representing the current market snapshot.
    distillation_output_text:
        The sector-sliced distillation output text.
    agents_config:
        Registry mapping agent-name strings to ``BaseAgentConfig`` instances.
    sectors_config:
        Registry mapping sector-name strings to ticker lists.
    deps:
        Injectable callables — qualitative loader, bundle assembler, harness.
    archive_root:
        Forwarded to the harness for diagnostic-archive writes.  ``None``
        skips archival (e.g. in tests).
    """
    wall_start = time.monotonic()

    # Raises ValueError on config drift — the mapping must cover every registered sector
    agent_name = _AGENT_NAME_BY_SECTOR[sector]
    if agent_name not in agents_config:
        raise ValueError(
            f"Agent name {agent_name!r} for sector {sector!r} is not in agents_config — "
            "config drift detected"
        )
    agent_config = agents_config[agent_name]

    qualitative_input: SectorQualitativeInput = deps.qualitative_loader(
        sector=sector,
        as_of=as_of,
        lookback_window_hours=24,
        max_headlines=30,
    )
    logger.info(
        "[%s] qualitative input loaded (%d headlines)",
        sector,
        len(qualitative_input.headlines),
    )

    bundle: InputBundle = deps.bundle_assembler(
        sector=sector,
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_text=distillation_output_text,
        qualitative_input=qualitative_input,
    )
    logger.info(
        "[%s] input bundle assembled (bundle_text length=%d)",
        sector,
        len(bundle.bundle_text),
    )

    _validate_sectors_config(sectors_config)

    # HarnessFailure propagates up unchanged; the orchestrator handles per fail-closed policy
    harness_result: HarnessSuccess = await deps.harness_fn(
        agent_config=agent_config,
        sector=sector,
        user_message=bundle.bundle_text,
        invocation_id=invocation_id,
        archive_root=archive_root,
        progress=progress,
        phase=phase,
    )
    logger.info(
        "[%s] harness invoked (retry_count=%d, tokens=%s)",
        sector,
        harness_result.retry_count,
        harness_result.tokens_used,
    )

    wall_elapsed = time.monotonic() - wall_start
    result = DomainResearcherResult(
        sector=sector,
        brief=harness_result.brief,
        input_bundle=bundle,
        tokens_used=harness_result.tokens_used,
        wall_clock_seconds=wall_elapsed,
        retry_count=harness_result.retry_count,
    )
    logger.info("[%s] brief validated and result assembled", sector)
    return result


# ---------------------------------------------------------------------------
# Public entry point (production defaults injected)
# ---------------------------------------------------------------------------


async def run_domain_researcher(  # noqa: PLR0913 — public signature plus ALP-497 progress/phase
    sector: Sector,
    invocation_id: str,
    as_of: datetime,
    distillation_output_text: str,
    *,
    session: Session,
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    archive_root: Path | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "domain_researchers",
) -> DomainResearcherResult:
    """Invoke a domain researcher and return a validated ``DomainResearcherResult``.

    This is the public entry point used by the parallel orchestrator (story 11).
    It wires the production callables and delegates to ``_run_domain_researcher``.

    Any :class:`~alphamind.analysis.domain_researchers.harness.HarnessFailure`
    raised by the harness propagates up unchanged — the orchestrator handles
    per fail-closed policy.

    Parameters
    ----------
    sector:
        Which sector to research.
    invocation_id:
        Stable identifier for this pipeline invocation.
    as_of:
        Timestamp representing the current market snapshot.
    distillation_output_text:
        The sector-sliced distillation output text.
    session:
        SQLAlchemy session for the qualitative input loader.
    agents_config:
        Registry mapping agent-name strings to ``BaseAgentConfig`` instances.
    sectors_config:
        Registry mapping sector-name strings to ticker lists.
    archive_root:
        Forwarded to the harness for diagnostic-archive writes.  ``None``
        skips archival.
    """

    def _qualitative_loader(
        *,
        sector: Sector,
        as_of: datetime,
        lookback_window_hours: int,
        max_headlines: int,
    ) -> SectorQualitativeInput:
        return load_sector_qualitative_input(
            session,
            sector,
            as_of,
            lookback_window_hours=lookback_window_hours,
            max_headlines=max_headlines,
        )

    return await _run_domain_researcher(
        sector=sector,
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_output_text=distillation_output_text,
        agents_config=agents_config,
        sectors_config=sectors_config,
        deps=_Deps(
            qualitative_loader=_qualitative_loader,
            bundle_assembler=assemble_input_bundle,
            harness_fn=invoke_domain_researcher_in_subprocess,
        ),
        archive_root=archive_root,
        progress=progress,
        phase=phase,
    )
