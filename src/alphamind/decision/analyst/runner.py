"""Analyst runner — story 08 (ALP-299).

Composes :func:`build_initial_validation_state` (story 04, shared MCP wrapper),
the input-bundle assembler (story 06), and the harness (story 07) into a
single ``run_analyst`` entry point. Returns the parsed analyst output bundled
with invocation metadata.

The runner is ``async`` because the harness is ``async``. Per the parent
issue's "fail-closed propagation" invariant, any harness failure aborts the
analyst invocation; the runner does NOT catch and degrade.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._sdk_subprocess import invoke_analyst_in_subprocess
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import (
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.decision.analyst.harness import (  # noqa: F401 — kept for tests that inject the in-process harness
    HarnessSuccess,
    invoke_analyst,
)
from alphamind.decision.analyst.input_bundle import (
    assemble_input_bundle_halt,
    assemble_input_bundle_normal,
)
from alphamind.decision.analyst.models import AnalystOutput
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.analyst import AnalystView
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.validation_tool_mcp import (
    build_initial_validation_state,
)

__all__ = ["ANALYST_TOOL_NAMES", "AnalystResult", "load_analyst_agent_config", "run_analyst"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo-root + agents.yaml resolution (mirrors synthesizer.runner)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"


# ---------------------------------------------------------------------------
# Canonical analyst tool names
# ---------------------------------------------------------------------------

# Wire-form names rendered into the analyst's input bundle's ``=== AVAILABLE
# TOOLS ===`` section. The harness's two MCP factories
# (``build_validate_guardrail_mcp_server`` and ``build_retrieve_brief_mcp_server``)
# emit these same names as their allowed-tools lists.
ANALYST_TOOL_NAMES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__validate_guardrail",
    "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalystResult:
    """Runner return type — the parsed analyst output plus invocation metadata."""

    output: AnalystOutput
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


# ---------------------------------------------------------------------------
# agents.yaml loader
# ---------------------------------------------------------------------------


def load_analyst_agent_config(
    agents_yaml_path: Path | None = None,
) -> BaseAgentConfig:
    """Read ``config/agents.yaml`` and return the analyst entry.

    Mirrors :func:`alphamind.analysis.synthesizer.runner.load_synthesizer_agent_config`.
    Defaults to the in-tree ``config/agents.yaml``; tests and the verification
    script can override via ``agents_yaml_path``. The analyst slot is
    non-tool-loop, so the loaded entry is a plain :class:`BaseAgentConfig`.
    """
    path = agents_yaml_path or _AGENTS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    entry = cfg.agents[AgentName.analyst]
    # The analyst is a non-tool-loop slot per agents.yaml; AgentsConfig's
    # validator already guarantees this. Narrow the type for callers.
    assert isinstance(entry, BaseAgentConfig)
    return entry


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_analyst(  # noqa: PLR0913 — signature dictated by ALP-299 spec plus ALP-497 progress/phase
    *,
    mode: Literal["normal", "watchlist"],
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    sector_resolver: Callable[[str], str],
    portfolio_state_snapshot: PortfolioStateSnapshot,
    active_sectors: frozenset[str],
    invocation_id: str,
    timestamp: datetime,
    state_delivery_config: StateDeliveryConfig,
    options_enabled: bool,
    short_selling_enabled: bool,
    halt_state: HaltState | None = None,
    archive_root: Path | None = None,
    provenance_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    agent_config: BaseAgentConfig | None = None,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "analyst",
) -> AnalystResult:
    """Invoke the analyst and return an :class:`AnalystResult`.

    Resolves the analyst's :class:`BaseAgentConfig` from ``config/agents.yaml``
    via the existing :class:`AgentsConfig` loader unless an ``agent_config``
    is supplied — the pipeline composition runner forwards a per-trigger-
    overridden config so ``run_types/<trigger>.yaml`` budget knobs reach the
    analyst slot.

    Builds the validation-tool initial state (fresh per invocation), composes
    the user-message via :func:`assemble_input_bundle_normal` or
    :func:`assemble_input_bundle_halt` based on *mode*, invokes
    :func:`invoke_analyst`, and returns the parsed output bundled with
    invocation metadata.

    Any :class:`~alphamind.decision.analyst.harness.HarnessFailure` raised by
    the harness propagates up unchanged.
    """
    if mode == "watchlist" and halt_state is None:
        raise ValueError(
            "run_analyst(mode='watchlist') requires a halt_state; "
            "the halt-mode header renderer cannot be composed without one"
        )

    resolved_config = agent_config or load_analyst_agent_config()

    initial_validation_state = build_initial_validation_state(
        invocation_id=invocation_id,
        starting_snapshot=portfolio_state_snapshot,
        starting_risk_budget=risk_budget,
        starting_active_risk_parameters=active_risk_parameters,
        profile_feature_flags=profile_feature_flags,
        library_config=library_config,
        library_market=library_market,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    active_sectors_tuple = tuple(sorted(active_sectors))
    user_message = _assemble_user_message(
        mode=mode,
        halt_state=halt_state,
        analyst_view=analyst_view,
        risk_budget=risk_budget,
        active_risk_parameters=active_risk_parameters,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors_tuple=active_sectors_tuple,
        state_delivery_config=state_delivery_config,
        synthesizer_text=synthesizer_text,
        underlying_prices=library_market.underlying_prices,
    )
    logger.info("analyst input bundle assembled (mode=%s, chars=%d)", mode, len(user_message))

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade. The pipeline-level orchestrator handles fail-closed semantics.
    # ALP-650: route through the subprocess wrapper so an SDK stall in the
    # analyst's harness no longer wedges the parent pipeline.
    harness_result: HarnessSuccess = await invoke_analyst_in_subprocess(
        agent_config=resolved_config,
        user_message=user_message,
        invocation_id=invocation_id,
        initial_validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=active_sectors,
        as_of=timestamp,
        archive_root=archive_root,
        provenance_root=provenance_root,
        sdk_query_fn=sdk_query_fn,
        progress=progress,
        phase=phase,
    )
    logger.info(
        "analyst harness invoked (tokens=%s, tool_calls=%d, retry_count=%d, stop_reason=%s)",
        harness_result.tokens_used,
        harness_result.tool_calls_used,
        harness_result.retry_count,
        harness_result.stop_reason,
    )

    return AnalystResult(
        output=harness_result.output,
        retry_count=harness_result.retry_count,
        tokens_used=harness_result.tokens_used,
        tool_calls_used=harness_result.tool_calls_used,
        wall_clock_seconds=harness_result.wall_clock_seconds,
        stop_reason=harness_result.stop_reason,
    )


def _assemble_user_message(  # noqa: PLR0913 — fan-in of input-bundle assembler args
    *,
    mode: Literal["normal", "watchlist"],
    halt_state: HaltState | None,
    analyst_view: AnalystView,
    risk_budget: RiskBudgetConsumption,
    active_risk_parameters: ActiveRiskParameterSet,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors_tuple: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    synthesizer_text: str,
    underlying_prices: Mapping[str, float],
) -> str:
    """Dispatch to the mode-specific input-bundle assembler.

    Both assemblers share the same non-halt arguments; pulling the dispatch
    out of :func:`run_analyst` keeps that function under the linter's
    PLR0915 threshold. ``underlying_prices`` (the ALP-742 reference map) is only
    surfaced in normal mode — watchlist mode emits no brackets to anchor.
    """
    if mode == "normal":
        return assemble_input_bundle_normal(
            analyst_view=analyst_view,
            risk_budget=risk_budget,
            active_risk_parameters=active_risk_parameters,
            invocation_id=invocation_id,
            timestamp=timestamp,
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
            active_sectors=active_sectors_tuple,
            state_delivery_config=state_delivery_config,
            synthesizer_brief_text=synthesizer_text,
            tool_names=ANALYST_TOOL_NAMES,
            underlying_prices=underlying_prices,
        )

    # mode == "watchlist" — the runner-side guard above guarantees halt_state
    # is not None at this point.
    assert halt_state is not None
    return assemble_input_bundle_halt(
        halt_state=halt_state,
        analyst_view=analyst_view,
        risk_budget=risk_budget,
        active_risk_parameters=active_risk_parameters,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors_tuple,
        state_delivery_config=state_delivery_config,
        synthesizer_brief_text=synthesizer_text,
        tool_names=ANALYST_TOOL_NAMES,
    )
