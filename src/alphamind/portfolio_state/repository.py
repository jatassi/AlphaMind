"""PortfolioStateRepository protocol, stub, and invocation-scaffolding value objects."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from alphamind.execution.regt_margin_attribution.aggregates import RegTExcessAggregates
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord

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


def _check_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite; got {value}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Helper value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioPnLInputs:
    """OMS-aggregate inputs needed for portfolio-pnl rollup (category 2b)."""

    daily_realized_pnl_usd: float
    cumulative_realized_pnl_usd: float
    rolling_realized_pnl: dict[str, float]
    win_rate_pct: float | None
    average_win_size_usd: float | None
    average_loss_size_usd: float | None
    profit_factor: float | None

    def __post_init__(self) -> None:
        _check_finite(self.daily_realized_pnl_usd, "daily_realized_pnl_usd")
        _check_finite(self.cumulative_realized_pnl_usd, "cumulative_realized_pnl_usd")
        for key, val in self.rolling_realized_pnl.items():
            if not math.isfinite(val):
                msg = f"rolling_realized_pnl[{key!r}] must be a finite float, got {val!r}"
                raise ValueError(msg)
        if self.win_rate_pct is not None and not (0.0 <= self.win_rate_pct <= 100.0):
            msg = f"win_rate_pct must satisfy 0 <= value <= 100; got {self.win_rate_pct}"
            raise ValueError(msg)
        if self.average_win_size_usd is not None and self.average_win_size_usd < 0:
            msg = (
                f"average_win_size_usd must be >= 0 when not None; got {self.average_win_size_usd}"
            )
            raise ValueError(msg)
        if self.average_loss_size_usd is not None and self.average_loss_size_usd < 0:
            msg = (
                f"average_loss_size_usd must be >= 0 when not None; "
                f"got {self.average_loss_size_usd}"
            )
            raise ValueError(msg)
        if self.profit_factor is not None and self.profit_factor < 0:
            msg = f"profit_factor must be >= 0 when not None; got {self.profit_factor}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CurrentInvocationMetadata:
    """Identity fields for the currently running pipeline invocation."""

    invocation_id: str
    phase1_committed_at: datetime
    pipeline_invocation_started_at: datetime | None

    def __post_init__(self) -> None:
        if len(self.invocation_id) < 1:
            msg = "invocation_id must be non-empty"
            raise ValueError(msg)
        _require_tz_aware(self.phase1_committed_at, "phase1_committed_at")
        if self.pipeline_invocation_started_at is not None:
            _require_tz_aware(self.pipeline_invocation_started_at, "pipeline_invocation_started_at")


@dataclass(frozen=True, slots=True)
class PriorInvocationContext:
    """Prior-invocation state used to compute parameter_change_flag."""

    prior_invocation_id: str | None
    prior_active_risk_parameters: ActiveRiskParameterSet | None
    prior_phase1_committed_at: datetime | None

    def __post_init__(self) -> None:
        id_is_none = self.prior_invocation_id is None
        params_is_none = self.prior_active_risk_parameters is None
        if id_is_none != params_is_none:
            msg = (
                "prior_invocation_id and prior_active_risk_parameters must both be None "
                "or both be non-None"
            )
            raise ValueError(msg)
        if self.prior_phase1_committed_at is not None:
            _require_tz_aware(self.prior_phase1_committed_at, "prior_phase1_committed_at")


# ---------------------------------------------------------------------------
# PortfolioStateRepository Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PortfolioStateRepository(Protocol):
    """Read-only view of all OMS state categories needed for snapshot assembly.

    All methods are synchronous: SQLite is the persistence engine and the
    Protocol carries no real I/O concurrency. Per ALP-454 Pre-resolved
    decision (C), the async colouring was stripped; revisit if/when
    Postgres lands.
    """

    # Category 1 — Position inventory
    def get_open_positions(self) -> tuple[PositionRecord, ...]: ...

    def get_pending_positions(self) -> tuple[PositionRecord, ...]: ...

    # Category 2c — Drawdown
    def get_drawdown_state(self) -> DrawdownState: ...

    # Category 2 rollup helper
    def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs: ...

    # Category 3 — Thesis registry
    def get_active_theses(self) -> tuple[ThesisRecord, ...]: ...

    def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]: ...

    # Category 4 — Capital and capacity
    def get_cash_ledger(self) -> CashLedger: ...

    def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates: ...

    def get_pending_orders(self) -> tuple[OrderRecord, ...]: ...

    def get_active_risk_parameters(self) -> ActiveRiskParameterSet: ...

    # Category 5 — Activity log
    def get_intra_invocation_changelog(
        self, *, invocation_id: str
    ) -> tuple[ActivityLogEntry, ...]: ...

    def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]: ...

    def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]: ...

    # Category 6 — Thesis quality
    def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate: ...

    # Brackets
    def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]: ...

    # Invocation scaffolding
    def get_current_invocation_metadata(self) -> CurrentInvocationMetadata: ...

    def get_prior_invocation_context(self) -> PriorInvocationContext: ...


# ---------------------------------------------------------------------------
# RepositoryFixture — frozen value object carrying all protocol return values
# ---------------------------------------------------------------------------


def _default_regt_aggregates() -> RegTExcessAggregates:
    return RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )


@dataclass(frozen=True, slots=True)
class RepositoryFixture:
    """Frozen value object carrying every field the Protocol returns; used by the stub."""

    open_positions: tuple[PositionRecord, ...]
    pending_positions: tuple[PositionRecord, ...]
    drawdown_state: DrawdownState
    portfolio_pnl_inputs: PortfolioPnLInputs
    active_theses: tuple[ThesisRecord, ...]
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...]
    cash_ledger: CashLedger
    pending_orders: tuple[OrderRecord, ...]
    active_risk_parameters: ActiveRiskParameterSet
    intra_invocation_changelog: tuple[ActivityLogEntry, ...]
    recent_pm_decision_log: tuple[ActivityLogEntry, ...]
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]
    thesis_quality_aggregates: ThesisQualityAggregate
    brackets: tuple[BracketRecord, ...]
    current_invocation_metadata: CurrentInvocationMetadata
    prior_invocation_context: PriorInvocationContext
    regt_excess_aggregates: RegTExcessAggregates = field(default_factory=_default_regt_aggregates)


# ---------------------------------------------------------------------------
# StubPortfolioStateRepository — test double backed by RepositoryFixture
# ---------------------------------------------------------------------------


class StubPortfolioStateRepository:
    """Concrete test double satisfying PortfolioStateRepository via a RepositoryFixture."""

    def __init__(self, fixture: RepositoryFixture) -> None:
        self._fixture = fixture

    def get_open_positions(self) -> tuple[PositionRecord, ...]:
        return self._fixture.open_positions

    def get_pending_positions(self) -> tuple[PositionRecord, ...]:
        return self._fixture.pending_positions

    def get_drawdown_state(self) -> DrawdownState:
        return self._fixture.drawdown_state

    def get_portfolio_pnl_inputs(self) -> PortfolioPnLInputs:
        return self._fixture.portfolio_pnl_inputs

    def get_active_theses(self) -> tuple[ThesisRecord, ...]:
        return self._fixture.active_theses

    def get_recent_thesis_resolutions(
        self, *, lookback_trading_days: int
    ) -> tuple[RecentThesisResolution, ...]:
        del lookback_trading_days
        return self._fixture.recent_thesis_resolutions

    def get_cash_ledger(self) -> CashLedger:
        return self._fixture.cash_ledger

    def get_regt_excess_aggregates(self, now: datetime) -> RegTExcessAggregates:
        del now
        return self._fixture.regt_excess_aggregates

    def get_pending_orders(self) -> tuple[OrderRecord, ...]:
        return self._fixture.pending_orders

    def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        return self._fixture.active_risk_parameters

    def get_intra_invocation_changelog(self, *, invocation_id: str) -> tuple[ActivityLogEntry, ...]:
        del invocation_id
        return self._fixture.intra_invocation_changelog

    def get_recent_pm_decision_log(
        self, *, sliding_window_invocations: int
    ) -> tuple[ActivityLogEntry, ...]:
        del sliding_window_invocations
        return self._fixture.recent_pm_decision_log

    def get_position_modification_trail(
        self, *, position_ids: tuple[str, ...]
    ) -> dict[str, tuple[ActivityLogEntry, ...]]:
        trail = self._fixture.position_modification_trail
        return {pid: trail[pid] for pid in position_ids if pid in trail}

    def get_thesis_quality_aggregates(self) -> ThesisQualityAggregate:
        return self._fixture.thesis_quality_aggregates

    def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        return tuple(b for b in self._fixture.brackets if b.position_id in position_ids)

    def get_current_invocation_metadata(self) -> CurrentInvocationMetadata:
        return self._fixture.current_invocation_metadata

    def get_prior_invocation_context(self) -> PriorInvocationContext:
        return self._fixture.prior_invocation_context
