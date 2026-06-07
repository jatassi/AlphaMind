"""Master snapshot aggregate for portfolio state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alphamind._kernel.money import Money
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry, EventType
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord, OrderStatus
from alphamind.portfolio_state.records.positions import PositionStatus
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Snapshot-time rollup types (declared here, not in records/)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioPnL:
    """Snapshot-time P/L rollup (raw state category 2b).

    ALP-462 — USD aggregate fields carry :class:`Money` (Decimal-backed) so
    they round-trip through the cash-ledger / activity-log Decimal-aware
    accumulators without binary float drift. Percentage / ratio fields stay
    ``float`` because they are derived ratios, not money preservation
    quantities.
    """

    total_unrealized_pnl_usd: Money
    total_unrealized_pnl_pct_of_portfolio: float
    daily_realized_pnl_usd: Money
    daily_total_pnl_usd: Money
    cumulative_realized_pnl_usd: Money
    rolling_realized_pnl: dict[Literal["1d", "3d", "5d", "20d"], Money]
    win_rate_pct: float | None
    average_win_size_usd: Money | None
    average_loss_size_usd: Money | None
    profit_factor: float | None


@dataclass(frozen=True, slots=True)
class SectorExposureEntry:
    """Per-sector long/short rollup (raw state 1b sector allocation).

    ALP-462 — USD aggregates carry :class:`Money`; pct/ratio fields stay float.
    """

    sector: str
    long_delta_adjusted_usd: Money
    short_delta_adjusted_usd: Money
    long_pct_of_portfolio: float
    short_pct_of_portfolio: float
    long_short_ratio: float | None


@dataclass(frozen=True, slots=True)
class DirectionalExposure:
    """Portfolio-level directional rollup (raw state 1b net directional and gross exposure).

    ALP-462 — USD aggregates carry :class:`Money`; pct fields stay float.
    """

    total_long_delta_adjusted_usd: Money
    total_short_delta_adjusted_usd: Money
    net_directional_pct_of_portfolio: float
    gross_pct_of_portfolio: float

    def __post_init__(self) -> None:
        if self.gross_pct_of_portfolio < 0:
            msg = f"gross_pct_of_portfolio must be >= 0; got {self.gross_pct_of_portfolio}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Master snapshot
# ---------------------------------------------------------------------------

_PENDING_ORDER_STATUSES = frozenset({OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED})


@dataclass(frozen=True, slots=True)
class PortfolioStateSnapshot:
    """Frozen point-in-time aggregate of all raw portfolio state categories 1-6."""

    # Identity / scaffolding
    invocation_id: str
    fill_collection_committed_at: datetime
    snapshot_assembled_at: datetime

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

    pipeline_invocation_started_at: datetime | None = None

    # ------------------------------------------------------------------
    # Validators — coalesced into one __post_init__ calling private checks
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if len(self.invocation_id) < 1:
            msg = "invocation_id must be non-empty"
            raise ValueError(msg)
        self._check_timestamp_tz_awareness()
        self._check_timestamp_ordering()
        self._check_open_position_statuses()
        self._check_pending_position_statuses()
        self._check_pending_order_statuses()
        self._check_position_id_uniqueness()
        self._check_bracket_id_uniqueness()
        self._check_orphan_free_brackets()
        self._check_position_modification_trail()
        self._check_intra_invocation_changelog()
        self._check_pm_decision_log()

    def _check_timestamp_tz_awareness(self) -> None:
        for name, value in [
            ("fill_collection_committed_at", self.fill_collection_committed_at),
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

    def _check_timestamp_ordering(self) -> None:
        if self.fill_collection_committed_at > self.snapshot_assembled_at:
            msg = (
                "fill_collection_committed_at must be <= snapshot_assembled_at; "
                f"got {self.fill_collection_committed_at} > {self.snapshot_assembled_at}"
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

    def _check_open_position_statuses(self) -> None:
        bad = [p.position_id for p in self.open_positions if p.status != PositionStatus.OPEN]
        if bad:
            msg = f"open_positions contains positions with status != OPEN: {bad}"
            raise ValueError(msg)

    def _check_pending_position_statuses(self) -> None:
        bad = [p.position_id for p in self.pending_positions if p.status != PositionStatus.PENDING]
        if bad:
            msg = f"pending_positions contains positions with status != PENDING: {bad}"
            raise ValueError(msg)

    def _check_pending_order_statuses(self) -> None:
        bad = [o.order_id for o in self.pending_orders if o.status not in _PENDING_ORDER_STATUSES]
        if bad:
            msg = (
                f"pending_orders contains orders with status not in "
                f"{{PENDING, PARTIALLY_FILLED}}: {bad}"
            )
            raise ValueError(msg)

    def _check_position_id_uniqueness(self) -> None:
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

    def _check_bracket_id_uniqueness(self) -> None:
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

    def _check_orphan_free_brackets(self) -> None:
        orphans = [
            b.bracket_id for b in self.brackets if self.position_by_id(b.position_id) is None
        ]
        if orphans:
            msg = (
                "brackets reference position_ids not found in open_positions or "
                f"pending_positions: {orphans}"
            )
            raise ValueError(msg)

    def _check_position_modification_trail(self) -> None:
        unresolvable = [
            pid for pid in self.position_modification_trail if self.position_by_id(pid) is None
        ]
        if unresolvable:
            msg = (
                "position_modification_trail contains position_id keys not found in "
                f"open_positions or pending_positions: {unresolvable}"
            )
            raise ValueError(msg)

    def _check_intra_invocation_changelog(self) -> None:
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

    def _check_pm_decision_log(self) -> None:
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
