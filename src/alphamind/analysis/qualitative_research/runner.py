"""Qualitative-researcher runner — story 06 (ALP-252).

Composes the input loaders (story 03d), the news-digest renderer (story 04a),
the input-bundle assembler (story 05), the harness (story 04b), and the tool
registry (story 03e) into a single call surface used by the higher-level
pipeline orchestrator.

The runner is ``async`` because the harness is ``async``.  Dependencies are
injected via the private ``_run_qualitative_researcher`` function; the public
``run_qualitative_researcher`` wires the production defaults and delegates.

Per the parent issue's "fail-closed propagation" invariant: any harness
failure aborts the qualitative-researcher invocation; the runner does NOT
catch and degrade.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from alphamind.analysis._shared import Sector, TokensUsed
from alphamind.analysis.qualitative_research.harness import (
    HarnessSuccess,
    invoke_qualitative_researcher,
)
from alphamind.analysis.qualitative_research.input_bundle import (
    InputBundle,
    assemble_input_bundle,
)
from alphamind.analysis.qualitative_research.loaders import (
    QualitativeInputs,
    load_qualitative_inputs,
)
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.qualitative_research.news_digest import NewsDigest, render_news_digest
from alphamind.config.models.agents import AgentName, BaseAgentConfig
from alphamind.distillation.sector_assembly import (
    DOMAIN_RESEARCHER_BY_AUDIENCE,
    load_sector_roster,
)

__all__ = [
    "QualitativeResearcherResult",
    "run_qualitative_researcher",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class QualitativeResearcherResult(BaseModel, frozen=True):
    """The runner's return type — carries the brief and all invocation metadata."""

    brief: QualitativeBrief
    input_bundle: InputBundle
    news_digest: NewsDigest
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    retry_count: int


# ---------------------------------------------------------------------------
# Dependency bundle (injectable callables grouped for the DI seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """Collects the four injectable callables so ``_run_qualitative_researcher``
    stays under the linter's argument-count threshold."""

    inputs_loader: Callable[..., QualitativeInputs]
    digest_renderer: Callable[..., NewsDigest]
    bundle_assembler: Callable[..., InputBundle]
    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]


# ---------------------------------------------------------------------------
# Private runner (dependency-injected)
# ---------------------------------------------------------------------------


async def _run_qualitative_researcher(  # noqa: PLR0913 — signature dictated by ALP-252 spec
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    universal_regime_label: dict[str, Any],
    sector_roster: Mapping[Sector, frozenset[str]],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    deps: _Deps,
    archive_root: Path | None = None,
) -> QualitativeResearcherResult:
    """Run the qualitative researcher with injected dependencies.

    This is the testable core.  ``run_qualitative_researcher`` delegates here
    with production callables wrapped in a :class:`_Deps` bundle.

    Parameters
    ----------
    invocation_id:
        Stable identifier for this pipeline invocation.
    as_of:
        Timestamp representing the current market snapshot.
    last_invocation_time:
        Prior invocation's ``as_of``; bounds the news-digest window.
    universal_regime_label:
        Regime payload from the prior pipeline stage (the distillation
        orchestrator's :attr:`DistillationOutputs.universal_regime_label`).
    sector_roster:
        Per-sector ticker rosters consumed by the news-digest renderer.
    universe:
        The full asset-universe ticker set forwarded to the harness for
        catalyst-watch ticker validation.
    agents_config:
        Registry mapping agent-name strings to ``BaseAgentConfig`` instances.
    deps:
        Injectable callables — inputs loader, digest renderer, bundle
        assembler, harness.
    archive_root:
        Forwarded to the harness for diagnostic-archive writes.  ``None``
        skips archival (e.g. in tests).
    """
    wall_start = time.monotonic()

    agent_name = AgentName.qualitative_researcher.value
    if agent_name not in agents_config:
        raise ValueError(
            f"Agent name {agent_name!r} is not in agents_config — config drift detected"
        )
    agent_config = agents_config[agent_name]

    inputs: QualitativeInputs = deps.inputs_loader(as_of=as_of)
    logger.info(
        "qualitative inputs loaded (%d sentiment, %d events)",
        len(inputs.sentiment_aggregates),
        len(inputs.events),
    )

    news_digest: NewsDigest = deps.digest_renderer(
        invocation_id=invocation_id,
        as_of=as_of,
        last_invocation_time=last_invocation_time,
        sector_roster=sector_roster,
    )
    logger.info(
        "news digest rendered (%d entries, %d collected)",
        news_digest.total_shown,
        news_digest.total_collected,
    )

    bundle: InputBundle = deps.bundle_assembler(
        invocation_id=invocation_id,
        as_of=as_of,
        regime_label=universal_regime_label,
        digest=news_digest,
        inputs=inputs,
    )
    logger.info("input bundle assembled (length=%d)", len(bundle.bundle_text))

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade.  The pipeline-level orchestrator handles fail-closed semantics.
    harness_result: HarnessSuccess = await deps.harness_fn(
        agent_config=agent_config,
        user_message=bundle.bundle_text,
        invocation_id=invocation_id,
        universe=universe,
        archive_root=archive_root,
    )
    logger.info(
        "harness invoked (retry_count=%d, tokens=%s, tool_calls=%d)",
        harness_result.retry_count,
        harness_result.tokens_used,
        harness_result.tool_calls_used,
    )

    wall_elapsed = time.monotonic() - wall_start
    return QualitativeResearcherResult(
        brief=harness_result.brief,
        input_bundle=bundle,
        news_digest=news_digest,
        tokens_used=harness_result.tokens_used,
        tool_calls_used=harness_result.tool_calls_used,
        wall_clock_seconds=wall_elapsed,
        retry_count=harness_result.retry_count,
    )


