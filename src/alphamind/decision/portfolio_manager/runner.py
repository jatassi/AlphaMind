"""Portfolio-manager runner — story 08 (ALP-330).

Composes :func:`build_initial_validation_state` (shared MCP wrapper, analyst
story 04), :func:`build_initial_submit_envelope_state` (PM story 06c), the
input-bundle assembler (PM story 04), and the harness (PM story 07) into a
single :func:`run_portfolio_manager` entry point. Returns the parsed
:class:`PMCompletionRecord` sentinel bundled with the engine-stub's submission
log and invocation metadata as :class:`PMResult`.

The runner is ``async`` because the harness is ``async``. Per the parent
issue's "fail-closed propagation" invariant, any harness failure aborts the
PM invocation; the runner does NOT catch and degrade.

Mirrors :mod:`alphamind.decision.strategist.runner` extended with the
additional state-cell construction (``build_initial_submit_envelope_state``),
the submission-log exposure on :class:`PMResult`, and the four-MCP-server
input-bundle dispatch (normal vs halt) per parent decision (M).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from alphamind._kernel.money import Money
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.commands.pm_envelope import PMCompletionRecord
from alphamind.commands.protocols import BrokerDispatch
from alphamind.commands.submission_log import SubmissionLogEntry
from alphamind.config.models.agents import (
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.decision.portfolio_manager.harness import (
    HarnessSuccess,
    invoke_pm,
)
from alphamind.decision.portfolio_manager.input_bundle import (
    assemble_input_bundle_halt,
    assemble_input_bundle_normal,
)
from alphamind.decision.portfolio_manager.submit_envelope import (
    build_initial_submit_envelope_state,
)
from alphamind.decision.proposal_pre_processor import ProposalPreProcessorBundle
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
    PortfolioManagerView,
)
from alphamind.portfolio_state.records.orders import OrderRecord
from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    resolve_ticker,
)
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import build_initial_validation_state
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig
from alphamind.risk_guardrails.state_delivery.portfolio_manager import (
    CorrelationState,
    CrossConstraintImpact,
    DependencyRiskFlag,
    RegimeOverride,
)

__all__ = [
    "PM_TOOL_NAMES",
    "PMResult",
    "load_pm_agent_config",
    "run_portfolio_manager",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo-root + agents.yaml resolution (mirrors strategist.runner)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"


# ---------------------------------------------------------------------------
# Canonical PM tool names
# ---------------------------------------------------------------------------

# Wire-form names rendered into the PM's input bundle's ``=== AVAILABLE
# TOOLS ===`` section. The harness's four MCP factories
# (``build_validate_guardrail_mcp_server``, ``build_retrieve_brief_mcp_server``,
# ``build_get_thesis_components_mcp_server``, ``build_submit_envelope_mcp_server``)
# emit these same names as their allowed-tools lists.
PM_TOOL_NAMES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__validate_guardrail",
    "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
    "mcp__alphamind_portfolio_state_thesis_components__get_thesis_components",
    "mcp__alphamind_execution_oms_submit__submit_envelope",
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PMResult:
    """Runner return type — the parsed PM completion sentinel plus invocation
    metadata and the engine-stub's per-envelope submission log.

    Mirrors :class:`HarnessSuccess` lifted to the runner's public surface so
    downstream callers do not depend on the harness's internal type. Per
    parent decision (D), the PM's actual product is the side-effect stream of
    ``submit_envelope`` tool calls captured in ``submission_log``; the
    structured ``output`` is the thin completion sentinel.
    """

    output: PMCompletionRecord
    submission_log: tuple[SubmissionLogEntry, ...]
    retry_count: int
    tokens_used: TokensUsed
    tool_calls_used: int
    wall_clock_seconds: float
    stop_reason: str | None


# ---------------------------------------------------------------------------
# agents.yaml loader
# ---------------------------------------------------------------------------


def load_pm_agent_config(
    agents_yaml_path: Path | None = None,
) -> BaseAgentConfig:
    """Read ``config/agents.yaml`` and return the portfolio_manager entry.

    Mirrors :func:`alphamind.decision.strategist.runner.load_strategist_agent_config`.
    Defaults to the in-tree ``config/agents.yaml``; tests and the verification
    script can override via ``agents_yaml_path``. The portfolio_manager slot
    is non-tool-loop, so the loaded entry is a plain :class:`BaseAgentConfig`.
    """
    path = agents_yaml_path or _AGENTS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    entry = cfg.agents[AgentName.portfolio_manager]
    # The portfolio_manager is a non-tool-loop slot per agents.yaml; AgentsConfig's
    # validator already guarantees this. Narrow the type for callers.
    assert isinstance(entry, BaseAgentConfig)
    return entry


# ---------------------------------------------------------------------------
# sector_resolver adapter — runner exposes ``Callable[[str], str]`` per parent
# decision (J); the input bundle threads a position-based resolver to the
# state-delivery renderer.
# ---------------------------------------------------------------------------


def _adapt_sector_resolver_for_input_bundle(
    sector_resolver: Callable[[str], str],
    sector_resolver_position: Callable[[PositionRecord], str | None] | None,
) -> Callable[[PositionRecord], str | None]:
    """Adapt a ticker→sector resolver to the position-based shape the input
    bundle's ``render_pm_header`` / ``render_pm_header_halt_mode`` expect.

    Callers that need full control over how a position resolves to a sector
    (e.g., handling spread positions or unknown tickers explicitly) can
    supply ``sector_resolver_position`` directly; otherwise the runner
    extracts the underlying ticker and forwards to ``sector_resolver``.
    """
    if sector_resolver_position is not None:
        return sector_resolver_position

    def _adapter(position: PositionRecord) -> str | None:
        ticker = resolve_ticker(position.details)
        if ticker is None:
            return None
        return sector_resolver(ticker)

    return _adapter


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_portfolio_manager(  # noqa: PLR0913 — signature dictated by ALP-330 spec
    *,
    mode: Literal["normal", "halt"],
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_text: str,
    retrieval_store: RetrievalStore,
    pm_view: PortfolioManagerView,
    thesis_component_reader: PortfolioManagerThesisComponentReader,
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
    total_portfolio_value_usd: Money,
    available_for_new_positions_usd: Money,
    cross_constraint_impact: CrossConstraintImpact,
    halt_state: HaltState | None = None,
    pending_orders: tuple[OrderRecord, ...] = (),
    current_price_lookup: Callable[[str], float] | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    active_regime_overrides: tuple[RegimeOverride, ...] = (),
    correlation_state: CorrelationState | None = None,
    dependency_risk_flag: DependencyRiskFlag | None = None,
    sector_resolver_position: Callable[[PositionRecord], str | None] | None = None,
    archive_root: Path | None = None,
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    agent_config: BaseAgentConfig | None = None,
    borrow_cost_resolver: Callable[[str], float] | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
    broker_dispatch: BrokerDispatch | None = None,
) -> PMResult:
    """Invoke the portfolio manager and return a :class:`PMResult`.

    Resolves the portfolio_manager :class:`BaseAgentConfig` from
    ``config/agents.yaml`` via the existing :class:`AgentsConfig` loader
    unless an ``agent_config`` is supplied — the pipeline composition runner
    forwards a per-trigger-overridden config so ``run_types/<trigger>.yaml``
    budget knobs reach the PM slot.

    Builds the validation-tool initial state and the submit_envelope initial
    state (both fresh per invocation; the latter wraps the former so
    cumulative-impact tracking is unified across pre-submission validation
    and submit-time re-validation), composes the user-message via
    :func:`assemble_input_bundle_normal` or :func:`assemble_input_bundle_halt`
    based on *mode* (per parent decision (M), ``mode`` is caller-supplied —
    never inferred from breach state), invokes :func:`invoke_pm`, and returns
    the parsed sentinel bundled with the submission log and invocation
    metadata.

    Any :class:`~alphamind.decision.portfolio_manager.harness.HarnessFailure`
    raised by the harness propagates up unchanged — the runner does NOT catch
    and degrade. The pipeline-level orchestrator handles fail-closed semantics.
    """
    if mode == "halt":
        if halt_state is None:
            raise ValueError(
                "run_portfolio_manager(mode='halt') requires a halt_state; "
                "the halt-mode header renderer cannot be composed without one"
            )
        if current_price_lookup is None:
            raise ValueError(
                "run_portfolio_manager(mode='halt') requires a current_price_lookup; "
                "the halt-mode pending-orders block needs ticker→price resolution"
            )

    resolved_config = agent_config or load_pm_agent_config()

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
    initial_submit_envelope_state = build_initial_submit_envelope_state(
        invocation_id=invocation_id,
        starting_validation_state=initial_validation_state,
    )

    active_sectors_tuple = tuple(sorted(active_sectors))
    bundle_resolver = _adapt_sector_resolver_for_input_bundle(
        sector_resolver, sector_resolver_position
    )
    user_message = _assemble_user_message(
        mode=mode,
        halt_state=halt_state,
        pm_view=pm_view,
        pre_processor_bundle=pre_processor_bundle,
        synthesizer_brief_text=synthesizer_text,
        invocation_id=invocation_id,
        timestamp=timestamp,
        pending_orders=pending_orders,
        current_price_lookup=current_price_lookup,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors_tuple=active_sectors_tuple,
        state_delivery_config=state_delivery_config,
        bundle_resolver=bundle_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        cross_constraint_impact=cross_constraint_impact,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        active_regime_overrides=active_regime_overrides,
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
        prior_health_snapshots=prior_health_snapshots,
    )
    logger.info(
        "portfolio_manager input bundle assembled (mode=%s, chars=%d)",
        mode,
        len(user_message),
    )

    halt_mode = mode == "halt"

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade. The pipeline-level orchestrator handles fail-closed semantics.
    harness_result: HarnessSuccess = await invoke_pm(
        agent_config=resolved_config,
        user_message=user_message,
        invocation_id=invocation_id,
        initial_validation_state=initial_validation_state,
        initial_submit_envelope_state=initial_submit_envelope_state,
        retrieval_store=retrieval_store,
        thesis_component_reader=thesis_component_reader,
        pre_processor_bundle=pre_processor_bundle,
        pm_view=pm_view,
        active_sectors=active_sectors,
        halt_mode=halt_mode,
        sector_resolver=sector_resolver,
        library_config=library_config,
        library_market=library_market,
        archive_root=archive_root,
        sdk_query_fn=sdk_query_fn,
        broker_dispatch=broker_dispatch,
    )
    logger.info(
        "portfolio_manager harness invoked "
        "(tokens=%s, tool_calls=%d, retry_count=%d, stop_reason=%s, envelopes=%d)",
        harness_result.tokens_used,
        harness_result.tool_calls_used,
        harness_result.retry_count,
        harness_result.stop_reason,
        len(harness_result.submission_log),
    )

    return PMResult(
        output=harness_result.output,
        submission_log=harness_result.submission_log,
        retry_count=harness_result.retry_count,
        tokens_used=harness_result.tokens_used,
        tool_calls_used=harness_result.tool_calls_used,
        wall_clock_seconds=harness_result.wall_clock_seconds,
        stop_reason=harness_result.stop_reason,
    )


def _assemble_user_message(  # noqa: PLR0913 — fan-in of input-bundle assembler args
    *,
    mode: Literal["normal", "halt"],
    halt_state: HaltState | None,
    pm_view: PortfolioManagerView,
    pre_processor_bundle: ProposalPreProcessorBundle,
    synthesizer_brief_text: str,
    invocation_id: str,
    timestamp: datetime,
    pending_orders: tuple[OrderRecord, ...],
    current_price_lookup: Callable[[str], float] | None,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors_tuple: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    bundle_resolver: Callable[[PositionRecord], str | None],
    total_portfolio_value_usd: Money,
    available_for_new_positions_usd: Money,
    cross_constraint_impact: CrossConstraintImpact,
    sector_label_display: dict[str, str] | None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...],
    active_regime_overrides: tuple[RegimeOverride, ...],
    correlation_state: CorrelationState | None,
    dependency_risk_flag: DependencyRiskFlag | None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...],
) -> str:
    """Dispatch to the mode-specific input-bundle assembler.

    Both assemblers share the same non-halt arguments; pulling the dispatch
    out of :func:`run_portfolio_manager` keeps that function under the
    linter's PLR0915 threshold.
    """
    if mode == "normal":
        return assemble_input_bundle_normal(
            pm_view=pm_view,
            pre_processor_bundle=pre_processor_bundle,
            synthesizer_brief_text=synthesizer_brief_text,
            invocation_id=invocation_id,
            timestamp=timestamp,
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
            active_sectors=active_sectors_tuple,
            state_delivery_config=state_delivery_config,
            sector_resolver=bundle_resolver,
            total_portfolio_value_usd=total_portfolio_value_usd,
            available_for_new_positions_usd=available_for_new_positions_usd,
            cross_constraint_impact=cross_constraint_impact,
            tool_names=PM_TOOL_NAMES,
            sector_label_display=sector_label_display,
            regime_transition_breaches=regime_transition_breaches,
            active_regime_overrides=active_regime_overrides,
            correlation_state=correlation_state,
            dependency_risk_flag=dependency_risk_flag,
            prior_health_snapshots=prior_health_snapshots,
        )

    # mode == "halt" — the runner-side guard above guarantees halt_state and
    # current_price_lookup are not None at this point.
    assert halt_state is not None
    assert current_price_lookup is not None
    return assemble_input_bundle_halt(
        halt_state=halt_state,
        pm_view=pm_view,
        pre_processor_bundle=pre_processor_bundle,
        synthesizer_brief_text=synthesizer_brief_text,
        invocation_id=invocation_id,
        timestamp=timestamp,
        pending_orders=pending_orders,
        current_price_lookup=current_price_lookup,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors_tuple,
        state_delivery_config=state_delivery_config,
        sector_resolver=bundle_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        cross_constraint_impact=cross_constraint_impact,
        tool_names=PM_TOOL_NAMES,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        active_regime_overrides=active_regime_overrides,
        correlation_state=correlation_state,
        dependency_risk_flag=dependency_risk_flag,
        prior_health_snapshots=prior_health_snapshots,
    )
