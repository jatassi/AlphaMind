"""Pydantic models for run_types/<trigger>.yaml — per-trigger overlay (story 04e).

Run-types are deterministic-only — they scope the agent roster (which agents fire
on this trigger) and the budget envelope (per-agent latency / token caps,
adaptive-research tool-call and token caps, qualitative news-digest depth) for
that invocation. They do not inject prompt instructions per
`docs/design/configuration-management.md § run_types/<trigger>.yaml`.

Per the same doc, the decision-layer trio (analyst, strategist, portfolio_manager)
and the synthesizer must fire on every run-type — the validator on
`AgentsSection.enabled` enforces this. Override fields are restricted to a closed
allow-list, with the two adaptive-only fields
(`cumulative_tool_call_limit`, `cumulative_tool_token_budget`) further restricted
to the `adaptive_researcher` slot per story 03i.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind.config.models.agents import AgentName

# Override fields any agent may receive. Mirrors the budget knobs declared on
# `BaseAgentConfig` in agents.py — adaptive-only fields (cumulative_tool_*) are
# layered on top below.
_BASE_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {
        "latency_budget_seconds",
        "context_token_budget",
        "output_token_budget",
    }
)

# Override fields restricted to the adaptive_researcher slot. Mirror
# `AdaptiveAgentConfig` extension on agents.py.
_ADAPTIVE_ONLY_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {
        "cumulative_tool_call_limit",
        "cumulative_tool_token_budget",
    }
)

# Full override allow-list keyed by agent. Adaptive-only fields appear only
# under adaptive_researcher.
_ALL_OVERRIDE_FIELDS: frozenset[str] = _BASE_OVERRIDE_FIELDS | _ADAPTIVE_ONLY_OVERRIDE_FIELDS


class RunType(StrEnum):
    """The six firing triggers the scheduler resolves to a run-type bundle.

    Member values match the filename stems under `config/run_types/` and the
    keys in `scheduler.yaml`'s `triggers:` map.
    """

    pre_open = "pre_open"
    market_hours_rolling = "market_hours_rolling"
    pre_close = "pre_close"
    off_hours_rolling = "off_hours_rolling"
    weekend_saturday = "weekend_saturday"
    weekend_sunday = "weekend_sunday"


class NewsDigestConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    top_n_per_sector: int = Field(ge=1)
    top_n_high_priority: int = Field(ge=0)


class QualitativeResearcherSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    news_digest: NewsDigestConfig


class AgentsSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: list[AgentName]
    overrides: dict[AgentName, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def enabled_is_well_formed(self) -> "AgentsSection":
        if not self.enabled:
            raise ValueError("agents.enabled must not be empty")
        if len(self.enabled) != len(set(self.enabled)):
            raise ValueError("agents.enabled must not contain duplicates")
        required = {
            AgentName.analyst,
            AgentName.strategist,
            AgentName.portfolio_manager,
            AgentName.synthesizer,
        }
        missing = required - set(self.enabled)
        if missing:
            missing_names = sorted(name.value for name in missing)
            raise ValueError(
                f"agents.enabled must include the decision layer and the synthesizer; "
                f"missing: {missing_names}"
            )
        return self

    @model_validator(mode="after")
    def overrides_reference_enabled_agents_only(self) -> "AgentsSection":
        enabled_set = set(self.enabled)
        for agent in self.overrides:
            if agent not in enabled_set:
                raise ValueError(
                    f"agents.overrides references {agent.value!r} which is not in "
                    f"agents.enabled — cannot override a disabled agent"
                )
        return self

    @model_validator(mode="after")
    def overrides_use_only_allow_listed_fields(self) -> "AgentsSection":
        for agent, override in self.overrides.items():
            for field_name in override:
                if field_name not in _ALL_OVERRIDE_FIELDS:
                    raise ValueError(
                        f"agents.overrides.{agent.value} field {field_name!r} is not a "
                        f"recognized override; allowed: {sorted(_ALL_OVERRIDE_FIELDS)}"
                    )
                if (
                    field_name in _ADAPTIVE_ONLY_OVERRIDE_FIELDS
                    and agent != AgentName.adaptive_researcher
                ):
                    raise ValueError(
                        f"agents.overrides.{agent.value}.{field_name} is adaptive-only "
                        f"and may only appear under "
                        f"{AgentName.adaptive_researcher.value!r}"
                    )
        return self


class RunTypeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    agents: AgentsSection
    qualitative_researcher: QualitativeResearcherSection