# ---------------------------------------------------------------------------
# Public entry point (production defaults injected)
# ---------------------------------------------------------------------------


async def run_qualitative_researcher(
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    *,
    session: Session,
    universal_regime_label: dict[str, Any],
    universe: frozenset[str],
    agents_config: Mapping[str, BaseAgentConfig],
    archive_root: Path | None = None,
) -> QualitativeResearcherResult:
    """Invoke the qualitative researcher and return a validated result.

    Wires production defaults: ``inputs_loader`` is :func:`load_qualitative_inputs`
    bound to ``session``, ``digest_renderer`` is :func:`render_news_digest` bound
    to ``session``, ``bundle_assembler`` is :func:`assemble_input_bundle`,
    ``harness_fn`` is :func:`invoke_qualitative_researcher` bound to ``session``
    and ``universe``.  Loads the per-sector ``sector_roster`` from the
    distillation module once and forwards to the digest renderer.

    Any :class:`~alphamind.analysis.qualitative_research.harness.HarnessFailure`
    raised by the harness propagates up unchanged.
    """
    # `Sector` values match `DOMAIN_RESEARCHER_BY_AUDIENCE` values by design
    # (see `_shared.py`'s assertion); iterate the values to build the roster.
    sector_roster: dict[Sector, frozenset[str]] = {
        Sector(domain_researcher): frozenset(load_sector_roster(session, domain_researcher))
        for domain_researcher in DOMAIN_RESEARCHER_BY_AUDIENCE.values()
    }

    def _inputs_loader(*, as_of: datetime) -> QualitativeInputs:
        return load_qualitative_inputs(session, as_of=as_of)

    def _digest_renderer(
        *,
        invocation_id: str,
        as_of: datetime,
        last_invocation_time: datetime,
        sector_roster: Mapping[Sector, frozenset[str]],
    ) -> NewsDigest:
        return render_news_digest(
            session,
            invocation_id=invocation_id,
            as_of=as_of,
            last_invocation_time=last_invocation_time,
            sector_roster=sector_roster,
        )

    async def _harness_fn(
        *,
        agent_config: BaseAgentConfig,
        user_message: str,
        invocation_id: str,
        universe: frozenset[str],
        archive_root: Path | None,
    ) -> HarnessSuccess:
        return await invoke_qualitative_researcher(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            session=session,
            universe=universe,
            archive_root=archive_root,
        )

    return await _run_qualitative_researcher(
        invocation_id=invocation_id,
        as_of=as_of,
        last_invocation_time=last_invocation_time,
        universal_regime_label=universal_regime_label,
        sector_roster=sector_roster,
        universe=universe,
        agents_config=agents_config,
        deps=_Deps(
            inputs_loader=_inputs_loader,
            digest_renderer=_digest_renderer,
            bundle_assembler=assemble_input_bundle,
            harness_fn=_harness_fn,
        ),
        archive_root=archive_root,
    )
