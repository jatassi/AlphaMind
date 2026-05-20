"""Portfolio-state MCP tools for the synthesizer agent — story 06b (ALP-206).

Wraps the three :class:`SynthesizerPortfolioStateReader` methods as Claude
Agent SDK MCP tools. Each tool renders the typed value object into compact
LLM-readable text the synthesizer's prompt consumes as a tool return.

The factory is per-invocation: the harness (story 08) constructs a reader
bound to the current portfolio snapshot, calls
:func:`build_portfolio_state_mcp_server`, and assigns the returned
``(mcp_servers, allowed_tools)`` tuple onto its
:class:`ClaudeAgentOptions`.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPortfolioStateReader,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)

__all__ = ["PORTFOLIO_TOOL_NAMES", "build_portfolio_state_mcp_server"]


# Canonical ordering of the three portfolio-state tools the synthesizer can
# call. Surfaced as a public constant so the runner's input-bundle text and
# the harness's `allowed_tools` list both reference the same source.
PORTFOLIO_TOOL_NAMES: tuple[str, ...] = (
    "get_positions_summary",
    "get_active_theses_summary",
    "get_exposure_snapshot",
)


_EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------


def _position_direction_label(position: SynthesizerPositionSummary) -> str:
    """Direction-column text for a position summary.

    An equity / single-leg options position shows its long/short direction.
    A multi-leg strategy has ``direction=None`` (its directionality lives
    per-leg); it shows the strategy-type label instead.
    """
    if position.direction is None:
        return position.strategy_type_label or "strategy"
    return position.direction.value


def _render_positions(positions: tuple[SynthesizerPositionSummary, ...]) -> str:
    """Render position summaries as one line per position."""
    if not positions:
        return "No open positions."
    return "\n".join(
        f"{p.ticker} | {_position_direction_label(p)} | sector={p.sector} | "
        f"size={p.size_pct:.2f}% | age={p.position_age_hours:.1f}h"
        for p in positions
    )


def _render_theses(theses: tuple[SynthesizerThesisSummary, ...]) -> str:
    """Render thesis summaries as one line per thesis."""
    if not theses:
        return "No active theses."
    return "\n".join(
        f"{t.ticker} | {t.summary} | catalyst: {t.key_catalyst} | time: {t.time_expectation_hours}"
        for t in theses
    )


def _render_exposure(snapshot: SynthesizerExposureSnapshot) -> str:
    """Render exposure snapshot as sector breakdown plus net + gross lines."""
    if (
        not snapshot.sector_exposure_pct
        and snapshot.net_directional_pct == 0.0
        and snapshot.gross_exposure_pct == 0.0
    ):
        return "No exposure (all positions flat or empty book)."
    sector_lines = "\n".join(
        f"  {sector}: {pct:.2f}%" for sector, pct in snapshot.sector_exposure_pct.items()
    )
    return (
        f"Sector exposure:\n{sector_lines}\n"
        f"Net directional: {snapshot.net_directional_pct:.2f}%\n"
        f"Gross exposure: {snapshot.gross_exposure_pct:.2f}%"
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_portfolio_state_mcp_server(
    reader: SynthesizerPortfolioStateReader,
    *,
    server_name: str = "alphamind_synthesizer_portfolio",
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *reader*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains
    three names of the form ``mcp__<server_name>__<tool>``.
    """

    @tool(
        "get_positions_summary",
        "Return current open and pending positions as one line per position.",
        _EMPTY_INPUT_SCHEMA,
    )
    async def _get_positions_summary(_args: dict[str, Any]) -> dict[str, Any]:
        positions = reader.get_positions_summary()
        return {"content": [{"type": "text", "text": _render_positions(positions)}]}

    @tool(
        "get_active_theses_summary",
        "Return active theses as one line per thesis.",
        _EMPTY_INPUT_SCHEMA,
    )
    async def _get_active_theses_summary(_args: dict[str, Any]) -> dict[str, Any]:
        theses = reader.get_active_theses_summary()
        return {"content": [{"type": "text", "text": _render_theses(theses)}]}

    @tool(
        "get_exposure_snapshot",
        "Return portfolio exposure: sector breakdown plus net directional and gross exposure.",
        _EMPTY_INPUT_SCHEMA,
    )
    async def _get_exposure_snapshot(_args: dict[str, Any]) -> dict[str, Any]:
        snapshot = reader.get_exposure_snapshot()
        return {"content": [{"type": "text", "text": _render_exposure(snapshot)}]}

    server = create_sdk_mcp_server(
        name=server_name,
        tools=[_get_positions_summary, _get_active_theses_summary, _get_exposure_snapshot],
    )
    allowed = [f"mcp__{server_name}__{name}" for name in PORTFOLIO_TOOL_NAMES]
    return {server_name: server}, allowed
