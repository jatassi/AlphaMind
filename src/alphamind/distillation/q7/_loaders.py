"""Q7 IO shell (ALP-486): pre-load every input the q7 compute path consumes.

The compute path is :func:`assemble_q7_blocks_from_inputs` — a pure
function over :class:`Q7Inputs` (frozen). This module is the only place
Q7 reaches the database for the Phase 2 dispatch: it composes the
session-bound reads for each sub-module's pre-loaded inputs, runs the
pure compute, and performs the intra-sector divergence-event writes
synchronously under the shared session.

After this loader returns, :func:`assemble_q7_blocks_from_inputs` consumes
:class:`Q7Inputs` without further DB access — the parallel pure compute
sees already-computed correlation matrices and narrative-lag verdicts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.data_sources.news.types import HeadlineType
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._helpers import (
    _format_iso_utc,
    _log_returns_from_closes,
    _window_bounds,
)
from alphamind.distillation.q7.breadth_internals_compute import (
    EMA_WINDOWS_DAYS,
    compute_breadth_internals_pure,
)
from alphamind.distillation.q7.correlation_regime_change_compute import (
    CorrelationRegimeChangeParameters,
    compute_correlation_regime_change_pure,
)
from alphamind.distillation.q7.cross_sector_rotation_compute import (
    compute_cross_sector_rotation_pure,
)
from alphamind.distillation.q7.intermarket_regime_compute import (
    GLD_TICKER,
    OIL_SERIES,
    OIL_SOURCE,
    REAL_YIELD_SERIES,
    REAL_YIELD_SOURCE,
    SPY_TICKER,
    TLT_TICKER,
    VIX_SERIES,
    VIX_SOURCE,
    XLE_TICKER,
    compute_intermarket_regime_pure,
)
from alphamind.distillation.q7.intra_sector_correlation_compute import (
    compute_intra_sector_correlation_pure,
)
from alphamind.distillation.q7.lead_lag_compute import (
    LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
    LeadLagPair,
    LeadLagPairInputs,
    compute_lead_lag_pure,
)
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationEventHistory,
    DistillationPairLag,
    MacroObservations,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    SectorClassification,
)

# The four cross-sector ETFs the cross-sector rotation block reads.
_CROSS_SECTOR_ETFS: tuple[str, ...] = ("XLK", "SMH", "XLF", "XLE")
"""ETF tickers driving the cross-sector rotation block."""

_RISK_PROXY_ETFS: tuple[str, ...] = ("IWM", "SPY")
"""Risk-appetite proxies (small-cap vs. broad market)."""

# Topic-tag set defining "regime-relevant" media coverage. The narrative-lag
# indicator is gated on news articles whose ``topic_tags`` overlap with this
# set so routine company news doesn't drown out the silence signal.
NARRATIVE_LAG_REGIME_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.MACRO_DATA,
        HeadlineType.REGULATORY,
        HeadlineType.GEOPOLITICAL,
        HeadlineType.SECTOR_ROTATION,
    }
)


# Re-export for back-compat with the legacy ``q7.lead_lag`` typed inputs.
LeadLagInputs = LeadLagPairInputs


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Q7Inputs:
    """Frozen pre-loaded q7 blocks ready for the parallel compute path.

    Each field carries the already-assembled :class:`OutputBlock` instances
    for one sub-module. The loader does every session-bound read and the
    intra-sector divergence-event writes; the pure compute
    (:func:`assemble_q7_blocks_from_inputs`) just concatenates the blocks.

    ``pair_correlations`` is the orchestrator-pre-computed dict (shared
    with Q3); it threads through here for API symmetry with the Q3 loader
    and is currently unused by q7's own block emissions — q7's intra-sector
    matrices are computed independently from the per-sector return series.
    """

    as_of: datetime
    ticker_scope: tuple[str, ...]
    intra_sector_blocks: tuple[OutputBlock, ...]
    cross_sector_blocks: tuple[OutputBlock, ...]
    breadth_blocks: tuple[OutputBlock, ...]
    intermarket_blocks: tuple[OutputBlock, ...]
    lead_lag_blocks: tuple[OutputBlock, ...]
    correlation_regime_change_blocks: tuple[OutputBlock, ...]
    pair_correlations: Mapping[tuple[str, str], float] | None


# ---------------------------------------------------------------------------
# Session-bound read helpers
# ---------------------------------------------------------------------------


def _select_close_series(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> list[float]:
    """Return ascending daily-bar adj_close values for ``ticker`` in the window."""
    stmt = (
        select(OhlcvBars.adj_close)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    return [float(v) for v in session.execute(stmt).scalars().all()]


def _select_macro_series(
    session: Session,
    *,
    source: str,
    series_id: str,
    range_start_date: str,
    range_end_date: str,
) -> list[float]:
    """Return ascending non-null macro values in a date window."""
    stmt = (
        select(MacroObservations.value)
        .where(
            MacroObservations.source == source,
            MacroObservations.series_id == series_id,
            MacroObservations.observation_date >= range_start_date,
            MacroObservations.observation_date <= range_end_date,
            MacroObservations.value.isnot(None),
        )
        .order_by(MacroObservations.observation_date)
    )
    return [float(v) for v in session.execute(stmt).scalars().all() if v is not None]


def _resolve_universe_tickers(session: Session) -> list[str]:
    """Return every ticker in ``asset_universe`` ascending."""
    stmt = select(AssetUniverse.ticker).order_by(AssetUniverse.ticker)
    return [str(t) for t in session.execute(stmt).scalars().all()]


def _resolve_sector_roster(
    session: Session,
    *,
    ticker_scope: Sequence[str],
) -> dict[str, list[str]]:
    """Return ``{alphamind_sector: [tickers...]}`` restricted to ``ticker_scope``."""
    if not ticker_scope:
        return {}
    stmt = (
        select(SectorClassification.ticker, SectorClassification.alphamind_sector)
        .where(SectorClassification.ticker.in_(list(ticker_scope)))
        .order_by(SectorClassification.alphamind_sector, SectorClassification.ticker)
    )
    out: dict[str, list[str]] = {}
    for ticker, sector in session.execute(stmt).all():
        out.setdefault(str(sector), []).append(str(ticker))
    return out


def _persist_correlation_divergence_events(
    session: Session,
    *,
    block: OutputBlock,
    as_of: datetime,
) -> None:
    """Persist one ``correlation_divergence`` event row per pair-divergence flag.

    The event row's ``ticker`` carries the lead leg of the pair and
    ``direction`` carries the lag leg, so the (lead, kind, ts) primary key
    remains unique. ``magnitude_atr_multiple`` carries the divergence
    z-score (re-using the column for visibility — the column is generic
    enough that the q7 reader treats it as the divergence magnitude).

    Insertion is idempotent: rows already present at the same
    ``(ticker, event_kind, event_ts)`` triple are not duplicated.
    """
    as_of_iso = _format_iso_utc(as_of)
    for flag in block.anomaly_flags:
        # Flag name shape: ``intra_sector_correlation_divergence:<lead>:<lag>``.
        try:
            _prefix, lead, lag = flag.name.split(":")
        except ValueError:
            continue
        existing = session.execute(
            select(DistillationEventHistory).where(
                DistillationEventHistory.ticker == lead,
                DistillationEventHistory.event_kind == "correlation_divergence",
                DistillationEventHistory.event_ts == as_of_iso,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            DistillationEventHistory(
                ticker=lead,
                event_kind="correlation_divergence",
                event_ts=as_of_iso,
                direction=lag,
                magnitude_atr_multiple=float(flag.magnitude),
                outcome="pending",
                outcome_observed_at=None,
                ingested_at=as_of_iso,
            )
        )


def _qualifying_articles_present(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    media_silence_hours: int,
) -> bool:
    """Return ``True`` when at least one regime-relevant article qualifies."""
    if not universe_tickers:
        return False
    silence_start = as_of - timedelta(hours=media_silence_hours)
    silence_start_iso = _format_iso_utc(silence_start)
    as_of_iso = _format_iso_utc(as_of)
    stmt = (
        select(NewsArticles.topic_tags)
        .join(
            NewsArticleTickers,
            NewsArticles.article_id == NewsArticleTickers.article_id,
        )
        .where(
            NewsArticleTickers.ticker.in_(list(universe_tickers)),
            NewsArticles.published_at >= silence_start_iso,
            NewsArticles.published_at <= as_of_iso,
            NewsArticles.topic_tags.isnot(None),
        )
    )
    rows = session.execute(stmt).scalars().all()
    for raw_tags in rows:
        if raw_tags is None:
            continue
        try:
            parsed = json.loads(raw_tags)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        article_tags: set[HeadlineType] = set()
        for tag in parsed:
            if not isinstance(tag, str):
                continue
            try:
                article_tags.add(HeadlineType(tag))
            except ValueError:
                continue
        if article_tags & NARRATIVE_LAG_REGIME_TAGS:
            return True
    return False


def _select_pair_lag_estimate(
    session: Session,
    *,
    lead: str,
    lag: str,
    as_of: datetime,
) -> tuple[float, int, str] | None:
    """Return the most recent persisted ``(lag_days, n_events, state)`` triple."""
    as_of_str = _format_iso_utc(as_of)
    stmt = (
        select(
            DistillationPairLag.lead_lag_days_estimate,
            DistillationPairLag.n_pair_events,
            DistillationPairLag.calibration_state,
        )
        .where(
            DistillationPairLag.lead_ticker == lead,
            DistillationPairLag.lag_ticker == lag,
            DistillationPairLag.as_of <= as_of_str,
        )
        .order_by(DistillationPairLag.as_of.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    estimate, n_events, state = row
    return float(estimate), int(n_events), str(state)


def _select_recent_returns_window(
    session: Session,
    *,
    ticker: str,
    as_of: datetime,
    window_days: int,
) -> list[float]:
    """Return ``window_days`` ascending day-over-day percentage returns up to ``as_of``."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    closes = _select_close_series(
        session, ticker=ticker, range_start=range_start, range_end=range_end
    )
    out: list[float] = []
    for prior, later in pairwise(closes):
        if prior > 0.0:
            out.append((later - prior) / prior)
    return out


