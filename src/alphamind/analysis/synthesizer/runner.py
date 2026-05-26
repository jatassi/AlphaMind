"""Synthesizer runner — story 10 (ALP-210).

Composes the four upstream-brief adapters (story 05a), the retrieval-store
assembler (story 05a), the input-bundle assembler (story 07), and the harness
(story 08) into a single ``run_synthesizer`` entry point. Returns the
synthesizer's prose alongside the populated ``RetrievalStore`` the
decision-layer agents will consume.

The runner is ``async`` because the harness is ``async``. Per the parent
issue's "fail-closed propagation" invariant, any harness failure aborts the
synthesizer invocation; the runner does NOT catch and degrade.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._sdk_subprocess import invoke_synthesizer_in_subprocess
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.adaptive_research.models import AdaptiveBrief
from alphamind.analysis.domain_researchers.models import SectorBrief
from alphamind.analysis.qualitative_research.models import QualitativeBrief
from alphamind.analysis.synthesizer.adapters import (
    adaptive_brief_to_bundle,
    correlation_regime_brief_to_bundle,
    qualitative_brief_to_bundle,
    sector_brief_to_bundle,
)
from alphamind.analysis.synthesizer.harness import (  # noqa: F401 — kept for tests that inject the in-process harness
    HarnessSuccess,
    invoke_synthesizer,
)
from alphamind.analysis.synthesizer.input_bundle import assemble_input_bundle
from alphamind.analysis.synthesizer.models import BriefBundle
from alphamind.analysis.synthesizer.portfolio_tools import PORTFOLIO_TOOL_NAMES
from alphamind.analysis.synthesizer.retrieval import (
    RetrievalStore,
    assemble_retrieval_store,
)
from alphamind.config.models.agents import (
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.portfolio_state.consumers.synthesizer import SynthesizerPortfolioStateReader

__all__ = [
    "SynthesizerResult",
    "load_synthesizer_agent_config",
    "run_synthesizer",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repo-root + config-path resolution (mirrors the pattern in
# `alphamind.config.models.agents` and the verification scripts).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SynthesizerResult:
    """Runner return type — the synthesis prose plus the per-invocation store.

    ``synthesis_text`` is the LLM's prose. ``retrieval_store`` is the
    populated store the decision-layer agents (analyst, strategist, PM)
    will query via the ``retrieve_brief`` MCP tool. The remaining four
    fields come from the harness response and surface invocation
    metadata for the caller's logs/diagnostics.
    """

    synthesis_text: str
    retrieval_store: RetrievalStore
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """Injectable harness callable.

    The runner's other composables (adapters, retrieval-store assembler,
    input-bundle assembler) are pure functions imported at module scope;
    only the harness needs DI for testability.
    """

    harness_fn: Callable[..., Coroutine[Any, Any, HarnessSuccess]]


# ---------------------------------------------------------------------------
# Private runner (dependency-injected)
# ---------------------------------------------------------------------------


async def _run_synthesizer(  # noqa: PLR0913 — signature dictated by ALP-210 spec
    *,
    agent_config: BaseAgentConfig,
    regime_label: str,
    sector_briefs: tuple[SectorBrief, ...],
    correlation_regime_brief: CorrelationRegimeBrief,
    qualitative_brief: QualitativeBrief,
    adaptive_brief: AdaptiveBrief | None,
    portfolio_reader: SynthesizerPortfolioStateReader,
    invocation_id: str,
    now_utc: datetime,
    deps: _Deps,
    archive_root: Path | None = None,
) -> SynthesizerResult:
    """Compose the synthesizer's full work and return a ``SynthesizerResult``.

    Adapt every upstream brief, assemble the retrieval store, render the
    user-message text, invoke the harness, and return the synthesis text
    bundled with the store.
    """
    bundles: list[BriefBundle] = [correlation_regime_brief_to_bundle(correlation_regime_brief)]
    bundles.extend(sector_brief_to_bundle(brief, now_utc) for brief in sector_briefs)
    bundles.append(qualitative_brief_to_bundle(qualitative_brief, now_utc))
    if adaptive_brief is not None:
        bundles.append(adaptive_brief_to_bundle(adaptive_brief, now_utc))

    retrieval_store = assemble_retrieval_store(bundles)
    logger.info(
        "retrieval store assembled (entries=%d, sources=%d)",
        len(retrieval_store.entries),
        len(retrieval_store.freshness_by_source),
    )

    user_message = assemble_input_bundle(
        regime_label=regime_label,
        brief_bundles=tuple(bundles),
        portfolio_tool_names=PORTFOLIO_TOOL_NAMES,
        now_utc=now_utc,
    )
    logger.info("synthesizer input bundle assembled (chars=%d)", len(user_message))

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade. The pipeline-level orchestrator handles fail-closed semantics.
    logger.info("invoking synthesizer harness (invocation_id=%s)", invocation_id)
    harness_result: HarnessSuccess = await deps.harness_fn(
        agent_config=agent_config,
        user_message=user_message,
        invocation_id=invocation_id,
        portfolio_reader=portfolio_reader,
        archive_root=archive_root,
    )
    logger.info(
        "synthesizer harness invoked (tokens=%s, tool_calls=%d, stop_reason=%s)",
        harness_result.tokens_used,
        harness_result.tool_calls_used,
        harness_result.stop_reason,
    )

    return SynthesizerResult(
        synthesis_text=harness_result.response_text,
        retrieval_store=retrieval_store,
        tokens_used=harness_result.tokens_used,
        tool_calls_used=harness_result.tool_calls_used,
        wall_clock_seconds=harness_result.wall_clock_seconds,
        stop_reason=harness_result.stop_reason,
    )


# ---------------------------------------------------------------------------
# Public entry point — production defaults injected
# ---------------------------------------------------------------------------


def load_synthesizer_agent_config(
    agents_yaml_path: Path | None = None,
) -> BaseAgentConfig:
    """Read ``config/agents.yaml`` and return the synthesizer entry.

    Uses the existing :class:`AgentsConfig` validator (no parallel YAML
    parsing). The synthesizer slot is non-tool-loop, so the loaded entry
    is a plain :class:`BaseAgentConfig` rather than ``AdaptiveAgentConfig``.

    Defaults to the in-tree ``config/agents.yaml``; tests and the
    verification script can override via ``agents_yaml_path``.
    """
    path = agents_yaml_path or _AGENTS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    return cfg.agents[AgentName.synthesizer]


async def run_synthesizer(  # noqa: PLR0913 — signature dictated by ALP-210 spec plus ALP-497 progress/phase kwargs
    *,
    regime_label: str,
    sector_briefs: tuple[SectorBrief, ...],
    correlation_regime_brief: CorrelationRegimeBrief,
    qualitative_brief: QualitativeBrief,
    adaptive_brief: AdaptiveBrief | None,
    portfolio_reader: SynthesizerPortfolioStateReader,
    invocation_id: str,
    now_utc: datetime,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    agent_config: BaseAgentConfig | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "synthesizer",
) -> SynthesizerResult:
    """Invoke the synthesizer and return a ``SynthesizerResult``.

    Resolves the synthesizer's :class:`BaseAgentConfig` from
    ``config/agents.yaml`` via the existing :class:`AgentsConfig`
    loader unless an ``agent_config`` is supplied — the pipeline
    composition runner forwards a per-trigger-overridden config so
    ``run_types/<trigger>.yaml`` budget knobs reach the synthesizer
    slot. Builds the retrieval store + input bundle from the upstream
    briefs, and invokes :func:`invoke_synthesizer` with
    ``sdk_query_fn`` forwarded for SDK-substitution in test contexts.

    Any :class:`~alphamind.analysis.synthesizer.harness.HarnessFailure`
    raised by the harness propagates up unchanged.
    """

    async def _default_harness(
        *,
        agent_config: BaseAgentConfig,
        user_message: str,
        invocation_id: str,
        portfolio_reader: SynthesizerPortfolioStateReader,
        archive_root: Path | None,
    ) -> HarnessSuccess:
        return await invoke_synthesizer_in_subprocess(
            agent_config=agent_config,
            user_message=user_message,
            invocation_id=invocation_id,
            portfolio_reader=portfolio_reader,
            as_of=now_utc,
            archive_root=archive_root,
            sdk_query_fn=sdk_query_fn,
            progress=progress,
            phase=phase,
        )

    return await _run_synthesizer(
        agent_config=agent_config or load_synthesizer_agent_config(),
        regime_label=regime_label,
        sector_briefs=sector_briefs,
        correlation_regime_brief=correlation_regime_brief,
        qualitative_brief=qualitative_brief,
        adaptive_brief=adaptive_brief,
        portfolio_reader=portfolio_reader,
        invocation_id=invocation_id,
        now_utc=now_utc,
        deps=_Deps(harness_fn=_default_harness),
        archive_root=archive_root,
    )
