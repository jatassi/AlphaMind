"""Per-consumer view and projection function for the strategist agent (story 07)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.computations.activity_log import filter_by_event_type
from alphamind.portfolio_state.consumers.analyst import (
    AnalystAbandonedOpening,
    _project_abandoned_openings,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    CommandAbandonedDetail,
    EventSource,
    EventType,
    PMDecisionDetail,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import BracketRecord, OrderRecord
from alphamind.portfolio_state.records.positions import InstrumentType, resolve_ticker
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


BetweenInvocationClosureOrigin = Literal[
    "bracket_manager",
    "margin_monitor",
    "guardrail_layer",
    "engine_guardrail",
]


class BetweenInvocationClosure(BaseModel):
    """A position closure recorded by the continuous monitor between invocations.

    Surfaces both (a) story 04c's direct-broker-call closures (price-based
    invalidation + P/L-target firings) sourced ``BRACKET_MANAGER``,
    ``MARGIN_MONITOR``, or ``GUARDRAIL_LAYER``, and (b) story 04a's
    engine-envelope cascade closures (``source_provenance="engine_guardrail"``
    on the PM decision envelope).

    Per parent issue ALP-123 decision (I), the strategist's next invocation
    must see monitor-fired closures prominently — this typed projection on
    :class:`StrategistView` provides the single surface.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    ticker: str
    instrument_type: InstrumentType
    closed_at: datetime
    closing_order_id: str | None
    exit_method: PositionExitMethod
    origin: BetweenInvocationClosureOrigin
    rationale: str


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
    between_invocation_closures: tuple[BetweenInvocationClosure, ...] = ()


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


_MONITOR_DIRECT_SOURCES: frozenset[EventSource] = frozenset(
    {
        EventSource.BRACKET_MANAGER,
        EventSource.MARGIN_MONITOR,
        EventSource.GUARDRAIL_LAYER,
    }
)

_SOURCE_TO_ORIGIN: dict[EventSource, BetweenInvocationClosureOrigin] = {
    EventSource.BRACKET_MANAGER: "bracket_manager",
    EventSource.MARGIN_MONITOR: "margin_monitor",
    EventSource.GUARDRAIL_LAYER: "guardrail_layer",
}


def _collect_engine_guardrail_command_ids(
    snapshot: PortfolioStateSnapshot,
) -> frozenset[str]:
    """Collect resulting_command_ids from PM decisions with engine_guardrail provenance.

    Cascade closures (story 04a) flow through the engine-envelope submission
    path; the PM_DECISION row records the envelope's ``source_provenance`` as
    ``engine_guardrail`` and lists the resulting OMS command IDs. The eventual
    POSITION_CLOSED row carries that command ID as its ``order_id`` (the
    fill-driven close), letting us join the two via this set.
    """
    pm_entries = filter_by_event_type(snapshot.intra_invocation_changelog, EventType.PM_DECISION)
    command_ids: set[str] = set()
    for entry in pm_entries:
        detail = entry.detail
        if not isinstance(detail, PMDecisionDetail):
            continue
        provenance = detail.source_provenance_json.get("source_provenance")
        if provenance != "engine_guardrail":
            continue
        command_ids.update(detail.resulting_command_ids)
    return frozenset(command_ids)


def _ticker_and_instrument_for_position(
    snapshot: PortfolioStateSnapshot,
    position_id: str,
) -> tuple[str, InstrumentType] | None:
    """Resolve ticker + instrument_type for a position from the snapshot.

    Walks ``open_positions`` + ``pending_positions``; closed positions are not
    present on the snapshot (the close already removed them). The caller falls
    back to an empty-ticker sentinel when this returns ``None`` (the position
    was already torn down before the snapshot assembled — uncommon when the
    closure happened the same invocation).
    """
    for view in (*snapshot.open_positions, *snapshot.pending_positions):
        if view.position_id == position_id:
            details = view.details
            ticker = resolve_ticker(details) or ""
            return ticker, details.instrument_type
    return None


def _rationale_for_closure(
    entry: ActivityLogEntry,
    detail: PositionClosedDetail,
    origin: BetweenInvocationClosureOrigin,
) -> str:
    """Short human-readable summary of the closure for strategist scanning.

    Strategy-position closures emit a zeroed ``exit_price`` per
    ``bracket_stops.task._estimated_exit_price_for`` (Phase 1 reconciliation
    overwrites the persisted P/L with the actual fill once the order lands).
    Rendering ``$0.00`` is operator-confusing for a position that did fill;
    surface ``"—"`` instead so the strategist's view distinguishes
    "fill-time price unavailable" from "filled at zero".
    """
    method = detail.exit_method.value
    exit_price = detail.exit_price
    pnl = detail.realized_pnl_usd
    exit_price_str = f"${float(exit_price):.2f}" if exit_price > 0 else "—"
    parts = [f"{origin}: exit_method={method}", f"exit_price={exit_price_str}"]
    if pnl != 0:
        parts.append(f"realized P/L=${float(pnl):.2f}")
    if entry.order_id is not None:
        parts.append(f"order_id={entry.order_id}")
    return "; ".join(parts)


def _project_between_invocation_closures(
    snapshot: PortfolioStateSnapshot,
) -> tuple[BetweenInvocationClosure, ...]:
    """Filter POSITION_CLOSED events by monitor-direct sources + engine-guardrail provenance.

    Two inclusion paths:

    * **Direct-call closures (story 04c + sibling guardrail/margin firings):**
      ``event_source ∈ {BRACKET_MANAGER, MARGIN_MONITOR, GUARDRAIL_LAYER}``.
    * **Engine-envelope cascade closures (story 04a):** the POSITION_CLOSED
      row's ``order_id`` is in the set of ``resulting_command_ids`` of any
      PM_DECISION whose envelope carries ``source_provenance=engine_guardrail``.

    Results are returned in chronological order by ``closed_at``; ties broken
    by ``entry_id`` for determinism.
    """
    cascade_command_ids = _collect_engine_guardrail_command_ids(snapshot)
    closed_entries = filter_by_event_type(
        snapshot.intra_invocation_changelog, EventType.POSITION_CLOSED
    )
    closures: list[BetweenInvocationClosure] = []
    for entry in closed_entries:
        detail = entry.detail
        if not isinstance(detail, PositionClosedDetail):
            continue
        if entry.position_id is None:
            continue
        origin: BetweenInvocationClosureOrigin | None = None
        if entry.source in _MONITOR_DIRECT_SOURCES:
            origin = _SOURCE_TO_ORIGIN[entry.source]
        elif entry.order_id is not None and entry.order_id in cascade_command_ids:
            origin = "engine_guardrail"
        if origin is None:
            continue
        resolved = _ticker_and_instrument_for_position(snapshot, entry.position_id)
        if resolved is None:
            ticker = ""
            instrument_type = InstrumentType.EQUITY
        else:
            ticker, instrument_type = resolved
        closures.append(
            BetweenInvocationClosure(
                position_id=entry.position_id,
                ticker=ticker,
                instrument_type=instrument_type,
                closed_at=entry.timestamp,
                closing_order_id=entry.order_id,
                exit_method=detail.exit_method,
                origin=origin,
                rationale=_rationale_for_closure(entry, detail, origin),
            )
        )
    closures.sort(key=lambda c: (c.closed_at, c.position_id))
    return tuple(closures)


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
        between_invocation_closures=_project_between_invocation_closures(snapshot),
    )
