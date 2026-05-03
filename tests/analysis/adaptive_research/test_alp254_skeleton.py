"""Tests for ALP-254: skeleton, agents.yaml verification, shared-types audit.

Covers:
 - adaptive_researcher tools list is exactly the seven non-deferred tools
 - adaptive_researcher tool_caps keys match the same seven tools
 - adaptive_researcher cumulative limits are the design-doc values
 - shared types (Sector, AnomalySeverity, TokensUsed, SignalQuality) are importable
 - yaml validates against the Pydantic schema
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from alphamind.config.models import AdaptiveAgentConfig

CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"

_SEVEN_TOOLS = {
    "news_search",
    "ticker_deep_pull",
    "prediction_markets",
    "sec_lending",
    "short_interest",
    "earnings_calendar",
    "macro_data",
}

_DEFERRED_TOOLS = {"social_sentiment", "options_flow"}


def _load_yaml(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


# Parse once at module load — all tests in this file share the same yaml snapshot.
def _adaptive_entry() -> AdaptiveAgentConfig:
    from alphamind.config.models import AdaptiveAgentConfig, AgentName, AgentsConfig

    config = AgentsConfig.model_validate(_load_yaml(CONFIG_DIR / "agents.yaml"))
    entry = config.agents[AgentName.adaptive_researcher]
    assert isinstance(entry, AdaptiveAgentConfig)
    return entry


_ADAPTIVE = _adaptive_entry()


# ---------------------------------------------------------------------------
# AC1: tools list is exactly the seven non-deferred tools
# ---------------------------------------------------------------------------


def test_adaptive_researcher_tools_is_exactly_seven_non_deferred_tools() -> None:
    """adaptive_researcher.tools contains exactly the seven non-deferred tools."""
    assert set(_ADAPTIVE.tools) == _SEVEN_TOOLS
    assert len(_ADAPTIVE.tools) == 7


# ---------------------------------------------------------------------------
# AC2: tool_caps keys match the same seven tools; no deferred tools remain
# ---------------------------------------------------------------------------


def test_adaptive_researcher_tool_caps_keys_are_exactly_seven_non_deferred() -> None:
    """adaptive_researcher.tool_caps keys match the seven non-deferred tools."""
    assert set(_ADAPTIVE.tool_caps.keys()) == _SEVEN_TOOLS


# ---------------------------------------------------------------------------
# AC3: cumulative limits are the design-doc values
# ---------------------------------------------------------------------------


def test_adaptive_researcher_cumulative_tool_call_limit_is_25() -> None:
    """cumulative_tool_call_limit is 25 per design-doc cap."""
    assert _ADAPTIVE.cumulative_tool_call_limit == 25


def test_adaptive_researcher_cumulative_tool_token_budget_is_4000() -> None:
    """cumulative_tool_token_budget is 4000 per design-doc."""
    assert _ADAPTIVE.cumulative_tool_token_budget == 4000


# ---------------------------------------------------------------------------
# AC7: shared types are importable from _shared
# ---------------------------------------------------------------------------


def test_shared_types_are_all_importable_and_correct_kinds() -> None:
    """Sector, AnomalySeverity, TokensUsed, SignalQuality import from _shared
    with the expected kinds (StrEnum / Pydantic model / Literal alias)."""
    import typing
    from enum import StrEnum

    from pydantic import BaseModel

    from alphamind.analysis._shared import (
        AnomalySeverity,
        Sector,
        SignalQuality,
        TokensUsed,
    )

    assert issubclass(Sector, StrEnum)
    # AnomalySeverity is a typing.Literal alias from distillation.output
    assert typing.get_args(AnomalySeverity), "AnomalySeverity should be a non-empty Literal"
    assert issubclass(SignalQuality, StrEnum)
    assert issubclass(TokensUsed, BaseModel)


# ---------------------------------------------------------------------------
# AC8: yaml validates against the Pydantic schema
# ---------------------------------------------------------------------------


def test_agents_yaml_validates_against_schema_after_edit() -> None:
    """agents.yaml validates without error — the seven-tool adaptive_researcher
    entry satisfies the AgentsConfig Pydantic schema."""
    from alphamind.config.models import AgentName, AgentsConfig

    config = AgentsConfig.model_validate(_load_yaml(CONFIG_DIR / "agents.yaml"))
    assert AgentName.adaptive_researcher in config.agents
