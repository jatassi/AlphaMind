"""Per-consumer view and projection function for the analyst agent (story 07)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alphamind.portfolio_state.computations.activity_log import filter_by_event_type
from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.consumers.synthesizer import (
    SynthesizerThesisSummary,
    _project_theses,
    _ticker_from_position,
)
from alphamind.portfolio_state.records.activity_log import (
    CommandAbandonedDetail,
    EventType,
)
from alphamind.portfolio_state.records.orders import OrderRecord
from alphamind.portfolio_state.records.positions import Direction, InstrumentType
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

_ANALYST_AGENT = "analyst"


@dataclass(frozen=True, slots=True)
class AnalystHeldPosition:
    """Thin per-position summary for the analyst (no P/L, no thesis content)."""

    position_id: str
    ticker: str
    direction: Direction
    sector: str
    size_pct: float
    instrument_type: InstrumentType


@dataclass(frozen=True, slots=True)
class AnalystAvailableCapital:
    """Capital availability summary for the analyst."""

    available_for_new_positions_usd: float
    available_for_new_positions_pct: float
    per_position_max_size_usd: float
    per_position_max_size_pct: float


@dataclass(frozen=True, slots=True)
class AnalystAbandonedOpening:
    """An analyst-originated abandoned opening command."""

    envelope_id: str
    direction: Direction
    ticker: str
    instrument_type: InstrumentType
    size_pct: float
    abandoned_at: datetime
    failure_reason: str


@dataclass(frozen=True, slots=True)
class AnalystView:
    """Full analyst projection bundling held positions, capital, orders, and theses."""

    held_positions: tuple[AnalystHeldPosition, ...]
    active_thesis_summaries: tuple[SynthesizerThesisSummary, ...]
    available_capital: AnalystAvailableCapital
    pending_orders: tuple[OrderRecord, ...]
    abandoned_openings: tuple[AnalystAbandonedOpening, ...]


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _project_held_positions(
    snapshot: PortfolioStateSnapshot,
    sector_resolver: SectorResolver,
) -> tuple[AnalystHeldPosition, ...]:
    # Combine open + pending so the analyst sees the same set as the strategist
    # (``_project_position_views``) and matches the rule-budget consumption that
    # drives Hard Blocks (ALP-549).
    result = []
    for pos in (*snapshot.open_positions, *snapshot.pending_positions):
        ticker = _ticker_from_position(pos)
        sector = sector_resolver(pos.record) or "UNCLASSIFIED"
        result.append(
            AnalystHeldPosition(
                position_id=pos.position_id,
                ticker=ticker,
                direction=pos.direction,
                sector=sector,
                size_pct=pos.position_weight_pct,
                instrument_type=pos.instrument_type,
            )
        )
    return tuple(result)


def _project_available_capital(
    snapshot: PortfolioStateSnapshot,
    per_position_size_rule_id: str,
    total_portfolio_value_usd: float,
) -> AnalystAvailableCapital:
    deployable = snapshot.cash_ledger.true_deployable_capital_usd
    available_pct = (
        (deployable / total_portfolio_value_usd * 100.0) if total_portfolio_value_usd > 0 else 0.0
    )
    rule_entry = next(
        (
            e
            for e in snapshot.active_risk_parameters.entries
            if e.rule_id == per_position_size_rule_id
        ),
        None,
    )
    # The rule (``position_max_size_pct``) carries unit="pct"; convert to USD
    # via the portfolio value so the analyst's cap matches the strategist/PM
    # renderer (ALP-549).
    per_position_pct = rule_entry.value if rule_entry is not None else 0.0
    per_position_usd = (
        (per_position_pct / 100.0 * total_portfolio_value_usd)
        if total_portfolio_value_usd > 0
        else 0.0
    )
    return AnalystAvailableCapital(
        available_for_new_positions_usd=deployable,
        available_for_new_positions_pct=available_pct,
        per_position_max_size_usd=per_position_usd,
        per_position_max_size_pct=per_position_pct,
    )


def _project_abandoned_openings(
    snapshot: PortfolioStateSnapshot,
) -> tuple[AnalystAbandonedOpening, ...]:
    # COMMAND_ABANDONED entries live in intra_invocation_changelog (event_group=PM_DECISION)
    abandoned = filter_by_event_type(
        snapshot.intra_invocation_changelog, EventType.COMMAND_ABANDONED
    )
    result = []
    for entry in abandoned:
        detail: CommandAbandonedDetail = entry.detail
        if detail.originating_agent != _ANALYST_AGENT:
            continue
        result.append(
            AnalystAbandonedOpening(
                envelope_id=detail.envelope_id,
                direction=Direction.LONG,
                ticker="",
                instrument_type=InstrumentType.EQUITY,
                size_pct=0.0,
                abandoned_at=entry.timestamp,
                failure_reason=detail.failure_reason,
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Projection function
# ---------------------------------------------------------------------------


def project_analyst_view(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: SectorResolver,
    per_position_size_rule_id: str,
    total_portfolio_value_usd: float,
) -> AnalystView:
    """Project a PortfolioStateSnapshot into the analyst's typed view."""
    return AnalystView(
        held_positions=_project_held_positions(snapshot, sector_resolver),
        active_thesis_summaries=_project_theses(snapshot),
        available_capital=_project_available_capital(
            snapshot, per_position_size_rule_id, total_portfolio_value_usd
        ),
        pending_orders=snapshot.pending_orders,
        abandoned_openings=_project_abandoned_openings(snapshot),
    )
