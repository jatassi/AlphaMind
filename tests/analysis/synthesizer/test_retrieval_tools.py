"""Tests for the synthesizer retrieve_brief MCP tool — story 06a (ALP-205)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.analysis.synthesizer.models import BriefBundle, BriefSource
from alphamind.analysis.synthesizer.retrieval import assemble_retrieval_store
from alphamind.analysis.synthesizer.retrieval_tools import build_retrieve_brief_mcp_server

# ---------------------------------------------------------------------------
# In-process MCP-server handler invocation helper
# ---------------------------------------------------------------------------


async def _invoke_mcp_tool(server: Any, tool_name: str, args: dict[str, Any]) -> str:
    """Drive the in-process MCP server's ``tools/call`` handler for *tool_name*.

    Returns the rendered text from the handler's first content block.
    """
    from mcp.types import CallToolRequest, CallToolRequestParams

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=tool_name, arguments=args),
    )
    handler = server.request_handlers[CallToolRequest]
    result = await handler(request)
    content_blocks = result.root.content
    assert content_blocks, "tool returned no content blocks"
    block = content_blocks[0]
    text = block.text
    assert isinstance(text, str)
    return text


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


def _bundle(source: BriefSource, text: str) -> BriefBundle:
    """Build a :class:`BriefBundle` with a fixed freshness for tests."""
    return BriefBundle(
        source=source,
        text=text,
        freshness=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# 1. Factory shape
# ---------------------------------------------------------------------------


def test_factory_returns_server_and_allowed_tools() -> None:
    """build_retrieve_brief_mcp_server returns (mcp_servers, allowed_tools)
    with one tool name under the default server name."""
    store = assemble_retrieval_store([])
    mcp_servers, allowed_tools = build_retrieve_brief_mcp_server(store)

    assert "alphamind_synthesizer_retrieval" in mcp_servers
    server_config = mcp_servers["alphamind_synthesizer_retrieval"]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_synthesizer_retrieval"

    assert allowed_tools == [
        "mcp__alphamind_synthesizer_retrieval__retrieve_brief",
    ]


# ---------------------------------------------------------------------------
# 2. Handler resolves a known reference ID to its section text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_section_for_known_id() -> None:
    """The handler returns the upstream brief section text when ref_id is known."""
    bundle = _bundle(
        BriefSource.SA_TECH,
        "[SA-TECH-1] First finding.\n  some detail.\n[SA-TECH-2] Second finding.\n",
    )
    store = assemble_retrieval_store([bundle])
    mcp_servers, _ = build_retrieve_brief_mcp_server(store)
    server = mcp_servers["alphamind_synthesizer_retrieval"]["instance"]

    text = await _invoke_mcp_tool(server, "retrieve_brief", {"ref_id": "SA-TECH-1"})

    assert "First finding." in text
    assert "some detail." in text
    assert "Second finding." not in text


# ---------------------------------------------------------------------------
# 3. Handler returns graceful text (not exception) on unknown ref ID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_returns_text_for_unknown_id() -> None:
    """An unknown ref_id resolves to a non-empty text message — handler does
    not raise, since raising would break the SDK loop."""
    store = assemble_retrieval_store(
        [_bundle(BriefSource.SA_TECH, "[SA-TECH-1] only entry.\n")],
    )
    mcp_servers, _ = build_retrieve_brief_mcp_server(store)
    server = mcp_servers["alphamind_synthesizer_retrieval"]["instance"]

    text = await _invoke_mcp_tool(server, "retrieve_brief", {"ref_id": "SA-TECH-99"})

    assert text  # non-empty
    assert "SA-TECH-99" in text
    assert "only entry." not in text


# ---------------------------------------------------------------------------
# 4. Sub-typed prefixes (SA-TECH-ANOM-3, QR-CW-2) resolve correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_handles_subtype_prefixes() -> None:
    """Sub-typed prefixes (`SA-TECH-ANOM-3`, `QR-CW-2`) resolve to their own
    sections, not to the base-prefix sibling."""
    tech_bundle = _bundle(
        BriefSource.SA_TECH,
        "[SA-TECH-1] base finding one.\n"
        "[SA-TECH-ANOM-3] anomaly three details.\n"
        "[SA-TECH-TC-1] thesis candidate one.\n",
    )
    qr_bundle = _bundle(
        BriefSource.QR,
        "[QR-1] baseline narrative.\n[QR-CW-2] catalyst watch two.\n",
    )
    store = assemble_retrieval_store([tech_bundle, qr_bundle])
    mcp_servers, _ = build_retrieve_brief_mcp_server(store)
    server = mcp_servers["alphamind_synthesizer_retrieval"]["instance"]

    anom_text = await _invoke_mcp_tool(server, "retrieve_brief", {"ref_id": "SA-TECH-ANOM-3"})
    cw_text = await _invoke_mcp_tool(server, "retrieve_brief", {"ref_id": "QR-CW-2"})

    assert "anomaly three details." in anom_text
    assert "base finding one." not in anom_text
    assert "catalyst watch two." in cw_text
    assert "baseline narrative." not in cw_text


# ---------------------------------------------------------------------------
# 5. Each factory call produces an isolated server (closure over its own store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_call_produces_isolated_server() -> None:
    """Two factory calls with different stores yield handlers that resolve
    the same ref_id to each store's own section text."""
    store_a = assemble_retrieval_store(
        [_bundle(BriefSource.SA_TECH, "[SA-TECH-1] alpha section.\n")],
    )
    store_b = assemble_retrieval_store(
        [_bundle(BriefSource.SA_TECH, "[SA-TECH-1] beta section.\n")],
    )

    mcp_servers_a, _ = build_retrieve_brief_mcp_server(store_a)
    mcp_servers_b, _ = build_retrieve_brief_mcp_server(store_b)
    server_a = mcp_servers_a["alphamind_synthesizer_retrieval"]["instance"]
    server_b = mcp_servers_b["alphamind_synthesizer_retrieval"]["instance"]

    text_a = await _invoke_mcp_tool(server_a, "retrieve_brief", {"ref_id": "SA-TECH-1"})
    text_b = await _invoke_mcp_tool(server_b, "retrieve_brief", {"ref_id": "SA-TECH-1"})

    assert "alpha section." in text_a
    assert "beta section." not in text_a
    assert "beta section." in text_b
    assert "alpha section." not in text_b
