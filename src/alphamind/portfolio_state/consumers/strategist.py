"""Per-consumer view and projection function for the strategist agent (story 07)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from alphamind.portfolio_state.computations.activity_log import filter_by_event_type
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    _project_abandoned_openings,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    CommandAbandonedDetail,
    EventType,
)
from alphamind.portfolio_state.records.capital import (
    ActiveRiskParameterSet,
    DrawdownState,
    RiskBudgetConsumption,
)
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisRecord
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

_STRATEGIST_AGENT = "strategist"


class StrategistPositionView(BaseModel):
    """Per-position bundle for the strategist — position + thesis + bracket + orders + trail."""

    model_config = ConfigDict(frozen=True)

    position: PositionView
    thesis: ThesisRecord | None
    bracket: BracketRecord | None
    pending_orders: tuple[OrderRecord, ...]
    modification_trail: tuple[ActivityLogEntry, ...]


class StrategistAbandonedAction(BaseModel):
    """An abandoned command from the strategist."""

    model_config = ConfigDict(frozen=True)

    envelope_id: str
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
    position_id: str | None
    order_id: str | None
    abandoned_at: datetime
    failure_reason: str


class StrategistView(BaseModel):
    """Full strategist projection — all positions bundled, P/L, drawdown, activity logs."""

    model_config = ConfigDict(frozen=True)

    positions: tuple[StrategistPositionView, ...]
    recent_thesis_resolutions: tuple[RecentThesisResolution, ...]
    portfolio_pnl: PortfolioPnL
    drawdown: DrawdownState
    sector_exposure: tuple[SectorExposureEntry, ...]
    directional_exposure: DirectionalExposure
    risk_budget: RiskBudgetConsumption
    active_risk_parameters: ActiveRiskParameterSet
    intra_invocation_changelog: tuple[ActivityLogEntry, ...]
    recent_pm_decision_log: tuple[ActivityLogEntry, ...]
    abandoned_openings: tuple[AnalystAbandonedOpening, ...]
    abandoned_actions: tuple[StrategistAbandonedAction, ...]


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _project_position_views(
    snapshot: PortfolioStateSnapshot,
) -> tuple[StrategistPositionView, ...]:
    all_positions = sorted(
        (*snapshot.open_positions, *snapshot.pending_positions),
        key=lambda p: p.position_id,
    )
    result = []
    for pos in all_positions:
        thesis = snapshot.active_thesis_for_position(pos.position_id)
        bracket = snapshot.bracket_for_position(pos.position_id)
        orders = snapshot.pending_orders_for_position(pos.position_id)
        trail = snapshot.position_modification_trail.get(pos.position_id, ())
        result.append(
            StrategistPositionView(
                position=pos,
                thesis=thesis,
                bracket=bracket,
                pending_orders=orders,
                modification_trail=trail,
            )
        )
    return tuple(result)


def _project_abandoned_actions(
    snapshot: PortfolioStateSnapshot,
) -> tuple[StrategistAbandonedAction, ...]:
    abandoned = filter_by_event_type(
        snapshot.intra_invocation_changelog, EventType.COMMAND_ABANDONED
    )
    result = []
    for entry in abandoned:
        detail: CommandAbandonedDetail = entry.detail
        if detail.originating_agent != _STRATEGIST_AGENT:
            continue
        result.append(
            StrategistAbandonedAction(
                envelope_id=detail.envelope_id,
                command_type=detail.command_type,
                position_id=entry.position_id,
                order_id=entry.order_id,
                abandoned_at=entry.timestamp,
                failure_reason=detail.failure_reason,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Projection function
# ---------------------------------------------------------------------------


def project_strategist_view(snapshot: PortfolioStateSnapshot) -> StrategistView:
    """Project a PortfolioStateSnapshot into the strategist's typed view."""
    return StrategistView(
        positions=_project_position_views(snapshot),
        recent_thesis_resolutions=snapshot.recent_thesis_resolutions,
        portfolio_pnl=snapshot.portfolio_pnl,
        drawdown=snapshot.drawdown,
        sector_exposure=snapshot.sector_exposure,
        directional_exposure=snapshot.directional_exposure,
        risk_budget=snapshot.risk_budget,
        active_risk_parameters=snapshot.active_risk_parameters,
        intra_invocation_changelog=snapshot.intra_invocation_changelog,
        recent_pm_decision_log=snapshot.recent_pm_decision_log,
        abandoned_openings=_project_abandoned_openings(snapshot),
        abandoned_actions=_project_abandoned_actions(snapshot),
    )
