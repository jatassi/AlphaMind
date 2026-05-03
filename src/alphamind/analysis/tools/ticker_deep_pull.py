"""ticker_deep_pull on-demand tool — ALP-260.

Single-ticker, multi-category data aggregator. The adaptive researcher
invokes this tool when investigating an anomaly that needs more granular
data than routine ingestion delivers.

Supported categories: price_volume, short_data, earnings, macro_context.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import (
    AssetUniverse,
    BorrowCostDaily,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    MacroObservations,
    OhlcvBars,
    SectorClassification,
    ShortInterestSnapshot,
)

__all__ = [
    "EarningsPayload",
    "MacroContextPayload",
    "PriceVolumePayload",
    "ShortDataPayload",
    "TickerDeepPullCategory",
    "TickerDeepPullInput",
    "TickerDeepPullOutput",
    "ticker_deep_pull_factory",
]

# ---------------------------------------------------------------------------
# Named module constants — no magic numbers
# ---------------------------------------------------------------------------

_OHLCV_LOOKBACK_DAYS = 20
_TIMEFRAME_DAILY = "1d"

# Series IDs for treasury yields (FRED)
_FRED_2Y_SERIES = "DGS2"
_FRED_10Y_SERIES = "DGS10"

# Sector → canonical macro indicator mapping (v1 subset)
_SECTOR_MACRO_INDICATOR: dict[str, str] = {
    "energy": "DCOILWTICO",  # WTI crude (EIA via FRED)
    "real_estate": "MORTGAGE30US",  # 30yr fixed mortgage rate
}

# Quality rank — lower rank = worse quality. Used to find the minimum quality
# across populated categories: min(quality_rank[q] for q in per_category_qualities).
_QUALITY_RANK: dict[ToolQuality, int] = {
    ToolQuality.UNAVAILABLE: 0,
    ToolQuality.STALE: 1,
    ToolQuality.PARTIAL: 2,
    ToolQuality.COMPLETE: 3,
}

_RANK_TO_QUALITY: dict[int, ToolQuality] = {v: k for k, v in _QUALITY_RANK.items()}

# ---------------------------------------------------------------------------
# Category enum + supported set
# ---------------------------------------------------------------------------


class TickerDeepPullCategory(StrEnum):
    PRICE_VOLUME = "price_volume"
    SHORT_DATA = "short_data"
    EARNINGS = "earnings"
    MACRO_CONTEXT = "macro_context"


_SUPPORTED_CATEGORIES: frozenset[str] = frozenset(c.value for c in TickerDeepPullCategory)

# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


class TickerDeepPullInput(BaseModel, frozen=True):
    """Input for the ticker_deep_pull tool.

    `ticker` must be in AssetUniverse.
    `categories` must be a non-empty subset of TickerDeepPullCategory values; unknown
    values are accepted at the Pydantic layer but produce UNAVAILABLE rows downstream.
    """

    ticker: str = Field(min_length=1)
    categories: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Per-category payload models
# ---------------------------------------------------------------------------


class PriceVolumePayload(BaseModel, frozen=True):
    """Q1: recent price + volume snapshot.

    Pulls the last 20 trading days of ``OhlcvBars`` (timeframe=daily).
    """

    last_close: float | None
    last_volume: int | None
    pct_change_1d: float | None
    pct_change_5d: float | None
    pct_change_20d: float | None
    avg_volume_20d: int | None
    volume_vs_avg_ratio: float | None  # last_volume / avg_volume_20d


class ShortDataPayload(BaseModel, frozen=True):
    """Q4: short interest + borrow cost summary."""

    short_interest_pct: float | None  # most recent ShortInterestSnapshot
    borrow_rate_pct: float | None  # most recent BorrowCostDaily
    days_to_cover: float | None
    short_interest_change_30d_pct: float | None  # pp delta vs. 30 days prior


class EarningsPayload(BaseModel, frozen=True):
    """Q5: most-recent earnings event + revision summary."""

    most_recent_event_date: str | None  # ISO date of most recent earnings event
    eps_actual: float | None
    eps_consensus: float | None
    eps_surprise_pct: float | None
    revisions_30d_count: int | None  # count of EarningsEstimateRevisions in last 30d
    revision_direction: str | None  # "up" | "down" | "mixed" | "none"


class MacroContextPayload(BaseModel, frozen=True):
    """Q6: ticker-relevant macro snapshot.

    Returns 2y/10y treasury yields and the most recent sector-relevant macro
    indicator (from a ticker→sector mapping) when available; None otherwise.
    """

    treasury_2y_yield: float | None
    treasury_10y_yield: float | None
    yield_curve_2_10_spread: float | None
    sector_relevant_indicator: str | None  # e.g., "wti_spot" for energy tickers
    sector_relevant_value: float | None


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------


class TickerDeepPullOutput(ToolEnvelope, frozen=True):
    """Output envelope for ticker_deep_pull.

    Per-category payloads are Optional: None when the caller did not request the
    category, OR when the category was requested but data was unavailable for the ticker.
    The aggregate ``quality`` reflects the worst per-category result; aggregate
    ``data_freshness`` is the minimum freshness across populated categories.
    """

    ticker: str
    price_volume: PriceVolumePayload | None
    short_data: ShortDataPayload | None
    earnings: EarningsPayload | None
    macro_context: MacroContextPayload | None


# ---------------------------------------------------------------------------
# Helper: ticker universe check
# ---------------------------------------------------------------------------


def _ticker_in_universe(session: Session, ticker: str) -> bool:
    return (
        session.execute(
            select(AssetUniverse.ticker).where(AssetUniverse.ticker == ticker).limit(1)
        ).scalar()
        is not None
    )


# ---------------------------------------------------------------------------
# Per-category loaders
# ---------------------------------------------------------------------------


def _pct_change(earlier: float, later: float) -> float | None:
    if earlier == 0.0:
        return None
    return (later - earlier) / earlier * 100.0


def _load_price_volume(
    session: Session, ticker: str
) -> tuple[PriceVolumePayload | None, datetime | None, ToolQuality]:
    rows = session.execute(
        select(
            OhlcvBars.period_start,
            OhlcvBars.adj_close,
            OhlcvBars.adj_volume,
            OhlcvBars.ingested_at,
        )
        .where(OhlcvBars.ticker == ticker, OhlcvBars.timeframe == _TIMEFRAME_DAILY)
        .order_by(OhlcvBars.period_start.desc())
        .limit(_OHLCV_LOOKBACK_DAYS + 1)
    ).all()

    if not rows:
        return (
            PriceVolumePayload(
                last_close=None,
                last_volume=None,
                pct_change_1d=None,
                pct_change_5d=None,
                pct_change_20d=None,
                avg_volume_20d=None,
                volume_vs_avg_ratio=None,
            ),
            None,
            ToolQuality.PARTIAL,
        )

    # rows are ordered newest-first
    sorted_rows = list(reversed(rows))  # oldest-first for index arithmetic
    last = sorted_rows[-1]
    last_close: float = last.adj_close
    last_volume: int = last.adj_volume

    closes = [r.adj_close for r in sorted_rows]
    volumes = [r.adj_volume for r in sorted_rows]

    pct_1d = _pct_change(closes[-2], closes[-1]) if len(closes) >= 2 else None
    pct_5d = _pct_change(closes[-6], closes[-1]) if len(closes) >= 6 else None
    pct_20d = _pct_change(closes[-21], closes[-1]) if len(closes) >= 21 else None

    avg_vol = int(sum(volumes[-_OHLCV_LOOKBACK_DAYS:]) / len(volumes[-_OHLCV_LOOKBACK_DAYS:]))
    vol_ratio = last_volume / avg_vol if avg_vol > 0 else None

    freshness_raw = max((r.ingested_at for r in rows), default=None)
    freshness = parse_iso(freshness_raw) if freshness_raw else None

    payload = PriceVolumePayload(
        last_close=last_close,
        last_volume=last_volume,
        pct_change_1d=pct_1d,
        pct_change_5d=pct_5d,
        pct_change_20d=pct_20d,
        avg_volume_20d=avg_vol,
        volume_vs_avg_ratio=vol_ratio,
    )
    return payload, freshness, ToolQuality.COMPLETE


def _load_short_data(
    session: Session, ticker: str
) -> tuple[ShortDataPayload | None, datetime | None, ToolQuality]:
    recent_si = session.execute(
        select(
            ShortInterestSnapshot.settlement_date,
            ShortInterestSnapshot.current_short_shares,
            ShortInterestSnapshot.avg_daily_volume_shares,
            ShortInterestSnapshot.days_to_cover,
            ShortInterestSnapshot.ingested_at,
        )
        .where(ShortInterestSnapshot.ticker == ticker)
        .order_by(ShortInterestSnapshot.settlement_date.desc())
        .limit(1)
    ).first()

    recent_bc = session.execute(
        select(
            BorrowCostDaily.fee_pct,
            BorrowCostDaily.ingested_at,
        )
        .where(BorrowCostDaily.ticker == ticker)
        .order_by(BorrowCostDaily.observation_date.desc())
        .limit(1)
    ).first()

    if recent_si is None and recent_bc is None:
        return (
            ShortDataPayload(
                short_interest_pct=None,
                borrow_rate_pct=None,
                days_to_cover=None,
                short_interest_change_30d_pct=None,
            ),
            None,
            ToolQuality.PARTIAL,
        )

    # short_interest_pct: current_short_shares as % of avg_daily_volume_shares
    # (shares / avg_daily_vol * 100 gives a days-to-cover-adjacent pct proxy;
    # this is consistent with the Q4 schema which stores raw share counts, not a
    # pre-computed float-shares pct)
    si_pct: float | None = None
    days_to_cover: float | None = None
    if recent_si is not None:
        days_to_cover = recent_si.days_to_cover
        adv = recent_si.avg_daily_volume_shares
        if adv is not None and adv > 0:
            si_pct = recent_si.current_short_shares / adv * 100.0

    # 30d prior snapshot for delta
    window_ago = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d")
    older_si = session.execute(
        select(ShortInterestSnapshot.current_short_shares)
        .where(
            ShortInterestSnapshot.ticker == ticker,
            ShortInterestSnapshot.settlement_date <= window_ago,
        )
        .order_by(ShortInterestSnapshot.settlement_date.desc())
        .limit(1)
    ).scalar()

    change_30d: float | None = None
    if recent_si is not None and older_si is not None and older_si > 0:
        change_30d = (recent_si.current_short_shares - older_si) / older_si * 100.0

    borrow_rate = recent_bc.fee_pct if recent_bc is not None else None

    freshness_candidates: list[str] = []
    if recent_si is not None:
        freshness_candidates.append(recent_si.ingested_at)
    if recent_bc is not None:
        freshness_candidates.append(recent_bc.ingested_at)
    freshness = parse_iso(max(freshness_candidates)) if freshness_candidates else None

    payload = ShortDataPayload(
        short_interest_pct=si_pct,
        borrow_rate_pct=borrow_rate,
        days_to_cover=days_to_cover,
        short_interest_change_30d_pct=change_30d,
    )
    return payload, freshness, ToolQuality.COMPLETE


def _revision_direction(
    pairs: list[tuple[float | None, float | None]],
) -> str:
    ups = sum(1 for c, p in pairs if c is not None and p is not None and c > p)
    downs = sum(1 for c, p in pairs if c is not None and p is not None and c < p)
    if ups == 0 and downs == 0:
        return "none"
    if ups > 0 and downs == 0:
        return "up"
    if downs > 0 and ups == 0:
        return "down"
    return "mixed"


def _load_earnings(
    session: Session, ticker: str
) -> tuple[EarningsPayload | None, datetime | None, ToolQuality]:
    details_row = session.execute(
        select(
            EarningsEventDetails.reported_at,
            EarningsEventDetails.eps_actual,
            EarningsEventDetails.eps_consensus,
        )
        .where(
            EarningsEventDetails.ticker == ticker,
            EarningsEventDetails.reported_at.isnot(None),
        )
        .order_by(EarningsEventDetails.reported_at.desc())
        .limit(1)
    ).first()

    if details_row is None:
        return (
            EarningsPayload(
                most_recent_event_date=None,
                eps_actual=None,
                eps_consensus=None,
                eps_surprise_pct=None,
                revisions_30d_count=None,
                revision_direction=None,
            ),
            None,
            ToolQuality.PARTIAL,
        )

    reported_dt = parse_iso(details_row.reported_at)
    event_date_str = reported_dt.strftime("%Y-%m-%d")

    eps_actual = details_row.eps_actual
    eps_consensus = details_row.eps_consensus
    eps_surprise: float | None = None
    if eps_actual is not None and eps_consensus is not None and eps_consensus != 0.0:
        eps_surprise = (eps_actual - eps_consensus) / abs(eps_consensus) * 100.0

    window_start = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    revision_rows = session.execute(
        select(
            EarningsEstimateRevisions.consensus_value,
            EarningsEstimateRevisions.prior_consensus_value,
        ).where(
            EarningsEstimateRevisions.ticker == ticker,
            EarningsEstimateRevisions.revised_at >= window_start,
        )
    ).all()

    pairs = [(r.consensus_value, r.prior_consensus_value) for r in revision_rows]
    direction = _revision_direction(pairs) if pairs else "none"

    payload = EarningsPayload(
        most_recent_event_date=event_date_str,
        eps_actual=eps_actual,
        eps_consensus=eps_consensus,
        eps_surprise_pct=eps_surprise,
        revisions_30d_count=len(revision_rows),
        revision_direction=direction,
    )
    return payload, None, ToolQuality.COMPLETE


def _load_macro_context(
    session: Session, ticker: str
) -> tuple[MacroContextPayload | None, datetime | None, ToolQuality]:
    def _latest_value(series_id: str) -> tuple[float | None, str | None]:
        row = session.execute(
            select(MacroObservations.value, MacroObservations.ingested_at)
            .where(MacroObservations.series_id == series_id)
            .order_by(MacroObservations.observation_date.desc())
            .limit(1)
        ).first()
        if row is None:
            return None, None
        return row.value, row.ingested_at

    y2, y2_ingested = _latest_value(_FRED_2Y_SERIES)
    y10, y10_ingested = _latest_value(_FRED_10Y_SERIES)

    spread: float | None = None
    if y2 is not None and y10 is not None:
        spread = round(y2 - y10, 6)

    sector_indicator: str | None = None
    sector_value: float | None = None
    sector = _ticker_sector(session, ticker)
    if sector is not None:
        indicator_id = _SECTOR_MACRO_INDICATOR.get(sector)
        if indicator_id is not None:
            val, _ = _latest_value(indicator_id)
            if val is not None:
                sector_indicator = indicator_id
                sector_value = val

    freshness_candidates = [i for i in (y2_ingested, y10_ingested) if i is not None]
    freshness = parse_iso(max(freshness_candidates)) if freshness_candidates else None

    payload = MacroContextPayload(
        treasury_2y_yield=y2,
        treasury_10y_yield=y10,
        yield_curve_2_10_spread=spread,
        sector_relevant_indicator=sector_indicator,
        sector_relevant_value=sector_value,
    )

    quality = ToolQuality.COMPLETE if (y2 is not None or y10 is not None) else ToolQuality.PARTIAL
    return payload, freshness, quality


def _ticker_sector(session: Session, ticker: str) -> str | None:
    """Return the alphamind_sector for ticker, or None if unclassified."""
    return session.execute(
        select(SectorClassification.alphamind_sector)
        .where(SectorClassification.ticker == ticker)
        .limit(1)
    ).scalar()


# ---------------------------------------------------------------------------
# Quality aggregation helper
# ---------------------------------------------------------------------------


def _aggregate_quality(qualities: list[ToolQuality]) -> ToolQuality:
    """Return the worst quality across a list; UNAVAILABLE if the list is empty."""
    if not qualities:
        return ToolQuality.UNAVAILABLE
    min_rank = min(_QUALITY_RANK[q] for q in qualities)
    return _RANK_TO_QUALITY[min_rank]


# ---------------------------------------------------------------------------
# Unavailable envelope shortcut
# ---------------------------------------------------------------------------


def _unavailable_envelope(ticker: str, now: datetime) -> TickerDeepPullOutput:
    return TickerDeepPullOutput(
        ticker=ticker,
        data_freshness=now,
        quality=ToolQuality.UNAVAILABLE,
        price_volume=None,
        short_data=None,
        earnings=None,
        macro_context=None,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def ticker_deep_pull_factory(
    session: Session,
) -> Callable[[TickerDeepPullInput], TickerDeepPullOutput]:
    """Build the ticker_deep_pull callable bound to ``session``."""

    def _call(args: TickerDeepPullInput) -> TickerDeepPullOutput:
        now = datetime.now(UTC)
        ticker = args.ticker.upper()

        if not _ticker_in_universe(session, ticker):
            return _unavailable_envelope(ticker, now)

        if not args.categories:
            return _unavailable_envelope(ticker, now)

        return _dispatch_categories(session, ticker, args.categories, now)

    return _call


def _dispatch_categories(
    session: Session,
    ticker: str,
    categories: tuple[str, ...],
    now: datetime,
) -> TickerDeepPullOutput:
    """Fan out to per-category loaders and aggregate results."""
    price_volume: PriceVolumePayload | None = None
    short_data: ShortDataPayload | None = None
    earnings: EarningsPayload | None = None
    macro_context: MacroContextPayload | None = None
    qualities: list[ToolQuality] = []
    freshnesses: list[datetime] = []

    for category in categories:
        if category == TickerDeepPullCategory.PRICE_VOLUME:
            price_volume, freshness, quality = _load_price_volume(session, ticker)
        elif category == TickerDeepPullCategory.SHORT_DATA:
            short_data, freshness, quality = _load_short_data(session, ticker)
        elif category == TickerDeepPullCategory.EARNINGS:
            earnings, freshness, quality = _load_earnings(session, ticker)
        elif category == TickerDeepPullCategory.MACRO_CONTEXT:
            macro_context, freshness, quality = _load_macro_context(session, ticker)
        else:
            continue
        qualities.append(quality)
        if freshness is not None:
            freshnesses.append(freshness)

    return TickerDeepPullOutput(
        ticker=ticker,
        data_freshness=min(freshnesses) if freshnesses else now,
        quality=_aggregate_quality(qualities),
        price_volume=price_volume,
        short_data=short_data,
        earnings=earnings,
        macro_context=macro_context,
    )
