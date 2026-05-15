"""Parallel domain-researcher orchestrator — story 11 (ALP-190).

Fans out three per-sector runner calls under :class:`asyncio.TaskGroup`,
propagates the first :class:`HarnessFailure` under fail-closed semantics
(``llm-agent-failure-handling.md`` § "fail closed, not open"), and
aggregates the three :class:`DomainResearcherResult`s into a single
:class:`DomainResearchersOutput` value object the synthesizer consumes
downstream.

``asyncio.TaskGroup`` is the structured-concurrency replacement for
``asyncio.gather``: the first task that raises cancels every sibling and
the group exits via ``BaseExceptionGroup``. The orchestrator unwraps the
group to surface the first child unchanged so callers still see the same
``HarnessFailure`` subclass they did under the prior gather call — the
fail-closed contract is preserved (no partial result code path).

The volatility regime label is not forwarded as a separate argument — it is
already embedded in each ``SectorOutput.text`` slice as a UNIVERSAL_BROADCAST
block (see :mod:`alphamind.analysis.domain_researchers.input_bundle` for
the contract). The orchestrator passes the rendered slice through verbatim.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from alphamind.analysis._shared import _SECTOR_AUDIENCE_MAP, Sector, TokensUsed
from alphamind.analysis.domain_researchers.runner import (
    DomainResearcherResult,
    run_domain_researcher,
)
from alphamind.config.models.agents import BaseAgentConfig
from alphamind.distillation.orchestrator import DistillationOutputs

__all__ = [
    "DomainResearchersOutput",
    "run_domain_researchers",
]

# Deterministic sector roster — fixes the dispatch order so positional
# task lookups line up with the DomainResearchersOutput fields.
_SECTOR_ROSTER: tuple[Sector, ...] = (Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY)


# ---------------------------------------------------------------------------
# Output value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DomainResearchersOutput:
    """Aggregated result of the parallel three-sector orchestrator."""

    invocation_id: str
    as_of: datetime
    tech_semis: DomainResearcherResult
    financials: DomainResearcherResult
    energy: DomainResearcherResult
    total_tokens_used: TokensUsed  # field-wise sum across the three sectors
    total_wall_clock_seconds: float  # max() of the three (parallel execution)
    total_retry_count: int  # sum across the three


# ---------------------------------------------------------------------------
# Private orchestrator (dependency-injected runner)
# ---------------------------------------------------------------------------


_RunnerFn = Callable[..., Awaitable[DomainResearcherResult]]


async def _run_domain_researchers(
    *,
    invocation_id: str,
    as_of: datetime,
    distillation_outputs: DistillationOutputs,
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    runner_fn: _RunnerFn,
    session: Session | None = None,
    archive_root: Path | None = None,
) -> DomainResearchersOutput:
    """Run the three sectors in parallel via *runner_fn* and aggregate.

    The runner is dependency-injected so tests don't touch the SDK.
    Production callers go through :func:`run_domain_researchers`, which
    binds ``runner_fn = run_domain_researcher``.

    Per the fail-closed policy, :class:`asyncio.TaskGroup` cancels every
    sibling task as soon as one raises and re-raises the failures inside a
    ``BaseExceptionGroup``. The orchestrator unwraps the first child so
    callers see the original :class:`HarnessFailure` subclass — it does
    NOT swallow the failure and does NOT return a partial result.
    """
    sector_outputs = distillation_outputs.sector_outputs

    async def _run_one(sector: Sector) -> DomainResearcherResult:
        return await runner_fn(
            sector,
            invocation_id,
            as_of,
            sector_outputs[_SECTOR_AUDIENCE_MAP[sector]].text,
            session=session,
            agents_config=agents_config,
            sectors_config=sectors_config,
            archive_root=archive_root,
        )

    try:
        async with asyncio.TaskGroup() as tg:
            tasks: dict[Sector, asyncio.Task[DomainResearcherResult]] = {
                sector: tg.create_task(_run_one(sector)) for sector in _SECTOR_ROSTER
            }
    except BaseExceptionGroup as eg:
        # Preserve the prior ``asyncio.gather`` API: callers see the
        # first failure unchanged. The group is attached as ``__cause__``
        # via ``raise ... from eg`` so diagnostics still surface every
        # concurrent failure.
        first = eg.exceptions[0]
        raise first from eg

    tech_semis_result = tasks[Sector.TECH_SEMIS].result()
    financials_result = tasks[Sector.FINANCIALS].result()
    energy_result = tasks[Sector.ENERGY].result()
    results = (tech_semis_result, financials_result, energy_result)
    return DomainResearchersOutput(
        invocation_id=invocation_id,
        as_of=as_of,
        tech_semis=tech_semis_result,
        financials=financials_result,
        energy=energy_result,
        total_tokens_used=TokensUsed(
            input_tokens=sum(r.tokens_used.input_tokens for r in results),
            output_tokens=sum(r.tokens_used.output_tokens for r in results),
            cache_read_tokens=sum(r.tokens_used.cache_read_tokens for r in results),
            cache_write_tokens=sum(r.tokens_used.cache_write_tokens for r in results),
        ),
        total_wall_clock_seconds=max(r.wall_clock_seconds for r in results),
        total_retry_count=sum(r.retry_count for r in results),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_domain_researchers(
    *,
    invocation_id: str,
    as_of: datetime,
    distillation_outputs: DistillationOutputs,
    session: Session,
    agents_config: Mapping[str, BaseAgentConfig],
    sectors_config: Mapping[str, list[str]],
    archive_root: Path | None = None,
) -> DomainResearchersOutput:
    """Run all three domain researchers in parallel and aggregate.

    Per the fail-closed policy in
    ``docs/design/llm-agent-failure-handling.md``, any sector failure
    aborts the orchestrator. The first :class:`HarnessFailure` propagates
    immediately; the other coroutines are cancelled.
    """
    return await _run_domain_researchers(
        invocation_id=invocation_id,
        as_of=as_of,
        distillation_outputs=distillation_outputs,
        agents_config=agents_config,
        sectors_config=sectors_config,
        runner_fn=run_domain_researcher,
        session=session,
        archive_root=archive_root,
    )
