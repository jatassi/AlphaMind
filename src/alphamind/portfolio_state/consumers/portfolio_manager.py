"""Per-consumer view, reader protocol, and projection function for the PM agent (story 07)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.aggregates.thesis_quality import ThesisQualityAggregate
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    _project_abandoned_openings,
)
from alphamind.portfolio_state.consumers.strategist import (
    StrategistAbandonedAction,
    StrategistPositionView,
    _project_abandoned_actions,
    _project_position_views,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.theses import RecentThesisResolution, ThesisComponent
from alphamind.portfolio_state.snapshot import (
    DirectionalExposure,
    PortfolioPnL,
    PortfolioStateSnapshot,
    SectorExposureEntry,
)

# ---------------------------------------------------------------------------
# Reader protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PortfolioManagerThesisComponentReader(Protocol):
    """On-demand retrieval handle for thesis components (PM tool per portfolio-manager.md)."""

    async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]: ...


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioManagerView:
    """Full PM projection — all strategist fields plus thesis quality + trail dict."""

    # Strategist fields (re-declared; no inheritance per story notes)
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

    # PM-only fields
    thesis_quality_aggregates: ThesisQualityAggregate
    position_modification_trail: dict[str, tuple[ActivityLogEntry, ...]]


# ---------------------------------------------------------------------------
# Projection function
# ---------------------------------------------------------------------------


def project_portfolio_manager_view(snapshot: PortfolioStateSnapshot) -> PortfolioManagerView:
    """Project a PortfolioStateSnapshot into the portfolio manager's typed view."""
    return PortfolioManagerView(
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
        thesis_quality_aggregates=snapshot.thesis_quality_aggregates,
        position_modification_trail=snapshot.position_modification_trail,
    )


# ---------------------------------------------------------------------------
# Snapshot-backed adapter
# ---------------------------------------------------------------------------


class SnapshotBackedThesisComponentReader:
    """Adapts a PortfolioStateSnapshot to PortfolioManagerThesisComponentReader."""

    def __init__(self, snapshot: PortfolioStateSnapshot) -> None:
        self._snapshot = snapshot

    async def get_thesis_components(self, position_id: str) -> tuple[ThesisComponent, ...]:
        thesis = self._snapshot.active_thesis_for_position(position_id)
        if thesis is None:
            return ()
        return thesis.components
