"""Tests for the portfolio-manager get_thesis_components MCP tool — ALP-325."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from alphamind._kernel.ids import (
    PositionId,
    ThesisId,
)
from alphamind._kernel.money import signed_money
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.thesis_quality import (
    InvalidationTimingClass,
    InvalidationTimingStat,
    ResolutionWindowCounts,
    ThesisDurationStat,
    ThesisQualityAggregate,
    TrailingWindow,
)
from alphamind.portfolio_state.consumers.portfolio_manager import (
    SnapshotBackedThesisComponentReader,
)
from alphamind.portfolio_state.consumers.portfolio_manager_thesis_mcp import (
    build_get_thesis_components_mcp_server,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
)
from tests.portfolio_state.consumers._builders import (
    _make_active_risk_params,
    _make_cash_ledger,
    _make_drawdown_state,
    _make_open_position,
)

# ---------------------------------------------------------------------------
# Shared timestamps / IDs
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)
_T1 = datetime(2025, 1, 1, 10, 1, 0, tzinfo=UTC)
_T2 = datetime(2025, 1, 1, 10, 2, 0, tzinfo=UTC)
_INV_ID = "inv-mcp-001"
_SERVER_NAME = "alphamind_portfolio_state_thesis_components"

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
# Fixture builders (minimal — only what the MCP tests need)
# ---------------------------------------------------------------------------


def _make_thesis_component(
    ctype: ThesisComponentType,
    cid: str,
    thesis_id: str = "THESIS-001",
) -> ThesisComponent:
    return ThesisComponent(
        component_id=cid,
        thesis_id=ThesisId(thesis_id),
        component_type=ctype,
        linked_bracket_leg_type=None,
        instrument_reference="AAPL",
        narrative="Narrative text.",
        key_assumptions=(KeyAssumption(text="Assumption", outcome=None),),
        generation_timestamp=_T0,
        resolution_outcome=None,
        resolution_notes=None,
    )


def _make_thesis(
    thesis_id: str = "THESIS-001",
    position_id: str = "POS-001",
) -> ThesisRecord:
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Long AAPL on momentum.",
        components=(
            _make_thesis_component(ThesisComponentType.ENTRY_RATIONALE, "comp-1", thesis_id),
            _make_thesis_component(ThesisComponentType.TARGET_RATIONALE, "comp-2", thesis_id),
            _make_thesis_component(ThesisComponentType.INVALIDATION_RATIONALE, "comp-3", thesis_id),
        ),
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_T0,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=_T0 + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
        key_catalyst="earnings beat",
    )


def _make_pnl() -> PortfolioPnL:
    return PortfolioPnL(
        total_unrealized_pnl_usd=signed_money(500.0),
        total_unrealized_pnl_pct_of_portfolio=1.5,
        daily_realized_pnl_usd=signed_money(200.0),
        daily_total_pnl_usd=signed_money(700.0),
        cumulative_realized_pnl_usd=signed_money(10000.0),
        rolling_realized_pnl={
            "1d": signed_money(200.0),
            "3d": signed_money(600.0),
            "5d": signed_money(1000.0),
            "20d": signed_money(3000.0),
        },
        win_rate_pct=None,
        average_win_size_usd=None,
        average_loss_size_usd=None,
        profit_factor=None,
    )


def _make_directional() -> DirectionalExposure:
    return DirectionalExposure(
        total_long_delta_adjusted_usd=signed_money(0.0),
        total_short_delta_adjusted_usd=signed_money(0.0),
        net_directional_pct_of_portfolio=0.0,
        gross_pct_of_portfolio=0.0,
    )


def _make_snapshot_with_thesis(
    position_id: str = "POS-001",
    thesis_id: str = "THESIS-001",
) -> PortfolioStateSnapshot:
    """Minimal snapshot with one open position and one active thesis."""
    return PortfolioStateSnapshot(
        invocation_id=_INV_ID,
        fill_collection_committed_at=_T0,
        snapshot_assembled_at=_T1,
        pipeline_invocation_started_at=_T2,
        open_positions=(_make_open_position(position_id),),
        pending_positions=(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown_state(),
        active_theses=(_make_thesis(thesis_id, position_id),),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=RiskBudgetConsumption(entries=()),
        active_risk_parameters=_make_active_risk_params(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality(),
        brackets=(),
    )


def _make_empty_snapshot() -> PortfolioStateSnapshot:
    """Minimal snapshot with no positions or theses."""
    return PortfolioStateSnapshot(
        invocation_id=_INV_ID,
        fill_collection_committed_at=_T0,
        snapshot_assembled_at=_T1,
        pipeline_invocation_started_at=None,
        open_positions=(),
        pending_positions=(),
        sector_exposure=(),
        directional_exposure=_make_directional(),
        portfolio_pnl=_make_pnl(),
        drawdown=_make_drawdown_state(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        risk_budget=RiskBudgetConsumption(entries=()),
        active_risk_parameters=_make_active_risk_params(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality(),
        brackets=(),
    )


def _make_thesis_quality() -> ThesisQualityAggregate:
    rwc = ResolutionWindowCounts(
        window=TrailingWindow.FIVE_DAYS,
        total_resolutions=0,
        validated=0,
        profitable_but_wrong=0,
        invalidated_stopped_correctly=0,
        invalidated_wrong_on_exit=0,
        cancelled_never_entered=0,
    )
    dur = ThesisDurationStat(
        window=TrailingWindow.FIVE_DAYS,
        mean_actual_to_expected_ratio=1.0,
        median_actual_to_expected_ratio=1.0,
        count=0,
    )
    inv_timing = InvalidationTimingStat(
        window=TrailingWindow.FIVE_DAYS,
        class_distribution={
            InvalidationTimingClass.EARLY: 0,
            InvalidationTimingClass.ON_TIME: 0,
            InvalidationTimingClass.LATE: 0,
        },
        mean_position_age_at_invalidation_hours=0.0,
    )
    return ThesisQualityAggregate(
        as_of_timestamp=_T0,
        resolution_counts_by_window=(rwc,),
        duration_stats_by_window=(dur,),
        invalidation_timing_stats_by_window=(inv_timing,),
    )


# ---------------------------------------------------------------------------
# 1. Factory shape
# ---------------------------------------------------------------------------


def test_factory_returns_mcp_server_and_allowed_tools() -> None:
    """build_get_thesis_components_mcp_server returns (mcp_servers, allowed_tools)
    with one tool name under the default server name."""
    snapshot = _make_empty_snapshot()
    reader = SnapshotBackedThesisComponentReader(snapshot)
    mcp_servers, allowed_tools = build_get_thesis_components_mcp_server(reader)

    assert _SERVER_NAME in mcp_servers
    server_config = mcp_servers[_SERVER_NAME]
    assert server_config["type"] == "sdk"
    assert server_config["name"] == _SERVER_NAME

    assert allowed_tools == [
        f"mcp__{_SERVER_NAME}__get_thesis_components",
    ]


# ---------------------------------------------------------------------------
# 2. Tool returns components for a held position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_components_for_held_position() -> None:
    """The tool returns a JSON-serialized list of ThesisComponent records
    when position_id is found in the snapshot."""
    snapshot = _make_snapshot_with_thesis(
        position_id=PositionId("POS-001"), thesis_id=ThesisId("THESIS-001")
    )
    reader = SnapshotBackedThesisComponentReader(snapshot)
    mcp_servers, _ = build_get_thesis_components_mcp_server(reader)
    server = mcp_servers[_SERVER_NAME]["instance"]

    text = await _invoke_mcp_tool(server, "get_thesis_components", {"position_id": "POS-001"})

    data = json.loads(text)
    assert isinstance(data, list)
    assert len(data) == 3  # ENTRY + TARGET + INVALIDATION


# ---------------------------------------------------------------------------
# 3. Tool returns empty list for unknown position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_empty_for_unknown_position() -> None:
    """The tool returns '[]' when position_id has no thesis — no exception raised."""
    snapshot = _make_empty_snapshot()
    reader = SnapshotBackedThesisComponentReader(snapshot)
    mcp_servers, _ = build_get_thesis_components_mcp_server(reader)
    server = mcp_servers[_SERVER_NAME]["instance"]

    text = await _invoke_mcp_tool(server, "get_thesis_components", {"position_id": "UNKNOWN"})

    assert text == "[]"


# ---------------------------------------------------------------------------
# 4. Closure isolation — two factory calls yield distinct servers
# ---------------------------------------------------------------------------


def test_factory_does_not_share_state_across_calls() -> None:
    """Two separate calls to build_get_thesis_components_mcp_server return
    distinct mcp_servers instances (closure-isolation property)."""
    snapshot_a = _make_empty_snapshot()
    snapshot_b = _make_snapshot_with_thesis()
    reader_a = SnapshotBackedThesisComponentReader(snapshot_a)
    reader_b = SnapshotBackedThesisComponentReader(snapshot_b)

    mcp_servers_a, _ = build_get_thesis_components_mcp_server(reader_a)
    mcp_servers_b, _ = build_get_thesis_components_mcp_server(reader_b)

    server_a = mcp_servers_a[_SERVER_NAME]["instance"]
    server_b = mcp_servers_b[_SERVER_NAME]["instance"]

    assert server_a is not server_b