# ---------------------------------------------------------------------------
# Per-sub block loaders
# ---------------------------------------------------------------------------


def _load_intra_sector_blocks(
    session: Session,
    *,
    sector_roster: Mapping[str, Sequence[str]],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
) -> tuple[OutputBlock, ...]:
    """Read per-sector closes, compute correlation matrices, write divergence events."""
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)
    blocks: list[OutputBlock] = []
    for sector in sorted(sector_roster):
        sector_tickers = tuple(sector_roster[sector])
        long_returns: dict[str, tuple[float, ...]] = {}
        for ticker in sector_tickers:
            closes = _select_close_series(
                session, ticker=ticker, range_start=long_start, range_end=range_end
            )
            long_returns[ticker] = tuple(_log_returns_from_closes(closes))
        block = compute_intra_sector_correlation_pure(
            sector=sector,
            sector_tickers=sector_tickers,
            long_returns_by_ticker=long_returns,
            short_window_days=short_window_days,
            long_window_days=long_window_days,
            divergence_sigma=divergence_sigma,
            as_of=as_of,
        )
        _persist_correlation_divergence_events(session, block=block, as_of=as_of)
        blocks.append(block)
    if blocks:
        session.flush()
    return tuple(blocks)


def _load_cross_sector_blocks(
    session: Session,
    *,
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
) -> tuple[OutputBlock, ...]:
    """Read ETF closes for the cross-sector rotation block."""
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)
    all_etfs = _CROSS_SECTOR_ETFS + _RISK_PROXY_ETFS
    short_closes: dict[str, tuple[float, ...]] = {}
    long_closes: dict[str, tuple[float, ...]] = {}
    for etf in all_etfs:
        long_series = _select_close_series(
            session, ticker=etf, range_start=long_start, range_end=range_end
        )
        long_closes[etf] = tuple(long_series)
        short_closes[etf] = tuple(long_series[-short_window_days:])
    block = compute_cross_sector_rotation_pure(
        short_closes_by_etf=short_closes,
        long_closes_by_etf=long_closes,
        sector_etfs=_CROSS_SECTOR_ETFS,
        risk_proxies=_RISK_PROXY_ETFS,
        short_window_days=short_window_days,
        long_window_days=long_window_days,
        as_of=as_of,
    )
    return (block,)


