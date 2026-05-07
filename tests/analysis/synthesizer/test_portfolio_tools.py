"""Tests for the synthesizer portfolio-state MCP tools — story 06b (ALP-206)."""

from __future__ import annotations

from typing import Any

import pytest

from alphamind.analysis.synthesizer.portfolio_tools import build_portfolio_state_mcp_server
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerExposureSnapshot,
    SynthesizerPositionSummary,
    SynthesizerThesisSummary,
)
from alphamind.portfolio_state.records.positions import Direction

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
# Stub readers
# ---------------------------------------------------------------------------


class _StubReader:
    """In-memory reader stub satisfying SynthesizerPortfolioStateReader."""

    def __init__(
        self,
        *,
        positions: tuple[SynthesizerPositionSummary, ...] = (),
        theses: tuple[SynthesizerThesisSummary, ...] = (),
        exposure: SynthesizerExposureSnapshot | None = None,
    ) -> None:
        self._positions = positions
        self._theses = theses
        self._exposure = exposure or SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        )
        self.positions_calls = 0
        self.theses_calls = 0
        self.exposure_calls = 0

    async def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        self.positions_calls += 1
        return self._positions

    async def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        self.theses_calls += 1
        return self._theses

    async def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        self.exposure_calls += 1
        return self._exposure


# ---------------------------------------------------------------------------
# 1. Factory shape
# ---------------------------------------------------------------------------


def test_factory_returns_server_and_allowed_tools() -> None:
    """build_portfolio_state_mcp_server returns (mcp_servers, allowed_tools)
    with three tool names under the default server name."""
    reader = _StubReader()
    mcp_servers, allowed_tools = build_portfolio_state_mcp_server(reader)

    assert "alphamind_synthesizer_portfolio" in mcp_servers
    server_config = mcp_servers["alphamind_synthesizer_portfolio"]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == "alphamind_synthesizer_portfolio"

    assert sorted(allowed_tools) == sorted(
        [
            "mcp__alphamind_synthesizer_portfolio__get_positions_summary",
            "mcp__alphamind_synthesizer_portfolio__get_active_theses_summary",
            "mcp__alphamind_synthesizer_portfolio__get_exposure_snapshot",
        ]
    )


# ---------------------------------------------------------------------------
# 2. Positions handler renders text containing tickers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_handler_returns_text() -> None:
    """positions handler returns text mentioning each ticker the reader yields."""
    reader = _StubReader(
        positions=(
            SynthesizerPositionSummary(
                ticker="NVDA",
                direction=Direction.LONG,
                sector="TECH",
                size_pct=4.5,
                position_age_hours=72.0,
            ),
            SynthesizerPositionSummary(
                ticker="XOM",
                direction=Direction.SHORT,
                sector="ENERGY",
                size_pct=2.1,
                position_age_hours=10.0,
            ),
        ),
    )
    mcp_servers, _ = build_portfolio_state_mcp_server(reader)
    server = mcp_servers["alphamind_synthesizer_portfolio"]["instance"]

    text = await _invoke_mcp_tool(server, "get_positions_summary", {})

    assert "NVDA" in text
    assert "XOM" in text


# ---------------------------------------------------------------------------
# 3. Theses handler renders text containing thesis summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_theses_handler_returns_text() -> None:
    """theses handler returns text containing the thesis summary string."""
    summary = "AI capex cycle re-accelerates demand; data center buildout extends."
    reader = _StubReader(
        theses=(
            SynthesizerThesisSummary(
                position_id="pos-001",
                ticker="NVDA",
                summary=summary,
                key_catalyst="Earnings on 2026-05-22",
                time_expectation_hours=48.0,
            ),
        ),
    )
    mcp_servers, _ = build_portfolio_state_mcp_server(reader)
    server = mcp_servers["alphamind_synthesizer_portfolio"]["instance"]

    text = await _invoke_mcp_tool(server, "get_active_theses_summary", {})

    assert summary in text
    assert "NVDA" in text


# ---------------------------------------------------------------------------
# 4. Exposure handler renders sector and net exposure values
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exposure_handler_returns_text() -> None:
    """exposure handler text contains sector keys and net directional value."""
    reader = _StubReader(
        exposure=SynthesizerExposureSnapshot(
            sector_exposure_pct={"TECH": 12.5, "ENERGY": -3.2},
            net_directional_pct=9.3,
            gross_exposure_pct=15.7,
        ),
    )
    mcp_servers, _ = build_portfolio_state_mcp_server(reader)
    server = mcp_servers["alphamind_synthesizer_portfolio"]["instance"]

    text = await _invoke_mcp_tool(server, "get_exposure_snapshot", {})

    assert "TECH" in text
    assert "ENERGY" in text
    assert "12.5" in text
    assert "-3.2" in text
    assert "9.3" in text
    assert "15.7" in text


# ---------------------------------------------------------------------------
# 5. Empty-state handlers produce graceful messages, not exceptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_state_handlers_return_graceful_text() -> None:
    """Empty positions, empty theses, and zero exposure render to non-empty
    sentences explaining the empty state — not empty strings or exceptions."""
    reader = _StubReader(
        positions=(),
        theses=(),
        exposure=SynthesizerExposureSnapshot(
            sector_exposure_pct={},
            net_directional_pct=0.0,
            gross_exposure_pct=0.0,
        ),
    )
    mcp_servers, _ = build_portfolio_state_mcp_server(reader)
    server = mcp_servers["alphamind_synthesizer_portfolio"]["instance"]

    positions_text = await _invoke_mcp_tool(server, "get_positions_summary", {})
    theses_text = await _invoke_mcp_tool(server, "get_active_theses_summary", {})
    exposure_text = await _invoke_mcp_tool(server, "get_exposure_snapshot", {})

    assert positions_text == "No open positions."
    assert theses_text == "No active theses."
    assert exposure_text == "No exposure (all positions flat or empty book)."


# ---------------------------------------------------------------------------
# 6. Handlers await the reader's async methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handlers_use_async_reader_methods() -> None:
    """Each handler invocation calls the reader's corresponding async method
    exactly once and awaits its return."""
    reader = _StubReader()
    mcp_servers, _ = build_portfolio_state_mcp_server(reader)
    server = mcp_servers["alphamind_synthesizer_portfolio"]["instance"]

    await _invoke_mcp_tool(server, "get_positions_summary", {})
    await _invoke_mcp_tool(server, "get_active_theses_summary", {})
    await _invoke_mcp_tool(server, "get_exposure_snapshot", {})

    assert reader.positions_calls == 1
    assert reader.theses_calls == 1
    assert reader.exposure_calls == 1
