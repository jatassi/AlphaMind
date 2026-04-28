"""Sector and directional exposure rollup computations (story 05c)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from alphamind.portfolio_state.records.positions import Direction, PositionRecord
from alphamind.portfolio_state.snapshot import DirectionalExposure, SectorExposureEntry

# ---------------------------------------------------------------------------
# Public type alias
# ---------------------------------------------------------------------------

SectorResolver = Callable[[PositionRecord], str | None]

_UNCLASSIFIED = "UNCLASSIFIED"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_total(total_portfolio_value_usd: float) -> None:
    if total_portfolio_value_usd < 0:
        msg = f"total_portfolio_value_usd must be >= 0; got {total_portfolio_value_usd}"
        raise ValueError(msg)


def _check_enriched(positions: tuple[PositionRecord, ...]) -> None:
    for pos in positions:
        if pos.delta_adjusted_exposure_usd is None:
            msg = (
                f"delta_adjusted_exposure_usd must be non-None; "
                f"position_id={pos.position_id!r} is unenriched"
            )
            raise ValueError(msg)


def _pct(value: float, total: float) -> float:
    return (value / total * 100) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def compute_sector_exposure(
    open_positions: tuple[PositionRecord, ...],
    resolver: SectorResolver,
    total_portfolio_value_usd: float,
) -> tuple[SectorExposureEntry, ...]:
    """Compute per-sector long/short delta-adjusted exposure rollup.

    Positions where resolver returns None are aggregated under "UNCLASSIFIED".
    Bucket assignment uses Direction (LONG -> long, SHORT -> abs into short).
    Returns entries sorted by sector label ascending.
    """
    _validate_total(total_portfolio_value_usd)
    _check_enriched(open_positions)

    long_by_sector: dict[str, float] = defaultdict(float)
    short_by_sector: dict[str, float] = defaultdict(float)

    for pos in open_positions:
        sector = resolver(pos) or _UNCLASSIFIED
        dae: float = pos.delta_adjusted_exposure_usd
        if pos.direction == Direction.LONG:
            long_by_sector[sector] += dae
        else:
            short_by_sector[sector] += abs(dae)

    all_sectors = sorted(set(long_by_sector) | set(short_by_sector))

    entries: list[SectorExposureEntry] = []
    for sector in all_sectors:
        long_usd = long_by_sector[sector]
        short_usd = short_by_sector[sector]
        ratio: float | None = long_usd / short_usd if short_usd > 0 and long_usd > 0 else None
        entries.append(
            SectorExposureEntry(
                sector=sector,
                long_delta_adjusted_usd=long_usd,
                short_delta_adjusted_usd=short_usd,
                long_pct_of_portfolio=_pct(long_usd, total_portfolio_value_usd),
                short_pct_of_portfolio=_pct(short_usd, total_portfolio_value_usd),
                long_short_ratio=ratio,
            )
        )

    return tuple(entries)


def compute_directional_exposure(
    open_positions: tuple[PositionRecord, ...],
    total_portfolio_value_usd: float,
) -> DirectionalExposure:
    """Compute portfolio-level directional and gross exposure.

    Bucket assignment uses sign of delta_adjusted_exposure_usd (not Direction).
    Positive values go into the long bucket; negative values (absolute) into short.
    """
    _validate_total(total_portfolio_value_usd)
    _check_enriched(open_positions)

    total_long = sum(
        pos.delta_adjusted_exposure_usd
        for pos in open_positions
        if pos.delta_adjusted_exposure_usd > 0
    )
    total_short = sum(
        -pos.delta_adjusted_exposure_usd
        for pos in open_positions
        if pos.delta_adjusted_exposure_usd < 0
    )

    return DirectionalExposure(
        total_long_delta_adjusted_usd=total_long,
        total_short_delta_adjusted_usd=total_short,
        net_directional_pct_of_portfolio=_pct(total_long - total_short, total_portfolio_value_usd),
        gross_pct_of_portfolio=_pct(total_long + total_short, total_portfolio_value_usd),
    )
