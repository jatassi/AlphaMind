"""Shared pipeline-composition helpers — used by analysis + decision runners.

The module currently exports :func:`apply_agent_overrides` — the per-trigger
override layering helper that the analysis-layer pipeline (story ALP-276)
and the decision-layer pipeline (story ALP-403) both call. The helper folds
``RunTypeConfig.agent_overrides`` fields onto each :class:`BaseAgentConfig`
via Pydantic ``model_copy(update=...)`` and produces the string-keyed mapping
the agent runners consume.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphamind.config.models.agents import AgentName, BaseAgentConfig

__all__ = ["apply_agent_overrides"]


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