def _load_breadth_blocks(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    sector_roster: Mapping[str, Sequence[str]],
    as_of: datetime,
) -> tuple[OutputBlock, ...]:
    """Read universe + broad-market closes for the breadth-internals block."""
    long_window = max(EMA_WINDOWS_DAYS)
    range_start, range_end = _window_bounds(as_of=as_of, window_days=long_window)
    closes_by_ticker: dict[str, tuple[float, ...]] = {
        ticker: tuple(
            _select_close_series(
                session, ticker=ticker, range_start=range_start, range_end=range_end
            )
        )
        for ticker in ticker_scope
    }
    broad_market_closes = tuple(
        _select_close_series(
            session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
        )
    )
    block = compute_breadth_internals_pure(
        closes_by_ticker=closes_by_ticker,
        sector_members={k: tuple(v) for k, v in sector_roster.items()},
        universe_tickers=ticker_scope,
        broad_market_closes=broad_market_closes,
        as_of=as_of,
    )
    return (block,)


def _load_intermarket_blocks(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
    short_window_days: int,
) -> tuple[OutputBlock, ...]:
    """Read SPY/TLT/GLD/XLE closes + macro series for the intermarket block."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    range_start_date = (as_of - timedelta(days=window_days)).strftime("%Y-%m-%d")
    range_end_date = as_of.strftime("%Y-%m-%d")
    closes_by_ticker = {
        SPY_TICKER: tuple(
            _select_close_series(
                session, ticker=SPY_TICKER, range_start=range_start, range_end=range_end
            )
        ),
        TLT_TICKER: tuple(
            _select_close_series(
                session, ticker=TLT_TICKER, range_start=range_start, range_end=range_end
            )
        ),
        GLD_TICKER: tuple(
            _select_close_series(
                session, ticker=GLD_TICKER, range_start=range_start, range_end=range_end
            )
        ),
        XLE_TICKER: tuple(
            _select_close_series(
                session, ticker=XLE_TICKER, range_start=range_start, range_end=range_end
            )
        ),
    }
    macros_by_series = {
        REAL_YIELD_SERIES: tuple(
            _select_macro_series(
                session,
                source=REAL_YIELD_SOURCE,
                series_id=REAL_YIELD_SERIES,
                range_start_date=range_start_date,
                range_end_date=range_end_date,
            )
        ),
        VIX_SERIES: tuple(
            _select_macro_series(
                session,
                source=VIX_SOURCE,
                series_id=VIX_SERIES,
                range_start_date=range_start_date,
                range_end_date=range_end_date,
            )
        ),
        OIL_SERIES: tuple(
            _select_macro_series(
                session,
                source=OIL_SOURCE,
                series_id=OIL_SERIES,
                range_start_date=range_start_date,
                range_end_date=range_end_date,
            )
        ),
    }
    return tuple(
        compute_intermarket_regime_pure(
            closes_by_ticker=closes_by_ticker,
            macros_by_series=macros_by_series,
            window_days=window_days,
            short_window_days=short_window_days,
            as_of=as_of,
        )
    )


def _load_lead_lag_blocks(
    session: Session,
    *,
    pairs: Sequence[LeadLagPair],
    as_of: datetime,
    overdue_lead_sigma: float,
    lookback_window_days: int,
) -> tuple[OutputBlock, ...]:
    """Read recent returns + persisted lag estimates for the lead-lag pairs."""
    pair_inputs: list[LeadLagPairInputs] = []
    for pair in pairs:
        window_days = max(lookback_window_days, pair.max_days + 1)
        lead_returns = _select_recent_returns_window(
            session, ticker=pair.lead_ticker, as_of=as_of, window_days=window_days
        )
        lag_returns = _select_recent_returns_window(
            session, ticker=pair.lag_ticker, as_of=as_of, window_days=window_days
        )
        persisted = _select_pair_lag_estimate(
            session, lead=pair.lead_ticker, lag=pair.lag_ticker, as_of=as_of
        )
        pair_inputs.append(
            LeadLagPairInputs(
                pair=pair,
                lead_returns=tuple(lead_returns),
                lag_returns=tuple(lag_returns),
                persisted=persisted,
            )
        )
    return tuple(
        compute_lead_lag_pure(
            pair_inputs=tuple(pair_inputs),
            overdue_lead_sigma=overdue_lead_sigma,
            freshness_ts=as_of,
        )
    )


def _load_correlation_regime_change_blocks(
    session: Session,
    *,
    universe_tickers: Sequence[str],
    as_of: datetime,
    params: CorrelationRegimeChangeParameters,
) -> tuple[OutputBlock, ...]:
    """Read universe returns + qualifying-news flag for the regime-change blocks."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=params.long_window_days)
    long_returns: dict[str, tuple[float, ...]] = {}
    for ticker in universe_tickers:
        closes = _select_close_series(
            session, ticker=ticker, range_start=range_start, range_end=range_end
        )
        long_returns[ticker] = tuple(_log_returns_from_closes(closes))
    qualifying_news = _qualifying_articles_present(
        session,
        universe_tickers=universe_tickers,
        as_of=as_of,
        media_silence_hours=params.media_silence_hours,
    )
    return tuple(
        compute_correlation_regime_change_pure(
            universe_tickers=universe_tickers,
            long_returns_by_ticker=long_returns,
            qualifying_news_present=qualifying_news,
            params=params,
            as_of=as_of,
        )
    )


