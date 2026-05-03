"""Top-level Q7 assembly entry point and pair-correlation helper.

Implements ``assemble_q7_blocks`` and ``compute_pair_correlations``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.q7._helpers import (
    _correlation_matrix,
    _log_returns_from_closes,
    _select_close_series,
    _window_bounds,
)
from alphamind.distillation.q7.breadth_internals import compute_breadth_internals
from alphamind.distillation.q7.correlation_regime_change import (
    CorrelationRegimeChangeConfig,
    compute_correlation_regime_change,
)
from alphamind.distillation.q7.cross_sector_rotation import compute_cross_sector_rotation
from alphamind.distillation.q7.intermarket_regime import SPY_TICKER, compute_intermarket_regime
from alphamind.distillation.q7.intra_sector_correlation import compute_intra_sector_correlation
from alphamind.distillation.q7.lead_lag import LeadLagPair, compute_lead_lag
from alphamind.persistence.models import AssetUniverse, SectorClassification

# The four cross-sector ETFs the cross-sector rotation block reads. Pinned
# here per story 08d's named ETF roster (XLK / SMH / XLF / XLE); the
# constants live alongside ``SPY_TICKER`` etc. so the q7 module is the
# single source of truth for which tickers it pulls.
_CROSS_SECTOR_ETFS: tuple[str, ...] = ("XLK", "SMH", "XLF", "XLE")
"""ETF tickers driving the cross-sector rotation block."""

_RISK_PROXY_ETFS: tuple[str, ...] = ("IWM", "SPY")
"""Risk-appetite proxies (small-cap vs. broad market)."""


def _resolve_universe_tickers(session: Session) -> list[str]:
    """Return every ticker in ``asset_universe`` ascending.

    The cross-asset compute functions need broad coverage (sector-pair
    correlations, breadth) so the default scope is the full universe.
    """
    stmt = select(AssetUniverse.ticker).order_by(AssetUniverse.ticker)
    return [str(t) for t in session.execute(stmt).scalars().all()]


def _resolve_sector_roster(
    session: Session,
    *,
    ticker_scope: Sequence[str],
) -> dict[str, list[str]]:
    """Return ``{alphamind_sector: [tickers...]}`` restricted to ``ticker_scope``.

    Sector membership comes from ``sector_classification.alphamind_sector``;
    tickers in ``ticker_scope`` without a classification row are skipped.
    Result keys iterate sorted for deterministic downstream block_ids.
    """
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


def compute_pair_correlations(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: datetime,
    window_days: int,
) -> dict[tuple[str, str], float]:
    """Pairwise correlation dict for ``ticker_scope`` over the trailing window.

    The returned mapping is the canonical input the q3 pair-trade-signature
    detector reads (see :func:`alphamind.distillation.q3_options.detect_pair_trade_signatures`).
    Both ``(a, b)`` and ``(b, a)`` orderings are populated so the q3 scan
    sees every directional pairing once.

    Self-pairs (``a == b``) are omitted — q3's call/put leg-role assignment
    has no meaning for a single ticker.
    """
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


def assemble_q7_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
) -> list[OutputBlock]:
    """Top-level q7 entry point — six compute functions packaged as blocks.

    Resolves ``ticker_scope`` (defaults to every ticker in
    ``asset_universe`` when ``None``) and the sector roster, then dispatches
    the six independent compute helpers and concatenates their blocks.

    Audience routing follows story 08d:

    - ``q7.intra_sector_correlation.<sector>``,
      ``q7.cross_sector_rotation``, ``q7.lead_lag.<pair>``,
      ``q7.correlation_breakdown.<pair>``,
      ``q7.correlation_breakdown.dispersion_shift``,
      ``q7.narrative_lag`` — :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    - ``q7.breadth_internals``, ``q7.intermarket_regime.*`` — both
      :attr:`OutputAudience.CORRELATION_REGIME_BRIEF` and
      :attr:`OutputAudience.UNIVERSAL_BROADCAST`.

    An empty ``ticker_scope`` returns ``[]`` — the q7 layer has no
    standalone signal absent universe coverage.
    """
    if ticker_scope is None:
        scope = tuple(_resolve_universe_tickers(session))
    else:
        scope = tuple(ticker_scope)
    if not scope:
        return []

    pw = config.persistence_windows
    sector_roster = _resolve_sector_roster(session, ticker_scope=scope)

    blocks: list[OutputBlock] = []

    # Intra-sector correlation: one block per classified sector.
    # ``_resolve_sector_roster`` only registers a sector key when at least
    # one ticker maps to it, so no empty-sector guard is needed here.
    for sector in sorted(sector_roster):
        blocks.extend(
            compute_intra_sector_correlation(
                session,
                sector=sector,
                sector_tickers=tuple(sector_roster[sector]),
                as_of=as_of,
                short_window_days=pw.correlation_short_days,
                long_window_days=pw.correlation_long_days,
                divergence_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
            )
        )

    # Cross-sector rotation: one block over the four sector ETFs plus risk
    # proxies.
    blocks.extend(
        compute_cross_sector_rotation(
            session,
            sector_etfs=_CROSS_SECTOR_ETFS,
            risk_proxies=_RISK_PROXY_ETFS,
            as_of=as_of,
            short_window_days=pw.correlation_short_days,
            long_window_days=pw.correlation_long_days,
        )
    )

    # Breadth and market internals: read across the full scope, partitioned
    # by alphamind_sector for the advance/decline counts.
    blocks.extend(
        compute_breadth_internals(
            session,
            universe_tickers=scope,
            sectors=tuple(sorted(sector_roster)),
            sector_members={k: tuple(v) for k, v in sector_roster.items()},
            broad_market_etf=SPY_TICKER,
            as_of=as_of,
        )
    )

    # Intermarket regime — fixed series IDs encoded inside the helper.
    blocks.extend(
        compute_intermarket_regime(
            session,
            as_of=as_of,
            window_days=pw.correlation_long_days,
            short_window_days=pw.correlation_short_days,
        )
    )

    # Lead-lag — per-pair ``_max_days`` from the loaded config.
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
    blocks.extend(
        compute_lead_lag(
            session,
            pairs=pairs,
            as_of=as_of,
            overdue_lead_sigma=config.lead_lag.lead_lag_overdue_lead_sigma,
        )
    )

    # Correlation regime change — breakdown / dispersion / narrative-lag.
    blocks.extend(
        compute_correlation_regime_change(
            session,
            universe_tickers=scope,
            as_of=as_of,
            config=CorrelationRegimeChangeConfig(
                short_window_days=pw.correlation_short_days,
                long_window_days=pw.correlation_long_days,
                correlation_shift_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
                dispersion_window_days=pw.correlation_short_days,
                dispersion_sigma=config.narrative_lag.narrative_lag_correlation_shift_sigma,
                media_silence_hours=config.narrative_lag.narrative_lag_media_silence_hours,
            ),
        )
    )

    return blocks
