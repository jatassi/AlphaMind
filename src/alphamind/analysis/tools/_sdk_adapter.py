"""Adapt :class:`ToolDefinition` entries into Claude Agent SDK MCP tools.

The on-demand tool registry stores typed sync callables of shape
``Callable[[InputModel], OutputModel]``.  The Claude Agent SDK expects
async handlers of shape ``Callable[[dict], Awaitable[dict]]`` registered
via the ``@tool`` decorator and surfaced through an SDK MCP server.

This module bridges the two without leaking either concern back into the
registry or the harness.  The qualitative-research harness imports
:func:`build_qualitative_mcp_server` to construct the per-invocation
server bound to its SQLAlchemy session.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from sqlalchemy.orm import Session

from alphamind.analysis.tools import TOOLS, ToolDefinition

__all__ = [
    "MCP_SERVER_NAME",
    "build_qualitative_mcp_server",
]

# Stable server name — used both as the ``mcp_servers`` dict key and as
# the prefix in ``allowed_tools`` (``mcp__<server>__<tool>``).  Mirrors
# the qualitative-research design's namespacing for on-demand tools.
MCP_SERVER_NAME = "alphamind_qualitative"


def _allowed_tool_name(tool_name: str) -> str:
    return f"mcp__{MCP_SERVER_NAME}__{tool_name}"


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


def build_qualitative_mcp_server(
    tool_names: Sequence[str],
    session: Session,
) -> tuple[McpSdkServerConfig, list[str]]:
    """Build the SDK MCP server bound to *session* for the qualitative researcher.

    Returns ``(server_config, allowed_tool_names)`` where ``server_config``
    is the value to assign under the ``mcp_servers`` map and
    ``allowed_tool_names`` is the wire-format list to assign to
    ``ClaudeAgentOptions.allowed_tools``.

    The caller is responsible for confirming each name in *tool_names*
    exists in :data:`alphamind.analysis.tools.TOOLS` — this function
    raises :class:`KeyError` otherwise.
    """
    sdk_tools = [_build_sdk_tool(TOOLS[name], session) for name in tool_names]
    server = create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=sdk_tools)
    allowed = [_allowed_tool_name(name) for name in tool_names]
    return server, allowed
