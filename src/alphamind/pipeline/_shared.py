"""Shared pipeline-composition helpers — used by analysis + decision runners.

The module exports two helpers shared across the pipeline composition tier:

* :func:`apply_agent_overrides` — the per-trigger override layering helper
  that the analysis-layer pipeline (story ALP-276) and the decision-layer
  pipeline (story ALP-403) both call. The helper folds
  ``RunTypeConfig.agent_overrides`` fields onto each :class:`BaseAgentConfig`
  via Pydantic ``model_copy(update=...)`` and produces the string-keyed
  mapping the agent runners consume.
* :func:`build_phase1_enforcement_inputs` — the Phase 1 enforcement input
  gatherer (story ALP-433). Reads :class:`DrawdownState` from the supplied
  repository and returns the three-tuple suitable for
  :func:`compose_phase_1_enforcement`. Lives here so the analysis pipeline
  and other downstream consumers can reuse it if they need to compute the
  same view; the decision pipeline runner is the first caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from alphamind.config.models.agents import AgentName, BaseAgentConfig

if TYPE_CHECKING:
    from alphamind.config.models.guardrails import ProgressiveTier
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
    from alphamind.portfolio_state.repository import PortfolioStateRepository
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationOutput

__all__ = ["apply_agent_overrides", "build_phase1_enforcement_inputs"]


def apply_agent_overrides(
    agents_config: Mapping[AgentName, BaseAgentConfig],
    agent_overrides: Mapping[AgentName, Mapping[str, Any]],
) -> dict[str, BaseAgentConfig]:
    """Layer per-trigger budget overrides onto base agent configs.

    For each agent in ``agents_config``, applies any matching override
    via Pydantic ``model_copy(update=...)`` so the resulting config carries
    the trigger-specific budget knobs from ``config/run_types/<trigger>.yaml``.
    Returns a string-keyed mapping ready for the agent runners (which key on
    the agent's :class:`AgentName` string value).

    Override keys must match attribute names on the target agent's
    Pydantic model — :class:`RunTypeConfig.AgentsSection` already restricts
    them to the documented allow-list at parse time, so no field validation
    is repeated here.
    """
    return {
        name.value: (
            cfg.model_copy(update=dict(agent_overrides[name])) if name in agent_overrides else cfg
        )
        for name, cfg in agents_config.items()
    }


async def build_phase1_enforcement_inputs(
    *,
    repository: PortfolioStateRepository,
    regime_output: RegimeAdaptationOutput,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> tuple[RegimeAdaptationOutput, DrawdownState, tuple[ProgressiveTier, ...]]:
    """Gather the three inputs ``compose_phase_1_enforcement`` requires.

    Reads :class:`DrawdownState` from the supplied repository and bundles it
    with the regime-adaptation output and progressive-tier sequence into a
    tuple ready for unpacking into ``compose_phase_1_enforcement(**dict(
    zip([\"regime_output\", \"drawdown_state\", \"progressive_tiers\"],
    result)))``.

    Pure async I/O — no caching. The decision-pipeline runner (story
    ALP-433) is the canonical caller; analysis-layer or monitor consumers
    needing the same composition view may call this helper directly.
    """
    drawdown_state = await repository.get_drawdown_state()
    return regime_output, drawdown_state, progressive_tiers
