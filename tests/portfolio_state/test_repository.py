"""Tests for the PortfolioStateRepository protocol, stub, and value objects (story 04b)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.regime import (
    RegimeLabel,
)
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    OrderRecord,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.repository import (
    CurrentInvocationMetadata,
    PortfolioPnLInputs,
    PortfolioStateRepository,
    PriorInvocationContext,
    RepositoryConsistencyError,
    RepositoryFixture,
    RepositoryReadError,
    StubPortfolioStateRepository,
)

from ._view_builders import (
    _make_active_risk_parameters,
    _make_cash_ledger,
    _make_drawdown_state,
)

_NOW_UTC = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_NAIVE_DT = datetime.fromisoformat("2026-01-01T12:00:00")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


def test_repository_read_error_is_runtime_error() -> None:
    assert issubclass(RepositoryReadError, RuntimeError)


def test_repository_consistency_error_is_repository_read_error() -> None:
    assert issubclass(RepositoryConsistencyError, RepositoryReadError)


# ---------------------------------------------------------------------------
# PortfolioPnLInputs
# ---------------------------------------------------------------------------


_VALID_PNL: dict[str, Any] = {
    "daily_realized_pnl_usd": 100.0,
    "cumulative_realized_pnl_usd": 500.0,
    "rolling_realized_pnl": {"1d": 50.0, "5d": 200.0},
    "win_rate_pct": 60.0,
    "average_win_size_usd": 200.0,
    "average_loss_size_usd": 100.0,
    "profit_factor": 2.0,
}


def test_portfolio_pnl_inputs_valid() -> None:
    obj = PortfolioPnLInputs(**_VALID_PNL)
    assert obj.win_rate_pct == 60.0


def test_portfolio_pnl_inputs_null_optional_fields() -> None:
    data = {
        **_VALID_PNL,
        "win_rate_pct": None,
        "average_win_size_usd": None,
        "average_loss_size_usd": None,
        "profit_factor": None,
    }
    obj = PortfolioPnLInputs(**data)
    assert obj.win_rate_pct is None


def test_portfolio_pnl_inputs_win_rate_above_100_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "win_rate_pct": 101.0})


def test_portfolio_pnl_inputs_win_rate_below_0_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "win_rate_pct": -1.0})


def test_portfolio_pnl_inputs_negative_average_win_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "average_win_size_usd": -0.01})


def test_portfolio_pnl_inputs_negative_average_loss_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "average_loss_size_usd": -0.01})


def test_portfolio_pnl_inputs_negative_profit_factor_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "profit_factor": -0.01})


def test_portfolio_pnl_inputs_inf_daily_pnl_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "daily_realized_pnl_usd": float("inf")})


def test_portfolio_pnl_inputs_nan_daily_pnl_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "daily_realized_pnl_usd": float("nan")})


def test_portfolio_pnl_inputs_nonfinite_rolling_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PortfolioPnLInputs(**{**_VALID_PNL, "rolling_realized_pnl": {"1d": float("inf")}})


# ---------------------------------------------------------------------------
# CurrentInvocationMetadata
# ---------------------------------------------------------------------------


def test_current_invocation_metadata_valid() -> None:
    obj = CurrentInvocationMetadata(
        invocation_id="inv-001",
        fill_collection_committed_at=_NOW_UTC,
        pipeline_invocation_started_at=_NOW_UTC,
    )
    assert obj.invocation_id == "inv-001"


def test_current_invocation_metadata_nullable_started_at() -> None:
    obj = CurrentInvocationMetadata(
        invocation_id="inv-001",
        fill_collection_committed_at=_NOW_UTC,
        pipeline_invocation_started_at=None,
    )
    assert obj.pipeline_invocation_started_at is None


def test_current_invocation_metadata_empty_id_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        CurrentInvocationMetadata(
            invocation_id="",
            fill_collection_committed_at=_NOW_UTC,
            pipeline_invocation_started_at=None,
        )


def test_current_invocation_metadata_naive_fill_collection_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        CurrentInvocationMetadata(
            invocation_id="inv-001",
            fill_collection_committed_at=_NAIVE_DT,
            pipeline_invocation_started_at=None,
        )


def test_current_invocation_metadata_naive_started_at_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        CurrentInvocationMetadata(
            invocation_id="inv-001",
            fill_collection_committed_at=_NOW_UTC,
            pipeline_invocation_started_at=_NAIVE_DT,
        )


# ---------------------------------------------------------------------------
# PriorInvocationContext
# ---------------------------------------------------------------------------


def test_prior_invocation_context_both_none() -> None:
    obj = PriorInvocationContext(
        prior_invocation_id=None,
        prior_active_risk_parameters=None,
        prior_fill_collection_committed_at=None,
    )
    assert obj.prior_invocation_id is None


def test_prior_invocation_context_both_non_none() -> None:
    params = _make_active_risk_parameters()
    obj = PriorInvocationContext(
        prior_invocation_id="inv-000",
        prior_active_risk_parameters=params,
        prior_fill_collection_committed_at=_NOW_UTC,
    )
    assert obj.prior_invocation_id == "inv-000"


def test_prior_invocation_context_id_none_params_set_raises() -> None:
    params = _make_active_risk_parameters()
    with pytest.raises((ValueError, TypeError)):
        PriorInvocationContext(
            prior_invocation_id=None,
            prior_active_risk_parameters=params,
            prior_fill_collection_committed_at=None,
        )


def test_prior_invocation_context_id_set_params_none_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        PriorInvocationContext(
            prior_invocation_id="inv-000",
            prior_active_risk_parameters=None,
            prior_fill_collection_committed_at=_NOW_UTC,
        )


def test_prior_invocation_context_naive_committed_at_raises() -> None:
    params = _make_active_risk_parameters()
    with pytest.raises((ValueError, TypeError)):
        PriorInvocationContext(
            prior_invocation_id="inv-000",
            prior_active_risk_parameters=params,
            prior_fill_collection_committed_at=_NAIVE_DT,
        )


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_pnl_inputs() -> PortfolioPnLInputs:
    return PortfolioPnLInputs(**_VALID_PNL)


def _make_thesis_quality_aggregate() -> ThesisQualityAggregate:
    return ThesisQualityAggregate(
        as_of_timestamp=_NOW_UTC,
        resolution_counts_by_window=(),
        duration_stats_by_window=(),
        invalidation_timing_stats_by_window=(),
    )


def _make_position_record(position_id: str) -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=10.0,
            average_cost_basis_per_share=150.0,
        ),
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_activity_log_entry(entry_id: str, position_id: str | None = None) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id="inv-001",
        timestamp=_NOW_UTC,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=PMDecisionDetail(
            envelope_id="env-1",
            source_provenance_json={},
            evaluation_json={},
            modifications_json=[],
            resulting_command_ids=(),
            verdict=PMVerdict.APPROVE,
            originating_proposal_json={},
        ),
    )


def _make_bracket_record(position_id: str) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-1",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId("brk-1"),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-0"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_current_invocation_metadata() -> CurrentInvocationMetadata:
    return CurrentInvocationMetadata(
        invocation_id="inv-001",
        fill_collection_committed_at=_NOW_UTC,
        pipeline_invocation_started_at=None,
    )


def _make_prior_invocation_context() -> PriorInvocationContext:
    return PriorInvocationContext(
        prior_invocation_id=None,
        prior_active_risk_parameters=None,
        prior_fill_collection_committed_at=None,
    )


def _make_fixture(
    open_positions: tuple[PositionRecord, ...] = (),
    pending_positions: tuple[PositionRecord, ...] = (),
    active_theses: tuple[ThesisRecord, ...] = (),
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...] = (),
    pending_orders: tuple[OrderRecord, ...] = (),
    intra_invocation_changelog: tuple[ActivityLogEntry, ...] = (),
    recent_pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]] | None = None,
    brackets: tuple[BracketRecord, ...] = (),
) -> RepositoryFixture:
    return RepositoryFixture(
        open_positions=open_positions,
        pending_positions=pending_positions,
        drawdown_state=_make_drawdown_state(),
        portfolio_pnl_inputs=_make_pnl_inputs(),
        active_theses=active_theses,
        recent_thesis_resolutions=recent_thesis_resolutions,
        cash_ledger=_make_cash_ledger(),
        pending_orders=pending_orders,
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=intra_invocation_changelog,
        recent_pm_decision_log=recent_pm_decision_log,
        position_modification_trail=position_modification_trail or {},
        thesis_quality_aggregates=_make_thesis_quality_aggregate(),
        brackets=brackets,
        current_invocation_metadata=_make_current_invocation_metadata(),
        prior_invocation_context=_make_prior_invocation_context(),
    )


# ---------------------------------------------------------------------------
# RepositoryFixture
# ---------------------------------------------------------------------------


def test_repository_fixture_valid() -> None:
    fixture = _make_fixture()
    assert fixture.open_positions == ()


def test_repository_fixture_list_open_positions_stored_as_is() -> None:
    """Post-Pydantic dataclass: tuple-vs-list strictness lives at the codec / consumer
    boundary, not at construction. The frozen dataclass stores whatever sequence
    type the caller passes — downstream code uses :class:`tuple` annotations and
    will fail loudly if it relies on tuple semantics. Documenting the behavior
    change so callers don't accidentally rely on construction-time conversion.
    """
    fixture = RepositoryFixture(
        open_positions=[_make_position_record("pos-1")],  # type: ignore[arg-type]
        pending_positions=(),
        drawdown_state=_make_drawdown_state(),
        portfolio_pnl_inputs=_make_pnl_inputs(),
        active_theses=(),
        recent_thesis_resolutions=(),
        cash_ledger=_make_cash_ledger(),
        pending_orders=(),
        active_risk_parameters=_make_active_risk_parameters(),
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        position_modification_trail={},
        thesis_quality_aggregates=_make_thesis_quality_aggregate(),
        brackets=(),
        current_invocation_metadata=_make_current_invocation_metadata(),
        prior_invocation_context=_make_prior_invocation_context(),
    )
    assert isinstance(fixture.open_positions, list)


# ---------------------------------------------------------------------------
# StubPortfolioStateRepository — happy-path
# ---------------------------------------------------------------------------


def _make_stub_with_position(position_id: str) -> StubPortfolioStateRepository:
    pos = _make_position_record(position_id)
    entry = _make_activity_log_entry("entry-1", position_id)
    bracket = _make_bracket_record(position_id)
    fixture = _make_fixture(
        open_positions=(pos,),
        pending_positions=(pos,),
        intra_invocation_changelog=(entry,),
        recent_pm_decision_log=(entry,),
        position_modification_trail={position_id: (entry,)},
        brackets=(bracket,),
    )
    return StubPortfolioStateRepository(fixture)


def test_stub_isinstance_protocol() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    assert isinstance(stub, PortfolioStateRepository)


def test_stub_get_open_positions() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_open_positions()
    assert len(result) == 1
    assert result[0].position_id == "pos-1"


def test_stub_get_pending_positions() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_pending_positions()
    assert len(result) == 1


def test_stub_get_drawdown_state() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_drawdown_state()
    assert result.current_drawdown_pct == 0.0


def test_stub_get_portfolio_pnl_inputs() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_portfolio_pnl_inputs()
    assert result.win_rate_pct == 60.0


def test_stub_get_active_theses() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_active_theses()
    assert result == ()


def test_stub_get_recent_thesis_resolutions() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    # stub ignores lookback_trading_days
    result = stub.get_recent_thesis_resolutions(lookback_trading_days=5)
    assert result == ()


def test_stub_get_recent_thesis_resolutions_ignores_parameter() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    r1 = stub.get_recent_thesis_resolutions(lookback_trading_days=1)
    r2 = stub.get_recent_thesis_resolutions(lookback_trading_days=999)
    assert r1 == r2


def test_stub_get_cash_ledger() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_cash_ledger()
    assert result.current_cash_usd == 10000.0


def test_stub_get_pending_orders() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_pending_orders()
    assert result == ()


def test_stub_get_active_risk_parameters() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_active_risk_parameters()
    assert result.regime_label == RegimeLabel.NORMAL


def test_stub_get_intra_invocation_changelog() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_intra_invocation_changelog(invocation_id="any-id")
    assert len(result) == 1


def test_stub_get_recent_pm_decision_log() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_recent_pm_decision_log(sliding_window_invocations=3)
    assert len(result) == 1


def test_stub_get_thesis_quality_aggregates() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_thesis_quality_aggregates(now=datetime.now(UTC))
    assert result.resolution_counts_by_window == ()


def test_stub_get_current_invocation_metadata() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_current_invocation_metadata()
    assert result.invocation_id == "inv-001"


def test_stub_get_prior_invocation_context() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = stub.get_prior_invocation_context()
    assert result.prior_invocation_id is None


# ---------------------------------------------------------------------------
# get_position_modification_trail — empty and partial subset
# ---------------------------------------------------------------------------


def test_get_position_modification_trail_all_unknown_returns_empty() -> None:
    stub = _make_stub_with_position("pos-known")
    result = stub.get_position_modification_trail(position_ids=("pos-unknown-1", "pos-unknown-2"))
    assert result == {}


def test_get_position_modification_trail_mixed_returns_known_subset() -> None:
    stub = _make_stub_with_position("pos-known")
    result = stub.get_position_modification_trail(position_ids=("pos-known", "pos-unknown"))
    assert set(result.keys()) == {"pos-known"}
    assert len(result["pos-known"]) == 1


# ---------------------------------------------------------------------------
# get_brackets_for_positions — empty tuple
# ---------------------------------------------------------------------------


def test_get_brackets_for_positions_empty_tuple_returns_empty() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_brackets_for_positions(position_ids=())
    assert result == ()


def test_get_brackets_for_positions_matching_position_id() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_brackets_for_positions(position_ids=("pos-1",))
    assert len(result) == 1
    assert result[0].position_id == "pos-1"


def test_get_brackets_for_positions_non_matching_position_id() -> None:
    stub = _make_stub_with_position("pos-1")
    result = stub.get_brackets_for_positions(position_ids=("pos-other",))
    assert result == ()
