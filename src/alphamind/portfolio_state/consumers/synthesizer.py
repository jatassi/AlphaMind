"""Per-consumer view and reader protocol for the synthesizer agent (story 07)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from alphamind.portfolio_state.computations.exposure import SectorResolver
from alphamind.portfolio_state.records.positions import Direction, PositionRecord
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class SynthesizerPositionSummary(BaseModel):
    """Slim per-position summary for the synthesizer."""

    model_config = ConfigDict(frozen=True)

    ticker: str
    direction: Direction
    sector: str
    size_pct: float
    position_age_hours: float


class SynthesizerThesisSummary(BaseModel):
    """Slim thesis summary for the synthesizer."""

    model_config = ConfigDict(frozen=True)

    position_id: str
    ticker: str
    summary: str
    key_catalyst: str
    time_expectation_hours: str


class SynthesizerExposureSnapshot(BaseModel):
    """Portfolio exposure snapshot for the synthesizer."""

    model_config = ConfigDict(frozen=True)

    sector_exposure_pct: dict[str, float]
    net_directional_pct: float
    gross_exposure_pct: float


class SynthesizerView(BaseModel):
    """Full synthesizer projection bundling positions, theses, and exposure."""

    model_config = ConfigDict(frozen=True)

    positions: tuple[SynthesizerPositionSummary, ...]
    theses: tuple[SynthesizerThesisSummary, ...]
    exposure: SynthesizerExposureSnapshot


# ---------------------------------------------------------------------------
# Reader protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SynthesizerPortfolioStateReader(Protocol):
    """Three-tool surface the synthesizer reads from portfolio state."""

    async def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]: ...

    async def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]: ...

    async def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot: ...


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _ticker_from_position(pos: PositionRecord) -> str:
    if pos.equity_details is not None:
        return pos.equity_details.ticker
    if pos.options_details is not None:
        return pos.options_details.underlying_ticker
    if pos.strategy_details is not None and pos.strategy_details.legs:
        return pos.strategy_details.legs[0].options.underlying_ticker
    return ""


def _project_positions(
    snapshot: PortfolioStateSnapshot,
    sector_resolver: SectorResolver,
) -> tuple[SynthesizerPositionSummary, ...]:
    all_positions = (*snapshot.open_positions, *snapshot.pending_positions)
    result = []
    for pos in all_positions:
        ticker = _ticker_from_position(pos)
        sector = sector_resolver(pos) or "UNCLASSIFIED"
        result.append(
            SynthesizerPositionSummary(
                ticker=ticker,
                direction=pos.direction,
                sector=sector,
                size_pct=pos.position_weight_pct,
                position_age_hours=pos.position_age_hours,
            )
        )
    return tuple(result)


def _project_theses(
    snapshot: PortfolioStateSnapshot,
) -> tuple[SynthesizerThesisSummary, ...]:
    result = []
    for thesis in snapshot.active_theses:
        pos = snapshot.position_by_id(thesis.position_id)
        ticker = _ticker_from_position(pos) if pos is not None else ""
        result.append(
            SynthesizerThesisSummary(
                position_id=thesis.position_id,
                ticker=ticker,
                summary=thesis.summary,
                key_catalyst=thesis.key_catalyst,
                time_expectation_hours=thesis.time_expectation_hours,
            )
        )
    return tuple(result)


def _project_exposure(
    snapshot: PortfolioStateSnapshot,
) -> SynthesizerExposureSnapshot:
    sector_exposure_pct: dict[str, float] = {}
    for entry in snapshot.sector_exposure:
        net = entry.long_pct_of_portfolio - entry.short_pct_of_portfolio
        if net != 0.0:
            sector_exposure_pct[entry.sector] = net
    return SynthesizerExposureSnapshot(
        sector_exposure_pct=sector_exposure_pct,
        net_directional_pct=snapshot.directional_exposure.net_directional_pct_of_portfolio,
        gross_exposure_pct=snapshot.directional_exposure.gross_pct_of_portfolio,
    )


# ---------------------------------------------------------------------------
# Projection function
# ---------------------------------------------------------------------------


def project_synthesizer_view(
    snapshot: PortfolioStateSnapshot,
    *,
    sector_resolver: SectorResolver,
) -> SynthesizerView:
    """Project a PortfolioStateSnapshot into the synthesizer's typed view."""
    return SynthesizerView(
        positions=_project_positions(snapshot, sector_resolver),
        theses=_project_theses(snapshot),
        exposure=_project_exposure(snapshot),
    )


# ---------------------------------------------------------------------------
# Snapshot-backed adapter
# ---------------------------------------------------------------------------


class SnapshotBackedSynthesizerReader:
    """Adapts a PortfolioStateSnapshot to SynthesizerPortfolioStateReader."""

    def __init__(
        self,
        snapshot: PortfolioStateSnapshot,
        sector_resolver: SectorResolver,
    ) -> None:
        self._view = project_synthesizer_view(snapshot, sector_resolver=sector_resolver)

    async def get_positions_summary(self) -> tuple[SynthesizerPositionSummary, ...]:
        return self._view.positions

    async def get_active_theses_summary(self) -> tuple[SynthesizerThesisSummary, ...]:
        return self._view.theses

    async def get_exposure_snapshot(self) -> SynthesizerExposureSnapshot:
        return self._view.exposure
