"""Retrieve-brief MCP tool for the synthesizer's downstream agents — story 06a (ALP-205).

Wraps :class:`RetrievalStore.lookup` as a Claude Agent SDK MCP tool the
**decision-layer agents (analyst, strategist, PM)** call at runtime to pull
upstream brief sections by reference ID. The tool accepts a single
``ref_id`` argument and returns the corresponding section text from the
per-invocation retrieval store this synthesizer assembled.

The factory is per-invocation: each call to
:func:`build_retrieve_brief_mcp_server` returns a fresh SDK MCP server
whose handler closes over a single :class:`RetrievalStore`. This isolates
one decision-layer invocation's retrieval surface from another's.

The actual wiring of the returned ``(mcp_servers, allowed_tools)`` tuple
into each decision-layer agent's :class:`ClaudeAgentOptions` lives in the
analyst (ALP-115), strategist (ALP-116), and portfolio-manager (ALP-117)
work trees; this module provides the construction primitive they import.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from alphamind.analysis.synthesizer.retrieval import RetrievalStore

__all__ = ["build_retrieve_brief_mcp_server"]


_REF_ID_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ref_id": {"type": "string"}},
    "required": ["ref_id"],
}


def build_retrieve_brief_mcp_server(
    store: RetrievalStore,
    *,
    server_name: str = "alphamind_synthesizer_retrieval",
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *store*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains
    the single name ``mcp__<server_name>__retrieve_brief``.
    """

    @tool(
        "retrieve_brief",
        "Retrieve the upstream brief section for a reference ID like SA-TECH-3 or QR-CW-1.",
        _REF_ID_INPUT_SCHEMA,
    )
    async def _retrieve_brief(args: dict[str, Any]) -> dict[str, Any]:
        ref_id = args["ref_id"]
        section = store.lookup(ref_id)
        if section is None:
            text = f"No brief section found for reference ID {ref_id!r}."
        else:
            text = section
        return {"content": [{"type": "text", "text": text}]}

    server = create_sdk_mcp_server(name=server_name, tools=[_retrieve_brief])
    allowed = [f"mcp__{server_name}__retrieve_brief"]
    return {server_name: server}, allowed
