"""Per-invocation MCP wrapper exposing get_thesis_components to the PM agent — ALP-325.

Wraps :class:`PortfolioManagerThesisComponentReader` as a Claude Agent SDK MCP
tool the portfolio manager calls during reasoning to pull component-level thesis
structure for a held position on demand. The factory captures a
``PortfolioManagerThesisComponentReader`` instance in the tool closure; each
call to :func:`build_get_thesis_components_mcp_server` returns a fresh SDK MCP
server whose handler closes over its own reader. This isolates one
decision-layer invocation's thesis retrieval surface from another's.

Wire-form tool name:
  ``mcp__alphamind_portfolio_state_thesis_components__get_thesis_components``

Cross-links:
  - PM ``<tool_policy>`` in ``prompts/decision/pm.md``
  - Design doc § Thesis-component retrieval in
    ``docs/design/04-decision-layer/portfolio-manager.md``
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from alphamind.portfolio_state.consumers.portfolio_manager import (
    PortfolioManagerThesisComponentReader,
)


def _json_default(obj: object) -> object:
    """Convert non-JSON-native values to JSON-friendly forms.

    Used as ``default=`` to ``json.dumps`` so frozen-dataclass payloads carrying
    enums / datetimes / dates serialise without per-call adapters.
    """
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    msg = f"object of type {type(obj).__name__} is not JSON-serializable"
    raise TypeError(msg)


__all__ = ["build_get_thesis_components_mcp_server"]


_GET_THESIS_COMPONENTS_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"position_id": {"type": "string"}},
    "required": ["position_id"],
}


def build_get_thesis_components_mcp_server(
    reader: PortfolioManagerThesisComponentReader,
    *,
    server_name: str = "alphamind_portfolio_state_thesis_components",
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *reader*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains
    the single name ``mcp__<server_name>__get_thesis_components``.

    The factory captures *reader* in the tool closure. Each call produces a
    fresh, independent MCP server — calling the factory twice with the same
    reader yields two distinct server instances (closure-isolation property).
    """

    @tool(
        "get_thesis_components",
        (
            "Retrieve the component-level thesis structure for a held position. "
            "Returns a JSON-serialized list of ThesisComponent records. "
            "Returns an empty list when no active thesis exists for the position."
        ),
        _GET_THESIS_COMPONENTS_INPUT_SCHEMA,
    )
    async def _get_thesis_components(args: dict[str, Any]) -> dict[str, Any]:
        position_id: str = args["position_id"]
        components = await reader.get_thesis_components(position_id)
        payload = [dataclasses.asdict(c) for c in components]
        return {"content": [{"type": "text", "text": json.dumps(payload, default=_json_default)}]}

    server = create_sdk_mcp_server(name=server_name, tools=[_get_thesis_components])
    allowed = [f"mcp__{server_name}__get_thesis_components"]
    return {server_name: server}, allowed
