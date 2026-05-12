"""Master snapshot aggregate for portfolio state (story 04a)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.records.activity_log import ActivityLogEntry, EventType
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord, OrderStatus
from alphamind.portfolio_state.records.positions import PositionStatus
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.records.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Snapshot-time rollup types (declared here, not in records/)
# ---------------------------------------------------------------------------


class PortfolioPnL(BaseModel):
    """Snapshot-time P/L rollup (raw state category 2b)."""

    model_config = ConfigDict(frozen=True)

    total_unrealized_pnl_usd: float
    total_unrealized_pnl_pct_of_portfolio: float
    daily_realized_pnl_usd: float
    daily_total_pnl_usd: float
    cumulative_realized_pnl_usd: float
    rolling_realized_pnl: dict[Literal["1d", "3d", "5d", "20d"], float]
    win_rate_pct: float | None
    average_win_size_usd: float | None
    average_loss_size_usd: float | None
    profit_factor: float | None


class SectorExposureEntry(BaseModel):
    """Per-sector long/short rollup (raw state 1b sector allocation)."""

    model_config = ConfigDict(frozen=True)

    sector: str
    long_delta_adjusted_usd: float
    short_delta_adjusted_usd: float
    long_pct_of_portfolio: float
    short_pct_of_portfolio: float
    long_short_ratio: float | None


class DirectionalExposure(BaseModel):
    """Portfolio-level directional rollup (raw state 1b net directional and gross exposure)."""

    model_config = ConfigDict(frozen=True)

    total_long_delta_adjusted_usd: float
    total_short_delta_adjusted_usd: float
    net_directional_pct_of_portfolio: float
    gross_pct_of_portfolio: Annotated[float, Field(ge=0.0)]


# ---------------------------------------------------------------------------
# Master snapshot
# ---------------------------------------------------------------------------

_PENDING_ORDER_STATUSES = frozenset({OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED})


class PortfolioStateSnapshot(BaseModel):
    """Frozen point-in-time aggregate of all raw portfolio state categories 1-6."""

    model_config = ConfigDict(frozen=True)

    # Identity / scaffolding
    invocation_id: str = Field(min_length=1)
    phase1_committed_at: datetime
    snapshot_assembled_at: datetime
    pipeline_invocation_started_at: datetime | None = None

    # Category 1 — Position inventory
    open_positions: tuple[PositionView, ...]
    pending_positions: tuple[PositionView, ...]
    sector_exposure: tuple[SectorExposureEntry, ...]
    directional_exposure: DirectionalExposure

    # Category 2 — P/L and performance
    portfolio_pnl: PortfolioPnL
    drawdown: DrawdownState

    # Category 3 — Thesis registry
    active_theses: tuple[ThesisRecord, ...]
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...]

    # Category 4 — Capital and capacity
    cash_ledger: CashLedger
    pending_orders: tuple[OrderRecord, ...]
    risk_budget: RiskBudgetConsumption
    active_risk_parameters: ActiveRiskParameterSet

    # Category 5 — Activity log
    intra_invocation_changelog: tuple[ActivityLogEntry, ...]
    recent_pm_decision_log: tuple[ActivityLogEntry, ...]
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]

    # Category 6 — Thesis quality trends
    thesis_quality_aggregates: ThesisQualityAggregate

    # Cross-cutting brackets reference
    brackets: tuple[BracketRecord, ...]

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_timestamp_tz_awareness(self) -> PortfolioStateSnapshot:
        for name, value in [
            ("phase1_committed_at", self.phase1_committed_at),
            ("snapshot_assembled_at", self.snapshot_assembled_at),
        ]:
            if value.tzinfo is None or value.utcoffset() is None:
                msg = f"{name} must be a tz-aware UTC datetime"
                raise ValueError(msg)
        if self.pipeline_invocation_started_at is not None:
            v = self.pipeline_invocation_started_at
            if v.tzinfo is None or v.utcoffset() is None:
                msg = "pipeline_invocation_started_at must be tz-aware UTC when non-None"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_timestamp_ordering(self) -> PortfolioStateSnapshot:
        if self.phase1_committed_at > self.snapshot_assembled_at:
            msg = (
                "phase1_committed_at must be <= snapshot_assembled_at; "
                f"got {self.phase1_committed_at} > {self.snapshot_assembled_at}"
            )
            raise ValueError(msg)
        if (
            self.pipeline_invocation_started_at is not None
            and self.pipeline_invocation_started_at < self.snapshot_assembled_at
        ):
            msg = (
                "pipeline_invocation_started_at must be >= snapshot_assembled_at when non-None; "
                f"got {self.pipeline_invocation_started_at} < {self.snapshot_assembled_at}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_open_position_statuses(self) -> PortfolioStateSnapshot:
        bad = [p.position_id for p in self.open_positions if p.status != PositionStatus.OPEN]
        if bad:
            msg = f"open_positions contains positions with status != OPEN: {bad}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_pending_position_statuses(self) -> PortfolioStateSnapshot:
        bad = [p.position_id for p in self.pending_positions if p.status != PositionStatus.PENDING]
        if bad:
            msg = f"pending_positions contains positions with status != PENDING: {bad}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_pending_order_statuses(self) -> PortfolioStateSnapshot:
        bad = [o.order_id for o in self.pending_orders if o.status not in _PENDING_ORDER_STATUSES]
        if bad:
            msg = (
                f"pending_orders contains orders with status not in "
                f"{{PENDING, PARTIALLY_FILLED}}: {bad}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_position_id_uniqueness(self) -> PortfolioStateSnapshot:
        seen: set[str] = set()
        dupes: list[str] = []
        for pos in (*self.open_positions, *self.pending_positions):
            if pos.position_id in seen:
                dupes.append(pos.position_id)
            else:
                seen.add(pos.position_id)
        if dupes:
            msg = (
                "position_id values must be unique across open_positions and pending_positions; "
                f"duplicates: {dupes}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_bracket_id_uniqueness(self) -> PortfolioStateSnapshot:
        seen: set[str] = set()
        dupes: list[str] = []
        for brk in self.brackets:
            if brk.bracket_id in seen:
                dupes.append(brk.bracket_id)
            else:
                seen.add(brk.bracket_id)
        if dupes:
            msg = f"bracket_id values must be unique across brackets; duplicates: {dupes}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_orphan_free_brackets(self) -> PortfolioStateSnapshot:
        orphans = [
            b.bracket_id for b in self.brackets if self.position_by_id(b.position_id) is None
        ]
        if orphans:
            msg = (
                "brackets reference position_ids not found in open_positions or "
                f"pending_positions: {orphans}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_position_modification_trail(self) -> PortfolioStateSnapshot:
        unresolvable = [
            pid for pid in self.position_modification_trail if self.position_by_id(pid) is None
        ]
        if unresolvable:
            msg = (
                "position_modification_trail contains position_id keys not found in "
                f"open_positions or pending_positions: {unresolvable}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_intra_invocation_changelog(self) -> PortfolioStateSnapshot:
        bad = [
            entry.entry_id
            for entry in self.intra_invocation_changelog
            if entry.invocation_id != self.invocation_id
        ]
        if bad:
            msg = (
                f"intra_invocation_changelog entries must have invocation_id == "
                f"{self.invocation_id!r}; offending entry_ids: {bad}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_pm_decision_log(self) -> PortfolioStateSnapshot:
        bad = [
            entry.entry_id
            for entry in self.recent_pm_decision_log
            if entry.event_type != EventType.PM_DECISION
        ]
        if bad:
            msg = (
                f"recent_pm_decision_log entries must have event_type == PM_DECISION; "
                f"offending entry_ids: {bad}"
            )
            raise ValueError(msg)
        return self

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def position_by_id(self, position_id: str) -> PositionView | None:
        """Return the PositionView with the given position_id, searching open and pending."""
        for pos in self.open_positions:
            if pos.position_id == position_id:
                return pos
        for pos in self.pending_positions:
            if pos.position_id == position_id:
                return pos
        return None

    def bracket_for_position(self, position_id: str) -> BracketRecord | None:
        """Return the BracketRecord bound to position_id, or None."""
        for bracket in self.brackets:
            if bracket.position_id == position_id:
                return bracket
        return None

    def active_thesis_for_position(self, position_id: str) -> ThesisRecord | None:
        """Return the active ThesisRecord for position_id, or None."""
        for thesis in self.active_theses:
            if thesis.position_id == position_id:
                return thesis
        return None

    def pending_orders_for_position(self, position_id: str) -> tuple[OrderRecord, ...]:
        """Return all pending OrderRecords for position_id; empty tuple if none."""
        return tuple(o for o in self.pending_orders if o.position_id == position_id)