# ---------------------------------------------------------------------------
# Public pair-correlation helper (unchanged surface; lives here so the
# loader composition has one canonical source).
# ---------------------------------------------------------------------------


def compute_pair_correlations(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: datetime,
    window_days: int,
) -> dict[tuple[str, str], float]:
    """Pairwise correlation dict for ``ticker_scope`` over the trailing window.

    The returned mapping is the canonical input the q3 pair-trade-signature
    detector reads. Both ``(a, b)`` and ``(b, a)`` orderings are populated
    so the q3 scan sees every directional pairing once. Self-pairs are
    omitted — q3's call/put leg-role assignment has no meaning for a
    single ticker.
    """
    from alphamind.distillation.q7._helpers import _correlation_matrix

    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    returns_by_ticker: dict[str, list[float]] = {}
    for ticker in ticker_scope:
        closes = _select_close_series(
            session, ticker=ticker, range_start=range_start, range_end=range_end
        )
        returns_by_ticker[ticker] = _log_returns_from_closes(closes)
    matrix = _correlation_matrix(returns_by_ticker)
    out: dict[tuple[str, str], float] = {}
    for row, columns in matrix.items():
        for col, value in columns.items():
            if row == col:
                continue
            out[(row, col)] = value
    return out


# ---------------------------------------------------------------------------
# Top-level loader
# ---------------------------------------------------------------------------


