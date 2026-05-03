"""Adapt :class:`ToolDefinition` entries into Claude Agent SDK MCP tools.

The on-demand tool registry stores typed sync callables of shape
``Callable[[InputModel], OutputModel]``.  The Claude Agent SDK expects
async handlers of shape ``Callable[[dict], Awaitable[dict]]`` registered
via the ``@tool`` decorator and surfaced through an SDK MCP server.

This module bridges the two without leaking either concern back into the
registry or the harnesses.  Both the qualitative-research and adaptive-
research harnesses import :func:`build_analysis_mcp_server` to construct
their per-invocation server bound to its SQLAlchemy session, passing the
agent's chosen MCP server name (``alphamind_qualitative`` or
``alphamind_adaptive``) so the two agents' tool allowlists do not share
an MCP namespace.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from sqlalchemy.orm import Session

from alphamind.analysis.tools import TOOLS, ToolDefinition

__all__ = ["build_analysis_mcp_server"]


def _build_sdk_tool(definition: ToolDefinition, session: Session) -> SdkMcpTool[Any]:
    """Wrap a :class:`ToolDefinition` as an :class:`SdkMcpTool`.

    The handler validates ``args`` through the registered Pydantic input
    model, runs the synchronous tool callable, and serialises the
    Pydantic output to a JSON-compatible dict.  ``ToolEnvelope`` fields
    (``data_freshness``, ``quality``) ride through unchanged — they are
    declared on the output model and survive ``model_dump(mode="json")``.
    """
    sync_callable = definition.callable_factory(session)
    input_model = definition.input_model

    @tool(
        definition.name,
        definition.description,
        input_schema=input_model.model_json_schema(),
    )
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        validated = input_model.model_validate(args)
        result = sync_callable(validated)
        payload = result.model_dump(mode="json")
        # MCP protocol accepts text/image/audio/resource content blocks; use
        # text + JSON-encoded payload so ``data_freshness`` and ``quality``
        # ride through unchanged for the model to read.
        return {"content": [{"type": "text", "text": json.dumps(payload)}]}

    return _handler


def build_analysis_mcp_server(
    *,
    server_name: str,
    tool_names: Sequence[str],
    session: Session,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build the SDK MCP server bound to *session* under *server_name*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools`` respectively.

    *server_name* is the MCP namespace (``alphamind_qualitative`` or
    ``alphamind_adaptive``); the same string keys the returned dict and
    prefixes each entry in ``allowed_tool_names`` (``mcp__<name>__<tool>``).

    The caller is responsible for confirming each name in *tool_names*
    exists in :data:`alphamind.analysis.tools.TOOLS` — this function
    raises :class:`KeyError` otherwise.
    """
    sdk_tools = [_build_sdk_tool(TOOLS[name], session) for name in tool_names]
    server = create_sdk_mcp_server(name=server_name, tools=sdk_tools)
    allowed = [f"mcp__{server_name}__{name}" for name in tool_names]
    return {server_name: server}, allowed
