"""SqlDistillationRepository — concrete impl of :class:`DistillationRepository`.

Closes over a SQLAlchemy ``Session`` and implements the pilot-scoped read
surface declared in :mod:`alphamind.distillation._repository`. Every method
projects ORM rows into the frozen-dataclass shapes the compute layer
consumes — no ORM objects leak past the repository boundary.

The orchestrator constructs one instance per pipeline invocation at the
composition root and threads it into the per-category loaders.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CalibrationState
from alphamind.distillation._repository import (
    ContractCurrentStateRow,
    ContractHistoryEntry,
    ContractMetadataRow,
    DailyBarRow,
    DistillationRepository,
    GapEventCounts,
    NewsLabelCountsRow,
    OptionsContractRow,
    OptionsContractSnapshotRow,
    SectorClassificationRow,
    TickerADVRow,
    TickerBaselineRow,
)
from alphamind.distillation.baselines import PENDING_OUTCOME
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationTickerBaseline,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
)

# Mirror q1/output_blocks AUDIENCE_BY_SECTOR — production scope is "tickers
# classified into one of the three audience-covered alphamind_sectors".
# Imported lazily to avoid pulling the q1 module into the repository
# bootstrap order; instead the constants are inlined here. If the audience
# set grows the SqlDistillationRepository default-scope query expands here.
_AUDIENCE_COVERED_SECTORS: tuple[str, ...] = ("tech", "semis", "financials", "energy")


class SqlDistillationRepository(DistillationRepository):
    """Concrete repository that proxies every method to one ``Session``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- q1 universe / sector ---------------------------------------------

    def load_default_ticker_scope(self) -> tuple[str, ...]:
        rows = self._session.execute(
            select(AssetUniverse.ticker)
            .join(SectorClassification, SectorClassification.ticker == AssetUniverse.ticker)
            .where(SectorClassification.alphamind_sector.in_(_AUDIENCE_COVERED_SECTORS))
            .order_by(AssetUniverse.ticker)
        ).all()
        return tuple(row[0] for row in rows)

    def load_sector_classifications(
        self, *, tickers: Sequence[str]
    ) -> dict[str, SectorClassificationRow]:
        if not tickers:
            return {}
        rows = self._session.execute(
            select(
                SectorClassification.ticker,
                SectorClassification.alphamind_sector,
                SectorClassification.sector_etf,
            ).where(SectorClassification.ticker.in_(tuple(tickers)))
        ).all()
        return {
            ticker: SectorClassificationRow(
                ticker=ticker,
                alphamind_sector=alphamind_sector,
                sector_etf=sector_etf,
            )
            for ticker, alphamind_sector, sector_etf in rows
        }

    # --- q1 bars / baselines ----------------------------------------------

    def load_daily_bars(self, *, ticker: str, as_of: str, days: int) -> tuple[DailyBarRow, ...]:
        stmt = (
            select(OhlcvBars)
            .where(
                OhlcvBars.ticker == ticker,
                OhlcvBars.timeframe == "1d",
                OhlcvBars.period_start <= as_of,
            )
            .order_by(OhlcvBars.period_start.desc())
            .limit(days)
        )
        rows = list(self._session.execute(stmt).scalars().all())
        rows.reverse()
        return tuple(
            DailyBarRow(
                ticker=row.ticker,
                period_start=row.period_start,
                adj_open=float(row.adj_open),
                adj_high=float(row.adj_high),
                adj_low=float(row.adj_low),
                adj_close=float(row.adj_close),
                adj_volume=int(row.adj_volume),
            )
            for row in rows
        )

    def load_latest_baseline(
        self, *, ticker: str, kind: str, as_of: str
    ) -> TickerBaselineRow | None:
        stmt = (
            select(DistillationTickerBaseline)
            .where(
                DistillationTickerBaseline.ticker == ticker,
                DistillationTickerBaseline.baseline_kind == kind,
                DistillationTickerBaseline.as_of <= as_of,
            )
            .order_by(DistillationTickerBaseline.as_of.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return TickerBaselineRow(
            ticker=row.ticker,
            baseline_kind=row.baseline_kind,
            as_of=row.as_of,
            mean=float(row.mean),
            stdev=float(row.stdev),
            n_observations=int(row.n_observations),
            window_days=int(row.window_days),
            calibration_state=row.calibration_state,
        )

    # --- q1 gap history ---------------------------------------------------

    def load_gap_fill_event_counts(self, *, ticker: str, as_of: str) -> GapEventCounts:
        base = (
            select(func.count())
            .select_from(DistillationEventHistory)
            .where(
                DistillationEventHistory.ticker == ticker,
                DistillationEventHistory.event_kind == "gap",
                DistillationEventHistory.outcome != PENDING_OUTCOME,
                DistillationEventHistory.event_ts <= as_of,
            )
        )
        resolved = int(self._session.execute(base).scalar_one())
        if resolved == 0:
            return GapEventCounts(resolved=0, filled=0)
        filled = int(
            self._session.execute(
                base.where(DistillationEventHistory.outcome == "filled")
            ).scalar_one()
        )
        return GapEventCounts(resolved=resolved, filled=filled)

    def load_sector_pooled_gap_fill_counts(self, *, sector: str, as_of: str) -> GapEventCounts:
        base = (
            select(func.count())
            .select_from(DistillationEventHistory)
            .join(
                SectorClassification,
                SectorClassification.ticker == DistillationEventHistory.ticker,
            )
            .where(
                SectorClassification.alphamind_sector == sector,
                DistillationEventHistory.event_kind == "gap",
                DistillationEventHistory.outcome != PENDING_OUTCOME,
                DistillationEventHistory.event_ts <= as_of,
            )
        )
        resolved = int(self._session.execute(base).scalar_one())
        if resolved == 0:
            return GapEventCounts(resolved=0, filled=0)
        filled = int(
            self._session.execute(
                base.where(DistillationEventHistory.outcome == "filled")
            ).scalar_one()
        )
        return GapEventCounts(resolved=resolved, filled=filled)

    # --- q3 flow classification -------------------------------------------

    def load_options_contracts_for_underlying(
        self, *, underlying: str
    ) -> tuple[OptionsContractRow, ...]:
        stmt = (
            select(OptionsContracts)
            .where(OptionsContracts.underlying_ticker == underlying)
            .order_by(OptionsContracts.contract_ticker)
        )
        rows = self._session.execute(stmt).scalars().all()
        return tuple(
            OptionsContractRow(
                contract_ticker=row.contract_ticker,
                underlying_ticker=row.underlying_ticker,
                contract_type=row.contract_type,
            )
            for row in rows
        )

    def load_latest_options_snapshot_at(
        self, *, contract_ticker: str, as_of: str
    ) -> OptionsContractSnapshotRow | None:
        stmt = select(OptionsContractSnapshots).where(
            OptionsContractSnapshots.contract_ticker == contract_ticker,
            OptionsContractSnapshots.snapshot_ts == as_of,
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return OptionsContractSnapshotRow(
            contract_ticker=row.contract_ticker,
            snapshot_ts=row.snapshot_ts,
            volume_today=int(row.volume_today) if row.volume_today is not None else None,
            open_interest=int(row.open_interest) if row.open_interest is not None else None,
        )

    def load_prior_options_snapshot(
        self, *, contract_ticker: str, as_of: str
    ) -> OptionsContractSnapshotRow | None:
        stmt = (
            select(OptionsContractSnapshots)
            .where(
                OptionsContractSnapshots.contract_ticker == contract_ticker,
                OptionsContractSnapshots.snapshot_ts < as_of,
            )
            .order_by(OptionsContractSnapshots.snapshot_ts.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return OptionsContractSnapshotRow(
            contract_ticker=row.contract_ticker,
            snapshot_ts=row.snapshot_ts,
            volume_today=int(row.volume_today) if row.volume_today is not None else None,
            open_interest=int(row.open_interest) if row.open_interest is not None else None,
        )

    def load_ticker_adv(self, *, ticker: str) -> TickerADVRow | None:
        # One query selecting both the ticker key (presence sentinel) and
        # the ADV column. Missing row → None; present row with a NULL ADV
        # → TickerADVRow(avg_daily_volume_shares=None).
        stmt = select(AssetUniverse.ticker, AssetUniverse.avg_daily_volume_shares).where(
            AssetUniverse.ticker == ticker
        )
        row = self._session.execute(stmt).one_or_none()
        if row is None:
            return None
        _present_ticker, adv = row
        return TickerADVRow(
            ticker=ticker,
            avg_daily_volume_shares=float(adv) if adv is not None else None,
        )

    # --- qualitative news --------------------------------------------------

    def load_news_article_label_counts(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> NewsLabelCountsRow:
        stmt = (
            select(NewsArticleTickers.vendor_sentiment_label)
            .join(NewsArticles, NewsArticles.article_id == NewsArticleTickers.article_id)
            .where(
                NewsArticleTickers.ticker == ticker,
                NewsArticles.published_at >= range_start,
                NewsArticles.published_at <= range_end,
            )
        )
        counter: Counter[str] = Counter()
        for label in self._session.execute(stmt).scalars().all():
            if label is None:
                continue
            counter[label] += 1
        return NewsLabelCountsRow(
            positive=counter.get("positive", 0),
            negative=counter.get("negative", 0),
            neutral=counter.get("neutral", 0),
            mixed=counter.get("mixed", 0),
        )

    def load_news_article_sentiment_scores(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> tuple[float, int] | None:
        stmt = (
            select(NewsArticleTickers.vendor_sentiment_score)
            .join(NewsArticles, NewsArticles.article_id == NewsArticleTickers.article_id)
            .where(
                NewsArticleTickers.ticker == ticker,
                NewsArticleTickers.vendor_sentiment_score.isnot(None),
                NewsArticles.published_at >= range_start,
                NewsArticles.published_at <= range_end,
            )
        )
        scores = [float(v) for v in self._session.execute(stmt).scalars().all() if v is not None]
        if not scores:
            return None
        return statistics.fmean(scores), len(scores)

    def load_hourly_window_price_change(
        self, *, ticker: str, range_start: str, range_end: str
    ) -> float | None:
        stmt = (
            select(OhlcvBars.adj_open, OhlcvBars.adj_close, OhlcvBars.period_start)
            .where(
                OhlcvBars.ticker == ticker,
                OhlcvBars.timeframe == "1h",
                OhlcvBars.period_start >= range_start,
                OhlcvBars.period_start <= range_end,
            )
            .order_by(OhlcvBars.period_start)
        )
        rows = self._session.execute(stmt).all()
        if not rows:
            return None
        first_open = float(rows[0][0])
        last_close = float(rows[-1][1])
        return last_close - first_open

    def load_universe_pooled_sentiment_distribution(
        self, *, as_of: str
    ) -> tuple[float, float] | None:
        stmt = select(DistillationTickerBaseline.mean, DistillationTickerBaseline.stdev).where(
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.as_of == as_of,
            DistillationTickerBaseline.calibration_state == CalibrationState.CALIBRATED.value,
        )
        rows = self._session.execute(stmt).all()
        if not rows:
            return None
        means = [float(row.mean) for row in rows]
        stdevs = [float(row.stdev) for row in rows]
        return statistics.fmean(means), statistics.fmean(stdevs)

    # --- qualitative prediction markets ------------------------------------

    def load_contract_history(
        self, *, contract_id: str, range_start: str, range_end: str
    ) -> tuple[ContractHistoryEntry, ...]:
        stmt = (
            select(
                DistillationContractHistory.snapshot_ts,
                DistillationContractHistory.yes_probability,
            )
            .where(
                DistillationContractHistory.contract_id == contract_id,
                DistillationContractHistory.snapshot_ts >= range_start,
                DistillationContractHistory.snapshot_ts <= range_end,
            )
            .order_by(DistillationContractHistory.snapshot_ts)
        )
        return tuple(
            ContractHistoryEntry(snapshot_ts=row[0], yes_probability=float(row[1]))
            for row in self._session.execute(stmt).all()
        )

    def load_contract_current_state(
        self, *, contract_id: str, as_of: str
    ) -> ContractCurrentStateRow | None:
        stmt = (
            select(
                DistillationContractHistory.yes_probability,
                DistillationContractHistory.delta_pp_since_prior,
                DistillationContractHistory.snapshot_ts,
            )
            .where(
                DistillationContractHistory.contract_id == contract_id,
                DistillationContractHistory.snapshot_ts <= as_of,
            )
            .order_by(DistillationContractHistory.snapshot_ts.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).first()
        if row is None:
            return None
        yes_probability, delta_pp, snapshot_ts = row
        return ContractCurrentStateRow(
            yes_probability=float(yes_probability),
            delta_pp_since_prior=float(delta_pp),
            snapshot_ts=snapshot_ts,
        )

    def load_contract_metadata(self, *, contract_id: str) -> ContractMetadataRow | None:
        stmt = select(
            PredictionMarketContracts.platform,
            PredictionMarketContracts.description,
            PredictionMarketContracts.category,
        ).where(PredictionMarketContracts.contract_id == contract_id)
        row = self._session.execute(stmt).first()
        if row is None:
            return None
        platform, description, category = row
        return ContractMetadataRow(platform=platform, description=description, category=category)

    def load_contract_24h_volume_and_liquidity(
        self, *, contract_id: str, as_of: str
    ) -> tuple[float, float]:
        stmt = (
            select(
                PredictionMarketSnapshots.volume_24h_usd,
                PredictionMarketSnapshots.liquidity_usd,
            )
            .where(
                PredictionMarketSnapshots.contract_id == contract_id,
                PredictionMarketSnapshots.snapshot_ts <= as_of,
            )
            .order_by(PredictionMarketSnapshots.snapshot_ts.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).first()
        if row is None:
            return 0.0, 0.0
        volume = float(row[0]) if row[0] is not None else 0.0
        liquidity = float(row[1]) if row[1] is not None else 0.0
        return volume, liquidity


__all__ = ["SqlDistillationRepository"]
