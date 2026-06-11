"""Per-invocation MCP wrapper around the options chain reader — ALP-948.

Wraps :meth:`OptionsChainReader.chain_slice` as a Claude Agent SDK MCP tool
the analyst and strategist call during reasoning to pull contract-level
options data for one underlying. Mirrors the per-invocation factory shape of
:func:`alphamind.risk_guardrails.state_delivery.validation_tool_mcp.build_validate_guardrail_mcp_server`:
each call returns a fresh SDK MCP server whose handler closes over the
supplied reader.

The tool's input schema carries exactly one required string property —
``underlying``. Filter values (strike band, expiration window, open-interest
floor, contract cap) are config-owned via the reader's
:class:`~alphamind.state.repository.options_chain_read.ChainFilterParams`,
never LLM-supplied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from alphamind.state.repository.options_chain_read import (
    ChainSlice,
    ContractQuote,
    OptionsChainReader,
)

__all__ = [
    "OPTIONS_CHAIN_SERVER_NAME",
    "RETRIEVE_OPTIONS_CHAIN_TOOL_NAME",
    "build_retrieve_options_chain_mcp_server",
]


OPTIONS_CHAIN_SERVER_NAME = "alphamind_options_chain"
"""Canonical SDK MCP server name; the wire-form tool name derives from it."""

RETRIEVE_OPTIONS_CHAIN_TOOL_NAME = f"mcp__{OPTIONS_CHAIN_SERVER_NAME}__retrieve_options_chain"
"""Wire-form tool name rendered into the agents' AVAILABLE TOOLS section."""


_RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["underlying"],
    "properties": {
        "underlying": {
            "type": "string",
            "description": "Underlying equity ticker (e.g., NVDA).",
        },
    },
}


def build_retrieve_options_chain_mcp_server(
    reader: OptionsChainReader,
    *,
    server_name: str = OPTIONS_CHAIN_SERVER_NAME,
) -> tuple[dict[str, McpSdkServerConfig], list[str]]:
    """Build a per-invocation SDK MCP server bound to *reader*.

    Returns ``(mcp_servers_dict, allowed_tool_names)`` ready for direct
    assignment to ``ClaudeAgentOptions.mcp_servers`` and
    ``ClaudeAgentOptions.allowed_tools``. The allowed-tools list contains the
    single name ``mcp__<server_name>__retrieve_options_chain``.
    """

    @tool(
        "retrieve_options_chain",
        (
            "Retrieve the current options chain slice for an underlying: "
            "near-the-money contracts inside the configured strike band, "
            "expiration window, and open-interest floor, each with OCC symbol, "
            "expiration, strike, type, NBBO bid/ask, IV, delta, open interest, "
            "and snapshot age. Premiums for a long single-leg proposal anchor "
            "to this tool's NBBO."
        ),
        _RETRIEVE_OPTIONS_CHAIN_INPUT_SCHEMA,
    )
    async def _retrieve_options_chain(args: dict[str, Any]) -> dict[str, Any]:
        underlying = str(args["underlying"]).strip().upper()
        chain = reader.chain_slice(underlying)
        if chain is None or not chain.contracts:
            text = (
                f"No usable options chain data for {underlying} this invocation — "
                "no quoted contracts survive the configured filters. Prefer the "
                "equity expression for this name."
            )
        else:
            text = _render_chain_slice(chain)
        return {"content": [{"type": "text", "text": text}]}

    server = create_sdk_mcp_server(name=server_name, tools=[_retrieve_options_chain])
    allowed = [f"mcp__{server_name}__retrieve_options_chain"]
    return {server_name: server}, allowed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_chain_slice(chain: ChainSlice) -> str:
    """One header line, one line per contract, plus the omitted note."""
    lines = [
        f"{chain.underlying} options chain — spot {chain.spot:.2f}, as of {chain.as_of.isoformat()}"
    ]
    lines.extend(_render_contract(quote, as_of=chain.as_of) for quote in chain.contracts)
    if chain.omitted is not None:
        lines.append(chain.omitted)
    return "\n".join(lines)


def _render_contract(quote: ContractQuote, *, as_of: datetime) -> str:
    age_minutes = max(0, round((as_of - quote.snapshot_ts).total_seconds() / 60.0))
    cp = "C" if quote.contract_type == "call" else "P"
    return (
        f"  {quote.occ_symbol}  {quote.expiration.isoformat()} {quote.strike:g}{cp}  "
        f"bid/ask {quote.bid:.2f}/{quote.ask:.2f}  IV {quote.implied_volatility:.2f}  "
        f"delta {quote.delta:+.2f}  OI {quote.open_interest}  age {age_minutes}m"
    )