def load_q7_inputs(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None,
    pair_correlations: Mapping[tuple[str, str], float] | None,
) -> Q7Inputs:
    """Pre-load every q7 input the pure compute consumes.

    Performs all session-bound work:

    1. Resolve universe scope + per-sector roster.
    2. Read per-sector closes for intra-sector correlation; call the pure
       compute; write the divergence-event rows under the shared session.
    3. Read cross-sector ETF closes for the rotation block.
    4. Read universe + SPY closes for the breadth-internals block.
    5. Read intermarket closes + FRED/EIA macro series for the four
       intermarket-regime blocks.
    6. Read recent returns + persisted pair-lag rows for the lead-lag blocks.
    7. Read universe returns + qualifying-news flag for the regime-change
       blocks.

    After this returns, :func:`assemble_q7_blocks_from_inputs` runs in a
    thread without further DB access. The divergence-event writes have
    already executed under the shared session before the parallel core
    fires — no shared-mutable-Session conflict with q1 / q3 / qualitative.

    ``pair_correlations`` threads through :class:`Q7Inputs` for API
    symmetry with the q3 loader; q7's own block emissions don't consume
    it (intra-sector matrices are computed independently).
    """
    scope = (
        tuple(_resolve_universe_tickers(session)) if ticker_scope is None else tuple(ticker_scope)
    )
    if not scope:
        return Q7Inputs(
            as_of=as_of,
            ticker_scope=(),
            intra_sector_blocks=(),
            cross_sector_blocks=(),
            breadth_blocks=(),
            intermarket_blocks=(),
            lead_lag_blocks=(),
            correlation_regime_change_blocks=(),
            pair_correlations=pair_correlations,
        )

    pw = config.persistence_windows
    sector_roster = _resolve_sector_roster(session, ticker_scope=scope)

    intra_sector_blocks = _load_intra_sector_blocks(
        session,
        sector_roster=sector_roster,
        as_of=as_of,
        short_window_days=pw.correlation_short_days,
        long_window_days=pw.correlation_long_days,
        divergence_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
    )
    cross_sector_blocks = _load_cross_sector_blocks(
        session,
        as_of=as_of,
        short_window_days=pw.correlation_short_days,
        long_window_days=pw.correlation_long_days,
    )
    breadth_blocks = _load_breadth_blocks(
        session,
        ticker_scope=scope,
        sector_roster=sector_roster,
        as_of=as_of,
    )
    intermarket_blocks = _load_intermarket_blocks(
        session,
        as_of=as_of,
        window_days=pw.correlation_long_days,
        short_window_days=pw.correlation_short_days,
    )

    pair_max_days: Mapping[str, int] = {
        "credit_to_equity": config.lead_lag.lead_lag_credit_to_equity_max_days,
        "semis_to_tech": config.lead_lag.lead_lag_semis_to_tech_max_days,
        "financials_to_market": config.lead_lag.lead_lag_financials_to_market_max_days,
        "commodity_to_energy_equity": config.lead_lag.lead_lag_commodity_to_energy_equity_max_days,
    }
    pairs = tuple(
        LeadLagPair(
            pair_key=p.key,
            lead_ticker=p.lead,
            lag_ticker=p.lag,
            max_days=pair_max_days[p.key],
        )
        for p in config.lead_lag.pairs
    )
    lead_lag_blocks = _load_lead_lag_blocks(
        session,
        pairs=pairs,
        as_of=as_of,
        overdue_lead_sigma=config.lead_lag.lead_lag_overdue_lead_sigma,
        lookback_window_days=LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
    )

    regime_change_params = CorrelationRegimeChangeParameters(
        short_window_days=pw.correlation_short_days,
        long_window_days=pw.correlation_long_days,
        correlation_breakdown_sigma=config.narrative_lag.correlation_breakdown_sigma,
        dispersion_window_days=pw.correlation_short_days,
        dispersion_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
        media_silence_hours=config.narrative_lag.narrative_lag_media_silence_hours,
    )
    correlation_regime_change_blocks = _load_correlation_regime_change_blocks(
        session,
        universe_tickers=scope,
        as_of=as_of,
        params=regime_change_params,
    )

    return Q7Inputs(
        as_of=as_of,
        ticker_scope=scope,
        intra_sector_blocks=intra_sector_blocks,
        cross_sector_blocks=cross_sector_blocks,
        breadth_blocks=breadth_blocks,
        intermarket_blocks=intermarket_blocks,
        lead_lag_blocks=lead_lag_blocks,
        correlation_regime_change_blocks=correlation_regime_change_blocks,
        pair_correlations=pair_correlations,
    )


__all__ = [
    "NARRATIVE_LAG_REGIME_TAGS",
    "LeadLagInputs",
    "Q7Inputs",
    "compute_pair_correlations",
    "load_q7_inputs",
]
