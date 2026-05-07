"""Tests for the PortfolioStateRepository protocol, stub, and value objects (story 04b)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    CashLedger,
    DrawdownState,
    RegimeLabel,
    RegimeTransitionState,
    RiskBudgetConsumption,
    RiskZone,
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
    InstrumentType,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
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


_VALID_PNL = {
    "daily_realized_pnl_usd": 100.0,
    "cumulative_realized_pnl_usd": 500.0,
    "rolling_realized_pnl": {"1d": 50.0, "5d": 200.0},
    "win_rate_pct": 60.0,
    "average_win_size_usd": 200.0,
    "average_loss_size_usd": 100.0,
    "profit_factor": 2.0,
}


def test_portfolio_pnl_inputs_valid() -> None:
    obj = PortfolioPnLInputs.model_validate(_VALID_PNL)
    assert obj.win_rate_pct == 60.0


def test_portfolio_pnl_inputs_null_optional_fields() -> None:
    data = {
        **_VALID_PNL,
        "win_rate_pct": None,
        "average_win_size_usd": None,
        "average_loss_size_usd": None,
        "profit_factor": None,
    }
    obj = PortfolioPnLInputs.model_validate(data)
    assert obj.win_rate_pct is None


def test_portfolio_pnl_inputs_win_rate_above_100_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "win_rate_pct": 101.0})


def test_portfolio_pnl_inputs_win_rate_below_0_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "win_rate_pct": -1.0})


def test_portfolio_pnl_inputs_negative_average_win_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "average_win_size_usd": -0.01})


def test_portfolio_pnl_inputs_negative_average_loss_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "average_loss_size_usd": -0.01})


def test_portfolio_pnl_inputs_negative_profit_factor_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "profit_factor": -0.01})


def test_portfolio_pnl_inputs_inf_daily_pnl_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "daily_realized_pnl_usd": float("inf")})


def test_portfolio_pnl_inputs_nan_daily_pnl_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate({**_VALID_PNL, "daily_realized_pnl_usd": float("nan")})


def test_portfolio_pnl_inputs_nonfinite_rolling_raises() -> None:
    with pytest.raises(ValidationError):
        PortfolioPnLInputs.model_validate(
            {**_VALID_PNL, "rolling_realized_pnl": {"1d": float("inf")}}
        )


# ---------------------------------------------------------------------------
# CurrentInvocationMetadata
# ---------------------------------------------------------------------------


def test_current_invocation_metadata_valid() -> None:
    obj = CurrentInvocationMetadata.model_validate(
        {
            "invocation_id": "inv-001",
            "phase1_committed_at": _NOW_UTC,
            "pipeline_invocation_started_at": _NOW_UTC,
        }
    )
    assert obj.invocation_id == "inv-001"


def test_current_invocation_metadata_nullable_started_at() -> None:
    obj = CurrentInvocationMetadata.model_validate(
        {
            "invocation_id": "inv-001",
            "phase1_committed_at": _NOW_UTC,
            "pipeline_invocation_started_at": None,
        }
    )
    assert obj.pipeline_invocation_started_at is None


def test_current_invocation_metadata_empty_id_raises() -> None:
    with pytest.raises(ValidationError):
        CurrentInvocationMetadata.model_validate(
            {
                "invocation_id": "",
                "phase1_committed_at": _NOW_UTC,
                "pipeline_invocation_started_at": None,
            }
        )


def test_current_invocation_metadata_naive_phase1_raises() -> None:
    with pytest.raises(ValidationError):
        CurrentInvocationMetadata.model_validate(
            {
                "invocation_id": "inv-001",
                "phase1_committed_at": _NAIVE_DT,
                "pipeline_invocation_started_at": None,
            }
        )


def test_current_invocation_metadata_naive_started_at_raises() -> None:
    with pytest.raises(ValidationError):
        CurrentInvocationMetadata.model_validate(
            {
                "invocation_id": "inv-001",
                "phase1_committed_at": _NOW_UTC,
                "pipeline_invocation_started_at": _NAIVE_DT,
            }
        )


# ---------------------------------------------------------------------------
# PriorInvocationContext
# ---------------------------------------------------------------------------


def _make_active_risk_params() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet.model_validate(
        {
            "regime_label": RegimeLabel.NORMAL,
            "transition_state": RegimeTransitionState.STABLE,
            "transition_invocations_remaining": 0,
            "parameter_change_flag": False,
            "entries": [],
            "active_overlays": [],
        }
    )


def test_prior_invocation_context_both_none() -> None:
    obj = PriorInvocationContext.model_validate(
        {
            "prior_invocation_id": None,
            "prior_active_risk_parameters": None,
            "prior_phase1_committed_at": None,
        }
    )
    assert obj.prior_invocation_id is None


def test_prior_invocation_context_both_non_none() -> None:
    params = _make_active_risk_params()
    obj = PriorInvocationContext.model_validate(
        {
            "prior_invocation_id": "inv-000",
            "prior_active_risk_parameters": params,
            "prior_phase1_committed_at": _NOW_UTC,
        }
    )
    assert obj.prior_invocation_id == "inv-000"


def test_prior_invocation_context_id_none_params_set_raises() -> None:
    params = _make_active_risk_params()
    with pytest.raises(ValidationError):
        PriorInvocationContext.model_validate(
            {
                "prior_invocation_id": None,
                "prior_active_risk_parameters": params,
                "prior_phase1_committed_at": None,
            }
        )


def test_prior_invocation_context_id_set_params_none_raises() -> None:
    with pytest.raises(ValidationError):
        PriorInvocationContext.model_validate(
            {
                "prior_invocation_id": "inv-000",
                "prior_active_risk_parameters": None,
                "prior_phase1_committed_at": _NOW_UTC,
            }
        )


def test_prior_invocation_context_naive_committed_at_raises() -> None:
    params = _make_active_risk_params()
    with pytest.raises(ValidationError):
        PriorInvocationContext.model_validate(
            {
                "prior_invocation_id": "inv-000",
                "prior_active_risk_parameters": params,
                "prior_phase1_committed_at": _NAIVE_DT,
            }
        )


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _make_pnl_inputs() -> PortfolioPnLInputs:
    return PortfolioPnLInputs.model_validate(_VALID_PNL)


def _make_cash_ledger() -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": 10000.0,
            "settled_cash_usd": 9000.0,
            "reserved_capital_usd": 500.0,
            "available_buying_power_usd": 8500.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": [],
            "cash_pct_of_portfolio": 80.0,
            "true_deployable_capital_usd": 8000.0,
            "regt_excess_trailing_30d_usd": 100.0,
            "regt_excess_trailing_90d_usd": 300.0,
            "regt_excess_lifetime_usd": 500.0,
        }
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": 100000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 5.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption.model_validate({"entries": []})


def _make_active_risk_parameters() -> ActiveRiskParameterSet:
    return _make_active_risk_params()


def _make_thesis_quality_aggregate() -> ThesisQualityAggregate:
    return ThesisQualityAggregate.model_validate(
        {
            "as_of_timestamp": _NOW_UTC,
            "resolution_counts_by_window": [],
            "duration_stats_by_window": [],
            "invalidation_timing_stats_by_window": [],
            "signal_hit_rates": [],
            "signal_to_thesis_conversions": [],
            "conviction_calibration": [],
            "conviction_sizing_deviation_by_window": [],
            "performance_attribution": [],
            "alpha_beta_decomposition_by_window": [],
        }
    )


def _make_position_record(position_id: str) -> PositionRecord:
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": None,
            "bracket_id": None,
            "status": PositionStatus.PENDING,
            "direction": Direction.LONG,
            "entry_timestamp": None,
            "instrument_type": InstrumentType.EQUITY,
            "equity_details": {
                "ticker": "AAPL",
                "share_count": 10.0,
                "average_cost_basis_per_share": 150.0,
            },
            "execution_history": [],
            "realized_pnl_to_date_usd": None,
            "current_market_value_usd": 1500.0,
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "position_weight_pct": 5.0,
            "position_age_hours": 0.0,
            "notional_exposure_usd": 1500.0,
            "delta_adjusted_exposure_usd": 1500.0,
            "distance_to_target_usd": None,
            "distance_to_stop_usd": None,
            "risk_reward_at_current": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_activity_log_entry(entry_id: str, position_id: str | None = None) -> ActivityLogEntry:
    return ActivityLogEntry.model_validate(
        {
            "entry_id": entry_id,
            "invocation_id": "inv-001",
            "timestamp": _NOW_UTC,
            "event_type": EventType.PM_DECISION,
            "event_group": EventGroup.PM_DECISION,
            "position_id": position_id,
            "order_id": None,
            "thesis_id": None,
            "source": EventSource.COMMAND_EXECUTOR,
            "detail": PMDecisionDetail(
                envelope_id="env-1",
                source_provenance_json={},
                evaluation_json={},
                modifications_json=[],
                resulting_command_ids=(),
                verdict=PMVerdict.APPROVE,
            ),
        }
    )


def _make_bracket_record(position_id: str) -> BracketRecord:
    leg = BracketLeg.model_validate(
        {
            "leg_id": "leg-1",
            "leg_type": BracketLegType.PRICE_STOP,
            "order_id": "ord-1",
            "trigger": PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
            "enforcement": BracketLegEnforcement.MECHANICAL,
            "status": BracketLegStatus.ACTIVE,
        }
    )
    return BracketRecord.model_validate(
        {
            "bracket_id": "brk-1",
            "position_id": position_id,
            "status": BracketStatus.ACTIVE,
            "entry_order_id": "ord-0",
            "protective_legs": [leg],
            "modification_history": [],
            "corporate_action_cancellation_reason": None,
        }
    )


def _make_current_invocation_metadata() -> CurrentInvocationMetadata:
    return CurrentInvocationMetadata.model_validate(
        {
            "invocation_id": "inv-001",
            "phase1_committed_at": _NOW_UTC,
            "pipeline_invocation_started_at": None,
        }
    )


def _make_prior_invocation_context() -> PriorInvocationContext:
    return PriorInvocationContext.model_validate(
        {
            "prior_invocation_id": None,
            "prior_active_risk_parameters": None,
            "prior_phase1_committed_at": None,
        }
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
    return RepositoryFixture.model_validate(
        {
            "open_positions": open_positions,
            "pending_positions": pending_positions,
            "drawdown_state": _make_drawdown_state(),
            "portfolio_pnl_inputs": _make_pnl_inputs(),
            "active_theses": active_theses,
            "recent_thesis_resolutions": recent_thesis_resolutions,
            "cash_ledger": _make_cash_ledger(),
            "pending_orders": pending_orders,
            "risk_budget": _make_risk_budget(),
            "active_risk_parameters": _make_active_risk_parameters(),
            "intra_invocation_changelog": intra_invocation_changelog,
            "recent_pm_decision_log": recent_pm_decision_log,
            "position_modification_trail": position_modification_trail or {},
            "thesis_quality_aggregates": _make_thesis_quality_aggregate(),
            "brackets": brackets,
            "current_invocation_metadata": _make_current_invocation_metadata(),
            "prior_invocation_context": _make_prior_invocation_context(),
        }
    )


# ---------------------------------------------------------------------------
# RepositoryFixture
# ---------------------------------------------------------------------------


def test_repository_fixture_valid() -> None:
    fixture = _make_fixture()
    assert fixture.open_positions == ()


def test_repository_fixture_non_tuple_open_positions_raises() -> None:
    with pytest.raises(ValidationError):
        RepositoryFixture.model_validate(
            {
                "open_positions": [_make_position_record("pos-1")],  # list not tuple
                "pending_positions": (),
                "drawdown_state": _make_drawdown_state(),
                "portfolio_pnl_inputs": _make_pnl_inputs(),
                "active_theses": (),
                "recent_thesis_resolutions": (),
                "cash_ledger": _make_cash_ledger(),
                "pending_orders": (),
                "risk_budget": _make_risk_budget(),
                "active_risk_parameters": _make_active_risk_parameters(),
                "intra_invocation_changelog": (),
                "recent_pm_decision_log": (),
                "position_modification_trail": {},
                "thesis_quality_aggregates": _make_thesis_quality_aggregate(),
                "brackets": (),
                "current_invocation_metadata": _make_current_invocation_metadata(),
                "prior_invocation_context": _make_prior_invocation_context(),
            }
        )


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
    result = asyncio.run(stub.get_open_positions())
    assert len(result) == 1
    assert result[0].position_id == "pos-1"


def test_stub_get_pending_positions() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_pending_positions())
    assert len(result) == 1


def test_stub_get_drawdown_state() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_drawdown_state())
    assert result.current_drawdown_pct == 0.0


def test_stub_get_portfolio_pnl_inputs() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_portfolio_pnl_inputs())
    assert result.win_rate_pct == 60.0


def test_stub_get_active_theses() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_active_theses())
    assert result == ()


def test_stub_get_recent_thesis_resolutions() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    # stub ignores lookback_trading_days
    result = asyncio.run(stub.get_recent_thesis_resolutions(lookback_trading_days=5))
    assert result == ()


def test_stub_get_recent_thesis_resolutions_ignores_parameter() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    r1 = asyncio.run(stub.get_recent_thesis_resolutions(lookback_trading_days=1))
    r2 = asyncio.run(stub.get_recent_thesis_resolutions(lookback_trading_days=999))
    assert r1 == r2


def test_stub_get_cash_ledger() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_cash_ledger())
    assert result.current_cash_usd == 10000.0


def test_stub_get_pending_orders() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_pending_orders())
    assert result == ()


def test_stub_get_risk_budget_consumption() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_risk_budget_consumption())
    assert result.entries == ()


def test_stub_get_active_risk_parameters() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_active_risk_parameters())
    assert result.regime_label == RegimeLabel.NORMAL


def test_stub_get_intra_invocation_changelog() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_intra_invocation_changelog(invocation_id="any-id"))
    assert len(result) == 1


def test_stub_get_recent_pm_decision_log() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_recent_pm_decision_log(sliding_window_invocations=3))
    assert len(result) == 1


def test_stub_get_thesis_quality_aggregates() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_thesis_quality_aggregates())
    assert result.resolution_counts_by_window == ()


def test_stub_get_current_invocation_metadata() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_current_invocation_metadata())
    assert result.invocation_id == "inv-001"


def test_stub_get_prior_invocation_context() -> None:
    stub = StubPortfolioStateRepository(_make_fixture())
    result = asyncio.run(stub.get_prior_invocation_context())
    assert result.prior_invocation_id is None


# ---------------------------------------------------------------------------
# get_position_modification_trail — empty and partial subset
# ---------------------------------------------------------------------------


def test_get_position_modification_trail_all_unknown_returns_empty() -> None:
    stub = _make_stub_with_position("pos-known")
    result = asyncio.run(
        stub.get_position_modification_trail(position_ids=("pos-unknown-1", "pos-unknown-2"))
    )
    assert result == {}


def test_get_position_modification_trail_mixed_returns_known_subset() -> None:
    stub = _make_stub_with_position("pos-known")
    result = asyncio.run(
        stub.get_position_modification_trail(position_ids=("pos-known", "pos-unknown"))
    )
    assert set(result.keys()) == {"pos-known"}
    assert len(result["pos-known"]) == 1


# ---------------------------------------------------------------------------
# get_brackets_for_positions — empty tuple
# ---------------------------------------------------------------------------


def test_get_brackets_for_positions_empty_tuple_returns_empty() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_brackets_for_positions(position_ids=()))
    assert result == ()


def test_get_brackets_for_positions_matching_position_id() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_brackets_for_positions(position_ids=("pos-1",)))
    assert len(result) == 1
    assert result[0].position_id == "pos-1"


def test_get_brackets_for_positions_non_matching_position_id() -> None:
    stub = _make_stub_with_position("pos-1")
    result = asyncio.run(stub.get_brackets_for_positions(position_ids=("pos-other",)))
    assert result == ()
