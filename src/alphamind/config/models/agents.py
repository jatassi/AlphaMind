"""Pydantic models for agents.yaml — per-agent LLM configuration (story 03i).

The closed `AgentName` enum keys `AgentsConfig.agents`, making the per-agent slot
roster a contract. `BaseAgentConfig` carries the model identity, prompt path,
budget knobs, and tool allowlist; `AdaptiveAgentConfig` extends it with the
three structured tool-loop fields a tool-loop agent needs (per
`docs/design/03-analysis-layer/adaptive-research.md` and
`docs/design/03-analysis-layer/qualitative-research.md`). The discriminator
between the two is structural — tool-loop agents carry the extra fields and no
other agent does — enforced by a model validator on `AgentsConfig`. The set of
tool-loop slots is `_TOOL_LOOP_AGENTS`.
"""

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Repo root resolved by walking up: agents.py → models → config → alphamind →
# src → repo root. Mirrors the data_sources.py pattern.
_REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent

# Registered-tool naming convention from the analysis-layer specs.
_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AgentName(StrEnum):
    """Canonical agent identifiers, matching `state-persistence.md § Agent calls`."""

    analyst = "analyst"
    strategist = "strategist"
    portfolio_manager = "portfolio_manager"
    tech_semis_researcher = "tech_semis_researcher"
    financials_researcher = "financials_researcher"
    energy_researcher = "energy_researcher"
    qualitative_researcher = "qualitative_researcher"
    adaptive_researcher = "adaptive_researcher"
    synthesizer = "synthesizer"


# Slots whose entry must carry tool-loop fields (cumulative_tool_call_limit,
# cumulative_tool_token_budget, tool_caps). Any other slot must omit them.
_TOOL_LOOP_AGENTS: frozenset[AgentName] = frozenset(
    {AgentName.adaptive_researcher, AgentName.qualitative_researcher}
)


class AllowedModel(StrEnum):
    """Models the operator may assign to any agent.

    Haiku is unused in shipped configs but present so the operator can switch
    under sustained cap pressure per `cost-and-rate-limit-modeling.md
    § Cap-approach response`.
    """

    opus_4_8 = "claude-opus-4-8"
    sonnet_4_6 = "claude-sonnet-4-6"
    haiku_4_5 = "claude-haiku-4-5-20251001"


def _validate_tool_name(name: str) -> str:
    if not _TOOL_NAME_PATTERN.match(name):
        raise ValueError(f"Tool name {name!r} does not match {_TOOL_NAME_PATTERN.pattern!r}")
    return name


class BaseAgentConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: AllowedModel
    prompt: str
    latency_budget_seconds: int = Field(ge=1)
    context_token_budget: int = Field(ge=1)
    output_token_budget: int = Field(ge=1)
    tools: list[str]

    @field_validator("prompt")
    @classmethod
    def prompt_path_exists(cls, v: str) -> str:
        if not (_REPO_ROOT / v).is_file():
            raise ValueError(
                f"Prompt path {v!r} does not resolve to a file under repo root {_REPO_ROOT}"
            )
        return v

    @field_validator("tools")
    @classmethod
    def tool_names_are_well_formed(cls, v: list[str]) -> list[str]:
        for name in v:
            _validate_tool_name(name)
        return v


class AdaptiveAgentConfig(BaseAgentConfig):
    cumulative_tool_call_limit: int = Field(ge=1)
    cumulative_tool_token_budget: int = Field(ge=1)
    tool_caps: dict[str, int]

    @field_validator("tool_caps")
    @classmethod
    def tool_cap_keys_are_well_formed(cls, v: dict[str, int]) -> dict[str, int]:
        for name in v:
            _validate_tool_name(name)
        return v


class AgentsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    agents: dict[AgentName, BaseAgentConfig | AdaptiveAgentConfig]

    @model_validator(mode="after")
    def covers_every_agent_name(self) -> "AgentsConfig":
        present = set(self.agents)
        expected = set(AgentName)
        missing = expected - present
        if missing:
            raise ValueError(
                f"agents.yaml is missing entries for: {sorted(name.value for name in missing)}"
            )
        return self

    @model_validator(mode="after")
    def tool_loop_fields_isolated_to_tool_loop_agents(self) -> "AgentsConfig":
        for name, entry in self.agents.items():
            is_tool_loop_entry = isinstance(entry, AdaptiveAgentConfig)
            is_tool_loop_slot = name in _TOOL_LOOP_AGENTS
            if is_tool_loop_slot and not is_tool_loop_entry:
                raise ValueError(
                    f"Agent {name.value!r} must declare cumulative_tool_call_limit, "
                    f"cumulative_tool_token_budget, and tool_caps"
                )
            if is_tool_loop_entry and not is_tool_loop_slot:
                raise ValueError(
                    f"Agent {name.value!r} must not declare cumulative_tool_call_limit, "
                    f"cumulative_tool_token_budget, or tool_caps; only "
                    f"{sorted(n.value for n in _TOOL_LOOP_AGENTS)} carry those fields"
                )
        return self
