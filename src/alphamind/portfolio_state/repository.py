"""PortfolioStateRepository protocol, stub, and invocation-scaffolding value objects (story 04b)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.portfolio_state.records.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    CashLedger,
    DrawdownState,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate

# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class RepositoryReadError(RuntimeError):
    """Base exception for any infrastructure failure during a repository read."""


class RepositoryConsistencyError(RepositoryReadError):
    """Raised when the production reader detects a Phase 1 / Phase 2 isolation violation."""


# ---------------------------------------------------------------------------
# Helper — tz-aware datetime validator
# ---------------------------------------------------------------------------


def _require_tz_aware(v: datetime, field_name: str) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        msg = f"{field_name} must be timezone-aware"
        raise ValueError(msg)
    return v


# ---------------------------------------------------------------------------
# Helper value objects
# ---------------------------------------------------------------------------


class PortfolioPnLInputs(BaseModel):
    """OMS-aggregate inputs needed for portfolio-pnl rollup (category 2b)."""

    model_config = {"frozen": True}

    daily_realized_pnl_usd: Annotated[float, Field(allow_inf_nan=False)]
    cumulative_realized_pnl_usd: Annotated[float, Field(allow_inf_nan=False)]
    rolling_realized_pnl: dict[str, float]
    win_rate_pct: Annotated[float, Field(ge=0.0, le=100.0)] | None
    average_win_size_usd: Annotated[float, Field(ge=0.0)] | None
    average_loss_size_usd: Annotated[float, Field(ge=0.0)] | None
    profit_factor: Annotated[float, Field(ge=0.0)] | None

    @field_validator("rolling_realized_pnl")
    @classmethod
    def _validate_rolling_finite(cls, v: dict[str, float]) -> dict[str, float]:
        for key, val in v.items():
            if not math.isfinite(val):
                msg = f"rolling_realized_pnl[{key!r}] must be a finite float, got {val!r}"
                raise ValueError(msg)
        return v


class CurrentInvocationMetadata(BaseModel):
    """Identity fields for the currently running pipeline invocation."""

    model_config = {"frozen": True}

    invocation_id: str = Field(min_length=1)
    phase1_committed_at: datetime
    pipeline_invocation_started_at: datetime | None

    @field_validator("phase1_committed_at")
    @classmethod
    def _phase1_tz_aware(cls, v: datetime) -> datetime:
        return _require_tz_aware(v, "phase1_committed_at")

    @field_validator("pipeline_invocation_started_at")
    @classmethod
    def _started_at_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _require_tz_aware(v, "pipeline_invocation_started_at")


class PriorInvocationContext(BaseModel):
    """Prior-invocation state used to compute parameter_change_flag."""

    model_config = {"frozen": True}

    prior_invocation_id: str | None
    prior_active_risk_parameters: ActiveRiskParameterSet | None
    prior_phase1_committed_at: datetime | None

    @model_validator(mode="after")
    def _co_null_invariant(self) -> PriorInvocationContext:
        id_is_none = self.prior_invocation_id is None
        params_is_none = self.prior_active_risk_parameters is None
        if id_is_none != params_is_none:
            msg = (
                "prior_invocation_id and prior_active_risk_parameters must both be None "
                "or both be non-None"
            )
            raise ValueError(msg)
        return self

    @field_validator("prior_phase1_committed_at")
    @classmethod
    def _prior_committed_at_tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return _require_tz_aware(v, "prior_phase1_committed_at")


# ---------------------------------------------------------------------------
# PortfolioStateRepository Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PortfolioStateRepository(Protocol):
    """Read-only view of all OMS state categories needed for snapshot assembly."""

    # Category 1 — Position inventory
    async def get_open_positions(self) -> tuple[PositionRecord, ...]: ...

    async def get_pending_positions(self) -> tuple[PositionRecord, ...]: ...

    # Category 2c — Drawdown
    async def get_drawdown_state(self) -> DrawdownState: ...

    # Category 2 rollup helper
    async def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs: ...

    # Category 3 — Thesis registry
    async def get_active_theses(self) -> tuple[ThesisRecord, ...]: ...

    async def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]: ...

    # Category 4 — Capital and capacity
    async def get_cash_ledger(self) -> CashLedger: ...

    async def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates: ...

    async def get_pending_orders(self) -> tuple[OrderRecord, ...]: ...

    async def get_risk_budget_consumption(self) -> RiskBudgetConsumption: ...

    async def get_active_risk_parameters(self) -> ActiveRiskParameterSet: ...

    # Category 5 — Activity log
    async def get_intra_invocation_changelog(
        self, *, invocation_id: str
    ) -> tuple[ActivityLogEntry, ...]: ...

    async def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]: ...

    async def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]: ...

    # Category 6 — Thesis quality
    async def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate: ...

    # Brackets
    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]: ...

    # Invocation scaffolding
    async def get_current_invocation_metadata(self) -> CurrentInvocationMetadata: ...

    async def get_prior_invocation_context(self) -> PriorInvocationContext: ...


# ---------------------------------------------------------------------------
# RepositoryFixture — frozen value object carrying all protocol return values
# ---------------------------------------------------------------------------


class RepositoryFixture(BaseModel):
    """Frozen value object carrying every field the Protocol returns; used by the stub."""

    model_config = {"frozen": True, "strict": True}

    open_positions: tuple[PositionRecord, ...]
    pending_positions: tuple[PositionRecord, ...]
    drawdown_state: DrawdownState
    portfolio_pnl_inputs: PortfolioPnLInputs
    active_theses: tuple[ThesisRecord, ...]
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...]
    cash_ledger: CashLedger
    regt_excess_aggregates: RegTExcessAggregates = RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )
    pending_orders: tuple[OrderRecord, ...]
    risk_budget: RiskBudgetConsumption
    active_risk_parameters: ActiveRiskParameterSet
    intra_invocation_changelog: tuple[ActivityLogEntry, ...]
    recent_pm_decision_log: tuple[ActivityLogEntry, ...]
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]
    thesis_quality_aggregates: ThesisQualityAggregate
    brackets: tuple[BracketRecord, ...]
    current_invocation_metadata: CurrentInvocationMetadata
    prior_invocation_context: PriorInvocationContext


# ---------------------------------------------------------------------------
# StubPortfolioStateRepository — test double backed by RepositoryFixture
# ---------------------------------------------------------------------------


class StubPortfolioStateRepository:
    """Concrete test double satisfying PortfolioStateRepository via a RepositoryFixture."""

    def __init__(self, fixture: RepositoryFixture) -> None:
        self._fixture = fixture

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        return self._fixture.open_positions

    async def get_pending_positions(self) -> tuple[PositionRecord, ...]:
        return self._fixture.pending_positions

    async def get_drawdown_state(self) -> DrawdownState:
        return self._fixture.drawdown_state

    async def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs:
        return self._fixture.portfolio_pnl_inputs

    async def get_active_theses(self) -> tuple[ThesisRecord, ...]:
        return self._fixture.active_theses

    async def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]:
        del lookback_trading_days
        return self._fixture.recent_thesis_resolutions

    async def get_cash_ledger(self) -> CashLedger:
        return self._fixture.cash_ledger

    async def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates:
        del now
        return self._fixture.regt_excess_aggregates

    async def get_pending_orders(self) -> tuple[OrderRecord, ...]:
        return self._fixture.pending_orders

    async def get_risk_budget_consumption(self) -> RiskBudgetConsumption:
        return self._fixture.risk_budget

    async def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        return self._fixture.active_risk_parameters

    async def get_intra_invocation_changelog(
        self, *, invocation_id: str
    ) -> tuple[ActivityLogEntry, ...]:
        del invocation_id
        return self._fixture.intra_invocation_changelog

    async def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]:
        del sliding_window_invocations
        return self._fixture.recent_pm_decision_log

    async def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]:
        trail = self._fixture.position_modification_trail
        return {pid: trail[pid] for pid in position_ids if pid in trail}

    async def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate:
        return self._fixture.thesis_quality_aggregates

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        return tuple(b for b in self._fixture.brackets if b.position_id in position_ids)

    async def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        return self._fixture.current_invocation_metadata

    async def get_prior_invocation_context(self) -> PriorInvocationContext:
        return self._fixture.prior_invocation_context
