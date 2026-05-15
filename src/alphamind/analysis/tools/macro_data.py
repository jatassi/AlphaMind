"""macro_data on-demand tool — ALP-259.

Queries MacroObservations for generic macro indicators (FRED, EIA, BLS) and
TreasuryAuctions for canonical treasury tenor aliases (e.g. treasury_2y).
Computes 1-day delta, 5-day delta, and trailing-252-day percentile rank for
each observation in the requested lookback window.

Unknown indicators return UNAVAILABLE rather than running an empty query.
The supported indicator set lives in _SUPPORTED_INDICATORS (fail-closed).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import MacroObservations, TreasuryAuctions

__all__ = [
    "MacroDataInput",
    "MacroDataOutput",
    "MacroDataPoint",
    "macro_data_factory",
]

# ---------------------------------------------------------------------------
# Supported indicator registry — fail-closed on unknown names
# ---------------------------------------------------------------------------

# Canonical alias map: user-facing indicator name -> (source, series_id) for MacroObservations
_MACRO_OBS_MAP: dict[str, tuple[str, str]] = {
    "vix": ("FRED", "VIXCLS"),
    "fed_funds_rate": ("FRED", "FEDFUNDS"),
    "cpi_yoy": ("FRED", "CPIAUCSL"),
    "unemployment_rate": ("FRED", "UNRATE"),
    "gdp_growth": ("FRED", "A191RL1Q225SBEA"),
    "credit_spread_hy": ("FRED", "BAMLH0A0HYM2"),
    "credit_spread_ig": ("FRED", "BAMLC0A0CM"),
    "tips_breakeven_5y": ("FRED", "T5YIE"),
    "tips_breakeven_10y": ("FRED", "T10YIE"),
    "ism_manufacturing": ("FRED", "MANEMP"),
    "eia_crude_inventory": ("EIA", "WTTSTUS1"),
    "nfp": ("FRED", "PAYEMS"),
}

# Treasury tenor alias map: user-facing name -> tenor string in TreasuryAuctions.tenor
_TREASURY_TENOR_MAP: dict[str, str] = {
    "treasury_2y": "2-Year",
    "treasury_5y": "5-Year",
    "treasury_10y": "10-Year",
    "treasury_30y": "30-Year",
    "treasury_3m": "3-Month",
    "treasury_6m": "6-Month",
}

# All supported indicators in one frozenset for O(1) membership test
_SUPPORTED_INDICATORS: frozenset[str] = frozenset(_MACRO_OBS_MAP) | frozenset(_TREASURY_TENOR_MAP)

# Trailing distribution window for percentile rank
_PERCENTILE_RANK_WINDOW_DAYS = 252  # ~1 trading year


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class MacroDataInput(BaseModel, frozen=True):
    """Input for the macro_data tool.

    ``indicator`` must be non-empty and in _SUPPORTED_INDICATORS;
    unknown indicators return UNAVAILABLE.
    """

    indicator: str
    lookback_days: int = 30


class MacroDataPoint(BaseModel, frozen=True):
    date: str  # ISO date of the observation
    value: float
    change_1d: float | None  # delta vs. prior observation
    change_5d: float | None  # delta vs. 5 observations prior
    percentile_1y: float | None  # rank within trailing 252-day distribution; in [0.0, 1.0]


class MacroDataOutput(ToolEnvelope, frozen=True):
    series: tuple[MacroDataPoint, ...]
    indicator: str


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _percentile_rank(value: float, distribution: list[float]) -> float:
    """Fraction of distribution values strictly less than value, in [0.0, 1.0].

    Uses a simple rank - computes how many observations are below `value`.
    """
    if not distribution:
        return 0.0
    below = sum(1 for v in distribution if v < value)
    return below / len(distribution)


def _build_series(
    dated_values: list[tuple[str, float]],
    full_year_values: list[float],
) -> tuple[MacroDataPoint, ...]:
    """Build MacroDataPoint tuple from sorted (date, value) pairs.

    ``dated_values`` is sorted oldest-first (within the lookback window).
    ``full_year_values`` is the full 252-day distribution for percentile rank.
    """
    eligible_for_percentile = len(full_year_values) >= _PERCENTILE_RANK_WINDOW_DAYS
    points: list[MacroDataPoint] = []
    for i, (date, value) in enumerate(dated_values):
        change_1d = value - dated_values[i - 1][1] if i >= 1 else None
        change_5d = value - dated_values[i - 5][1] if i >= 5 else None
        pctile = _percentile_rank(value, full_year_values) if eligible_for_percentile else None
        points.append(
            MacroDataPoint(
                date=date,
                value=value,
                change_1d=change_1d,
                change_5d=change_5d,
                percentile_1y=pctile,
            )
        )
    return tuple(points)


def _query_macro_obs(
    session: Session, source: str, series_id: str, lookback_days: int, now: datetime
) -> tuple[list[tuple[str, float]], list[float], str | None]:
    """Return (lookback_values_sorted_asc, full_year_values, most_recent_ingested_at).

    A single query fetches the full trailing-252-day window; the caller
    receives both the display slice (lookback_days) and the distribution
    (full year) without a second round-trip.
    """
    year_start = (now - timedelta(days=_PERCENTILE_RANK_WINDOW_DAYS)).strftime("%Y-%m-%d")
    lookback_start = (now - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    # Use the broader of the two windows as the query boundary
    window_start = min(year_start, lookback_start)

    rows = session.execute(
        select(
            MacroObservations.observation_date,
            MacroObservations.value,
            MacroObservations.ingested_at,
        )
        .where(
            MacroObservations.source == source,
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date >= window_start,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    ).all()

    if not rows:
        return [], [], None

    display_cutoff = (now - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    dated = [(r.observation_date, r.value) for r in rows if r.observation_date >= display_cutoff]
    full_year = [r.value for r in rows]
    ingested_at = max(r.ingested_at for r in rows)
    return dated, full_year, ingested_at


def _query_treasury(
    session: Session, tenor: str, lookback_days: int, now: datetime
) -> tuple[list[tuple[str, float]], list[float], str | None]:
    """Return (lookback_values_sorted_asc, full_year_values, most_recent_ingested_at).

    Single query covers the broader of lookback and full-year windows.
    """
    year_start = (now - timedelta(days=_PERCENTILE_RANK_WINDOW_DAYS)).strftime("%Y-%m-%d")
    lookback_start = (now - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    window_start = min(year_start, lookback_start)

    rows = session.execute(
        select(
            TreasuryAuctions.auction_date,
            TreasuryAuctions.auction_yield_bp,
            TreasuryAuctions.ingested_at,
        )
        .where(
            TreasuryAuctions.tenor == tenor,
            TreasuryAuctions.auction_date >= window_start,
            TreasuryAuctions.auction_yield_bp.isnot(None),
        )
        .order_by(TreasuryAuctions.auction_date)
    ).all()

    if not rows:
        return [], [], None

    display_cutoff = (now - timedelta(days=lookback_days + 10)).strftime("%Y-%m-%d")
    dated = [(r.auction_date, r.auction_yield_bp) for r in rows if r.auction_date >= display_cutoff]
    full_year = [r.auction_yield_bp for r in rows]
    ingested_at = max(r.ingested_at for r in rows)
    return dated, full_year, ingested_at


def _get_macro_data(session: Session, inp: MacroDataInput, clock: Clock) -> MacroDataOutput:
    now = clock.now()
    indicator = inp.indicator.strip().lower()

    if not indicator or indicator not in _SUPPORTED_INDICATORS:
        return MacroDataOutput(
            series=(),
            indicator=inp.indicator,
            data_freshness=now,
            quality=ToolQuality.UNAVAILABLE,
        )

    if indicator in _TREASURY_TENOR_MAP:
        tenor = _TREASURY_TENOR_MAP[indicator]
        dated, full_year, ingested_at = _query_treasury(session, tenor, inp.lookback_days, now)
    else:
        source, series_id = _MACRO_OBS_MAP[indicator]
        dated, full_year, ingested_at = _query_macro_obs(
            session, source, series_id, inp.lookback_days, now
        )

    if not dated:
        return MacroDataOutput(
            series=(),
            indicator=inp.indicator,
            data_freshness=now,
            quality=ToolQuality.UNAVAILABLE,
        )

    series = _build_series(dated, full_year)
    freshness = parse_iso(ingested_at) if ingested_at else now

    return MacroDataOutput(
        series=series,
        indicator=inp.indicator,
        data_freshness=freshness,
        quality=ToolQuality.COMPLETE,
    )


def macro_data_factory(
    session: Session,
    *,
    clock: Clock | None = None,
) -> Callable[[MacroDataInput], MacroDataOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry.

    ``clock`` defaults to :class:`RealClock`; tests pass a fake to control
    the timestamp deterministically (ALP-474).
    """
    resolved_clock: Clock = clock if clock is not None else RealClock()

    def _call(inp: MacroDataInput) -> MacroDataOutput:
        return _get_macro_data(session, inp, resolved_clock)

    return _call
