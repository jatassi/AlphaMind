"""Strategist runner — story 07 (ALP-308).

Composes :func:`build_initial_validation_state` (shared MCP wrapper, story 04
in the analyst tree), the input-bundle assembler (story 04 here), and the
harness (story 06 here) into a single :func:`run_strategist` entry point.
Returns the parsed strategist output bundled with the validation result, the
cumulative token usage, and invocation metadata.

The runner is ``async`` because the harness is ``async``. Per the parent
issue's "fail-closed propagation" invariant, any harness failure aborts the
strategist invocation; the runner does NOT catch and degrade.

Mirrors :mod:`alphamind.decision.analyst.runner` point-for-point with
strategist substitutions: :class:`StrategistView` in place of
:class:`AnalystView`, ``defensive_posture`` in place of ``watchlist``, and
the strategist-specific input-bundle assemblers + harness.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from alphamind._kernel.progress import NOOP_PROGRESS_EMITTER, ProgressEmitter
from alphamind.analysis._sdk_subprocess import invoke_strategist_in_subprocess
from alphamind.analysis._shared import TokensUsed
from alphamind.analysis.synthesizer.retrieval import RetrievalStore
from alphamind.config.models.agents import (
    AgentName,
    AgentsConfig,
    BaseAgentConfig,
)
from alphamind.decision.strategist.harness import (  # noqa: F401 — kept for tests that inject the in-process harness
    HarnessSuccess,
    run_strategist_harness,
)
from alphamind.decision.strategist.input_bundle import (
    assemble_input_bundle_defensive_posture,
    assemble_input_bundle_normal,
)
from alphamind.decision.strategist.models import StrategistOutput
from alphamind.decision.strategist.validation import ValidationResult
from alphamind.portfolio_state.consumers.strategist import StrategistView
from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    resolve_ticker,
)
from alphamind.portfolio_state.views.thesis_health import ThesisHealthSnapshot
from alphamind.risk_guardrails.breach_behavior import HaltState
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones,
    FeatureFlagsView,
    LibraryConfig,
    MarketInputs,
    PortfolioStateSnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeTransitionBreach
from alphamind.risk_guardrails.state_delivery import build_initial_validation_state
from alphamind.risk_guardrails.state_delivery.config import StateDeliveryConfig

__all__ = [
    "STRATEGIST_TOOL_NAMES",
    "StrategistResult",
    "load_strategist_agent_config",
    "run_strategist",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo-root + agents.yaml resolution (mirrors analyst.runner)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENTS_YAML = _REPO_ROOT / "config" / "agents.yaml"


# ---------------------------------------------------------------------------
# Canonical strategist tool names
# ---------------------------------------------------------------------------

# Wire-form names rendered into the strategist's input bundle's
# ``=== AVAILABLE TOOLS ===`` section. The harness's two MCP factories
# (``build_validate_guardrail_mcp_server`` and ``build_retrieve_brief_mcp_server``)
# emit these same names as their allowed-tools lists. The validation factory
# exposes both the single-call and batch validators when
# ``include_batch_tool=True`` (default for strategist and PM); both must
# appear here so the per-invocation tool reminder lists every callable the
# allowed-tools list permits (ALP-621 Finding 7).
STRATEGIST_TOOL_NAMES: tuple[str, ...] = (
    "mcp__alphamind_decision_validation__validate_guardrail",
    "mcp__alphamind_decision_validation__validate_guardrail_batch",
    "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategistResult:
    """Runner return type — parsed strategist output plus invocation metadata.

    Mirrors the shape of :class:`HarnessSuccess` but lifted to the runner's
    public surface so downstream callers do not depend on the harness's
    internal type.
    """

    output: StrategistOutput
    validation_result: ValidationResult
    tokens_used: TokensUsed
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# agents.yaml loader
# ---------------------------------------------------------------------------


def load_strategist_agent_config(
    agents_yaml_path: Path | None = None,
) -> BaseAgentConfig:
    """Read ``config/agents.yaml`` and return the strategist entry.

    Mirrors :func:`alphamind.decision.analyst.runner.load_analyst_agent_config`.
    Defaults to the in-tree ``config/agents.yaml``; tests and the verification
    script can override via ``agents_yaml_path``. The strategist slot is
    non-tool-loop, so the loaded entry is a plain :class:`BaseAgentConfig`.
    """
    path = agents_yaml_path or _AGENTS_YAML
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = AgentsConfig.model_validate(data)
    entry = cfg.agents[AgentName.strategist]
    # The strategist is a non-tool-loop slot per agents.yaml; AgentsConfig's
    # validator already guarantees this. Narrow the type for callers.
    assert isinstance(entry, BaseAgentConfig)
    return entry


# ---------------------------------------------------------------------------
# sector_resolver adapter
# ---------------------------------------------------------------------------


def _adapt_sector_resolver_for_input_bundle(
    sector_resolver: Callable[[str], str],
) -> Callable[[PositionRecord], str | None]:
    """Adapt a ticker→sector resolver to the position-based shape the input
    bundle's ``render_strategist_header`` expects.

    The runner's public surface accepts ``Callable[[str], str]`` per parent
    decision (G); the input bundle threads a position-based resolver to the
    state-delivery renderer. The adapter extracts the underlying ticker from
    the position and forwards.
    """

    def _adapter(position: PositionRecord) -> str | None:
        ticker = resolve_ticker(position.details)
        if ticker is None:
            return None
        return sector_resolver(ticker)

    return _adapter


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_strategist(  # noqa: PLR0913 — signature dictated by ALP-308 spec plus ALP-497 progress/phase
    *,
    invocation_id: str,
    timestamp: datetime,
    mode: Literal["normal", "defensive_posture"],
    halt_state: HaltState | None,
    strategist_view: StrategistView,
    synthesizer_brief_text: str,
    retrieval_store: RetrievalStore,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[str], str],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    profile_feature_flags: FeatureFlagsView,
    library_config: LibraryConfig,
    library_market: MarketInputs,
    starting_snapshot: PortfolioStateSnapshot,
    archive_root: Path | None = None,
    agent_config: BaseAgentConfig | None = None,
    agents_config_path: Path | None = None,
    sector_label_display: dict[str, str] | None = None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...] = (),
    sdk_query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    borrow_cost_resolver: Callable[[str], float | None] | None = None,
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...] = (),
    progress: ProgressEmitter = NOOP_PROGRESS_EMITTER,
    phase: str = "strategist",
) -> StrategistResult:
    """Invoke the strategist and return a :class:`StrategistResult`.

    Resolves the strategist's :class:`BaseAgentConfig` from
    ``config/agents.yaml`` via the existing :class:`AgentsConfig` loader
    unless an ``agent_config`` is supplied — the pipeline composition runner
    forwards a per-trigger-overridden config so ``run_types/<trigger>.yaml``
    budget knobs reach the strategist slot.

    Builds the validation-tool initial state (fresh per invocation), composes
    the user-message via :func:`assemble_input_bundle_normal` or
    :func:`assemble_input_bundle_defensive_posture` based on *mode*, invokes
    :func:`run_strategist_harness`, and returns the parsed output bundled
    with invocation metadata.

    Any :class:`~alphamind.decision.strategist.harness.HarnessFailure` raised
    by the harness propagates up unchanged — the runner does NOT catch and
    degrade. The pipeline-level orchestrator handles fail-closed semantics.
    """
    if mode == "defensive_posture" and halt_state is None:
        raise ValueError(
            "run_strategist(mode='defensive_posture') requires a halt_state; "
            "the defensive-posture header renderer cannot be composed without one"
        )

    resolved_config = agent_config or load_strategist_agent_config(agents_config_path)
    system_prompt = (_REPO_ROOT / resolved_config.prompt).read_text(encoding="utf-8")

    initial_validation_state = build_initial_validation_state(
        invocation_id=invocation_id,
        starting_snapshot=starting_snapshot,
        starting_risk_budget=strategist_view.risk_budget,
        starting_active_risk_parameters=strategist_view.active_risk_parameters,
        profile_feature_flags=profile_feature_flags,
        library_config=library_config,
        library_market=library_market,
        sector_resolver=sector_resolver,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    user_message = _assemble_user_message(
        mode=mode,
        halt_state=halt_state,
        strategist_view=strategist_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        state_delivery_config=state_delivery_config,
        sector_resolver=sector_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        current_price_lookup=current_price_lookup,
        synthesizer_brief_text=synthesizer_brief_text,
        position_zones=library_config.position_zones,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        prior_health_snapshots=prior_health_snapshots,
    )
    logger.info("strategist input bundle assembled (mode=%s, chars=%d)", mode, len(user_message))

    # HarnessFailure propagates up unchanged — the runner does NOT catch and
    # degrade. The pipeline-level orchestrator handles fail-closed semantics.
    # ALP-650: route through the subprocess wrapper so an SDK stall in the
    # strategist's harness no longer wedges the parent pipeline.
    harness_result: HarnessSuccess = await invoke_strategist_in_subprocess(
        user_message=user_message,
        system_prompt=system_prompt,
        invocation_id=invocation_id,
        agent_config=resolved_config,
        validation_state=initial_validation_state,
        retrieval_store=retrieval_store,
        active_sectors=frozenset(active_sectors),
        archive_root=archive_root,
        sdk_query_fn=sdk_query_fn,
        progress=progress,
        phase=phase,
    )
    logger.info(
        "strategist harness invoked (tokens=%s, attempts=%d)",
        harness_result.tokens_used,
        harness_result.metadata.get("attempts"),
    )

    # The runner sets the mode authoritatively; the harness's parser/validator
    # should have caught any mismatch already, but pin it here so silent drift
    # surfaces as a fail-closed exception.
    if harness_result.output.mode != mode:
        raise ValueError(
            f"strategist harness returned output.mode={harness_result.output.mode!r} "
            f"but runner dispatched mode={mode!r}"
        )

    return StrategistResult(
        output=harness_result.output,
        validation_result=harness_result.validation_result,
        tokens_used=harness_result.tokens_used,
        metadata=harness_result.metadata,
    )


def _assemble_user_message(  # noqa: PLR0913 — fan-in of input-bundle assembler args
    *,
    mode: Literal["normal", "defensive_posture"],
    halt_state: HaltState | None,
    strategist_view: StrategistView,
    invocation_id: str,
    timestamp: datetime,
    options_enabled: bool,
    short_selling_enabled: bool,
    active_sectors: tuple[str, ...],
    state_delivery_config: StateDeliveryConfig,
    sector_resolver: Callable[[str], str],
    total_portfolio_value_usd: float,
    available_for_new_positions_usd: float,
    current_price_lookup: Callable[[str], float],
    synthesizer_brief_text: str,
    position_zones: EscalationZones,
    sector_label_display: dict[str, str] | None,
    regime_transition_breaches: tuple[RegimeTransitionBreach, ...],
    prior_health_snapshots: tuple[ThesisHealthSnapshot, ...],
) -> str:
    """Dispatch to the mode-specific input-bundle assembler.

    Both assemblers share the same non-halt arguments; pulling the dispatch
    out of :func:`run_strategist` keeps that function under the linter's
    PLR0915 threshold.
    """
    bundle_resolver = _adapt_sector_resolver_for_input_bundle(sector_resolver)
    if mode == "normal":
        return assemble_input_bundle_normal(
            strategist_view=strategist_view,
            invocation_id=invocation_id,
            timestamp=timestamp,
            options_enabled=options_enabled,
            short_selling_enabled=short_selling_enabled,
            active_sectors=active_sectors,
            state_delivery_config=state_delivery_config,
            sector_resolver=bundle_resolver,
            total_portfolio_value_usd=total_portfolio_value_usd,
            available_for_new_positions_usd=available_for_new_positions_usd,
            current_price_lookup=current_price_lookup,
            synthesizer_brief_text=synthesizer_brief_text,
            tool_names=STRATEGIST_TOOL_NAMES,
            position_zones=position_zones,
            sector_label_display=sector_label_display,
            regime_transition_breaches=regime_transition_breaches,
            prior_health_snapshots=prior_health_snapshots,
        )

    # mode == "defensive_posture" — the runner-side guard above guarantees
    # halt_state is not None at this point.
    assert halt_state is not None
    return assemble_input_bundle_defensive_posture(
        halt_state=halt_state,
        strategist_view=strategist_view,
        invocation_id=invocation_id,
        timestamp=timestamp,
        options_enabled=options_enabled,
        short_selling_enabled=short_selling_enabled,
        active_sectors=active_sectors,
        state_delivery_config=state_delivery_config,
        sector_resolver=bundle_resolver,
        total_portfolio_value_usd=total_portfolio_value_usd,
        available_for_new_positions_usd=available_for_new_positions_usd,
        current_price_lookup=current_price_lookup,
        synthesizer_brief_text=synthesizer_brief_text,
        tool_names=STRATEGIST_TOOL_NAMES,
        position_zones=position_zones,
        sector_label_display=sector_label_display,
        regime_transition_breaches=regime_transition_breaches,
        prior_health_snapshots=prior_health_snapshots,
    )
