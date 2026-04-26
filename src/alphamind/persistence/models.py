"""
SQLAlchemy 2.0 declarative models for AlphaMind's data layer.

Every table in ``docs/design/01-data-layer/collector/storage.md § Tables``
is represented here.  Column names, types, nullability, primary-key
participation, foreign keys, and indexes match the spec exactly.

Storage note: UTC timestamps and dates are stored as TEXT (ISO 8601).
Booleans are stored as INTEGER (SQLite has no native BOOLEAN type).
"""

from __future__ import annotations

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------


class AssetUniverse(Base):
    """One row per ticker; covers both universe and benchmark instruments."""

    __tablename__ = "asset_universe"

    asset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    asset_class: Mapped[str] = mapped_column(Text)
    asset_role: Mapped[str] = mapped_column(Text)
    exchange: Mapped[str] = mapped_column(Text)
    cik: Mapped[str | None] = mapped_column(Text)
    figi: Mapped[str | None] = mapped_column(Text)
    isin: Mapped[str | None] = mapped_column(Text)
    shares_outstanding: Mapped[int | None] = mapped_column(Integer)
    float_shares: Mapped[int | None] = mapped_column(Integer)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    avg_daily_volume_shares: Mapped[int | None] = mapped_column(Integer)
    avg_daily_volume_notional_usd: Mapped[float | None] = mapped_column(Float)
    beta_spy: Mapped[float | None] = mapped_column(Float)
    analyst_count: Mapped[int | None] = mapped_column(Integer)
    options_chain_liquid: Mapped[int | None] = mapped_column(Integer)
    ipo_date: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer)
    added_date: Mapped[str] = mapped_column(Text)
    removed_date: Mapped[str | None] = mapped_column(Text)
    removal_reason: Mapped[str | None] = mapped_column(Text)
    last_updated: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_asset_universe_role_active", "asset_role", "is_active"),)


class SectorClassification(Base):
    """Ticker → AlphaMind sector mapping with peer group and sector ETF."""

    __tablename__ = "sector_classification"

    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    asset_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.asset_id", ondelete="RESTRICT"),
        nullable=False,
    )
    alphamind_sector: Mapped[str] = mapped_column(Text)
    domain_researcher: Mapped[str] = mapped_column(Text)
    gics_sector: Mapped[str | None] = mapped_column(Text)
    gics_industry_group: Mapped[str | None] = mapped_column(Text)
    gics_industry: Mapped[str | None] = mapped_column(Text)
    gics_sub_industry: Mapped[str | None] = mapped_column(Text)
    sector_etf: Mapped[str] = mapped_column(Text)
    peer_group: Mapped[str | None] = mapped_column(Text)
    classification_source: Mapped[str] = mapped_column(Text)
    last_updated: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_sector_classification_sector", "alphamind_sector"),
        Index("ix_sector_classification_peer_group", "peer_group"),
    )


class EtfMembership(Base):
    """Many-to-many: ticker x ETF x weight as of a rebalance date."""

    __tablename__ = "etf_membership"

    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    etf_ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    weight_as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    etf_name: Mapped[str] = mapped_column(Text)
    weight_pct: Mapped[float] = mapped_column(Float)
    is_top_10: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_etf_membership_etf_date", "etf_ticker", "weight_as_of"),
        Index("ix_etf_membership_ticker", "ticker"),
    )


class TickerChangeHistory(Base):
    """Historical symbol changes per asset_id."""

    __tablename__ = "ticker_change_history"

    asset_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.asset_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    effective_date: Mapped[str] = mapped_column(Text, primary_key=True)
    previous_ticker: Mapped[str] = mapped_column(Text)
    new_ticker: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


# ---------------------------------------------------------------------------
# Market data — Q1
# ---------------------------------------------------------------------------


class OhlcvBars(Base):
    """
    Multi-timeframe OHLCV bars with paired adjusted / unadjusted columns.

    Primary key: (ticker, timeframe, period_start)
    """

    __tablename__ = "ohlcv_bars"

    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    period_start: Mapped[str] = mapped_column(Text, primary_key=True)
    period_end: Mapped[str] = mapped_column(Text)
    session: Mapped[str] = mapped_column(Text)

    adj_open: Mapped[float] = mapped_column(Float)
    adj_high: Mapped[float] = mapped_column(Float)
    adj_low: Mapped[float] = mapped_column(Float)
    adj_close: Mapped[float] = mapped_column(Float)
    adj_volume: Mapped[int] = mapped_column(Integer)
    adj_vwap: Mapped[float | None] = mapped_column(Float)

    unadj_open: Mapped[float] = mapped_column(Float)
    unadj_high: Mapped[float] = mapped_column(Float)
    unadj_low: Mapped[float] = mapped_column(Float)
    unadj_close: Mapped[float] = mapped_column(Float)
    unadj_volume: Mapped[int] = mapped_column(Integer)
    unadj_vwap: Mapped[float | None] = mapped_column(Float)

    trade_count: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_ohlcv_bars_timeframe_start", "timeframe", "period_start"),)


# ---------------------------------------------------------------------------
# Market data — Q12 corporate actions
# ---------------------------------------------------------------------------


class CorporateActions(Base):
    """Dividends, splits, spin-offs, mergers, symbol changes."""

    __tablename__ = "corporate_actions"

    action_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(Text)
    declaration_date: Mapped[str | None] = mapped_column(Text)
    ex_date: Mapped[str] = mapped_column(Text)
    record_date: Mapped[str | None] = mapped_column(Text)
    payable_date: Mapped[str | None] = mapped_column(Text)
    ratio: Mapped[float | None] = mapped_column(Float)
    cash_amount_per_share: Mapped[float | None] = mapped_column(Float)
    new_ticker: Mapped[str | None] = mapped_column(Text)
    acquirer_ticker: Mapped[str | None] = mapped_column(Text)
    spin_off_ticker: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_corporate_actions_ticker_exdate", "ticker", "ex_date"),
        Index("ix_corporate_actions_type_exdate", "action_type", "ex_date"),
    )


# ---------------------------------------------------------------------------
# Options — Q3
# ---------------------------------------------------------------------------


class OptionsContracts(Base):
    """Per-contract reference (slow-changing)."""

    __tablename__ = "options_contracts"

    contract_ticker: Mapped[str] = mapped_column(Text, primary_key=True)
    underlying_ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        nullable=False,
    )
    expiration_date: Mapped[str] = mapped_column(Text)
    strike_price: Mapped[float] = mapped_column(Float)
    contract_type: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_options_contracts_underlying_expiry",
            "underlying_ticker",
            "expiration_date",
        ),
    )


class OptionsContractSnapshots(Base):
    """Time-series options contract snapshots."""

    __tablename__ = "options_contract_snapshots"

    snapshot_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    contract_ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("options_contracts.contract_ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    underlying_ticker: Mapped[str] = mapped_column(Text)
    open_interest: Mapped[int | None] = mapped_column(Integer)
    volume_today: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[float | None] = mapped_column(Float)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    implied_volatility: Mapped[float | None] = mapped_column(Float)
    delta: Mapped[float | None] = mapped_column(Float)
    gamma: Mapped[float | None] = mapped_column(Float)
    theta: Mapped[float | None] = mapped_column(Float)
    vega: Mapped[float | None] = mapped_column(Float)
    rho: Mapped[float | None] = mapped_column(Float)
    underlying_price: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_options_contract_snapshots_underlying_ts",
            "underlying_ticker",
            "snapshot_ts",
        ),
    )


# ---------------------------------------------------------------------------
# Macro — Q6
# ---------------------------------------------------------------------------


class MacroObservations(Base):
    """
    Generic time-series for FRED, EIA, BLS, Treasury fiscal data.

    Primary key: (source, series_id, observation_date, revision_number)
    """

    __tablename__ = "macro_observations"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    series_id: Mapped[str] = mapped_column(Text, primary_key=True)
    observation_date: Mapped[str] = mapped_column(Text, primary_key=True)
    revision_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    release_date: Mapped[str | None] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float)
    units: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_macro_observations_series_date", "series_id", "observation_date"),
        Index("ix_macro_observations_release_date", "release_date"),
    )


class TreasuryAuctions(Base):
    """Specialized treasury auction results table."""

    __tablename__ = "treasury_auctions"

    auction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenor: Mapped[str] = mapped_column(Text)
    auction_date: Mapped[str] = mapped_column(Text)
    auction_yield_bp: Mapped[float | None] = mapped_column(Float)
    bid_to_cover: Mapped[float | None] = mapped_column(Float)
    tail_bp: Mapped[float | None] = mapped_column(Float)
    primary_dealer_pct: Mapped[float | None] = mapped_column(Float)
    indirect_pct: Mapped[float | None] = mapped_column(Float)
    direct_pct: Mapped[float | None] = mapped_column(Float)
    auction_size_usd: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_treasury_auctions_date_tenor", "auction_date", "tenor"),)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class EventCalendar(Base):
    """
    All scheduled events with an event_type discriminator.

    ``last_updated`` is mutable and self-maintains via onupdate.
    """

    __tablename__ = "event_calendar"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        nullable=True,
    )
    scheduled_at: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)
    last_updated: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_event_calendar_scheduled_at", "scheduled_at"),
        Index("ix_event_calendar_ticker_scheduled", "ticker", "scheduled_at"),
        Index("ix_event_calendar_type_scheduled", "event_type", "scheduled_at"),
    )


class EarningsEventDetails(Base):
    """One-to-one extension to event_calendar rows where event_type='earnings'."""

    __tablename__ = "earnings_event_details"

    event_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("event_calendar.event_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(Text)
    fiscal_period: Mapped[str] = mapped_column(Text)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    expected_call_time: Mapped[str | None] = mapped_column(Text)
    eps_consensus: Mapped[float | None] = mapped_column(Float)
    eps_actual: Mapped[float | None] = mapped_column(Float)
    revenue_consensus_usd: Mapped[float | None] = mapped_column(Float)
    revenue_actual_usd: Mapped[float | None] = mapped_column(Float)
    reported_at: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_earnings_event_details_ticker_year_period",
            "ticker",
            "fiscal_year",
            "fiscal_period",
        ),
    )


# ---------------------------------------------------------------------------
# News — Qual1
# ---------------------------------------------------------------------------


class NewsArticles(Base):
    """Article metadata; body text stored on disk."""

    __tablename__ = "news_articles"

    article_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text)
    source_outlet: Mapped[str | None] = mapped_column(Text)
    source_credibility_tier: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    headline_text: Mapped[str] = mapped_column(Text)
    body_path: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)
    vendor_sentiment_score: Mapped[float | None] = mapped_column(Float)
    vendor_sentiment_label: Mapped[str | None] = mapped_column(Text)
    topic_tags: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_news_articles_published_at_desc", "published_at"),
        Index("ix_news_articles_source_published", "source", "published_at"),
    )


class NewsArticleTickers(Base):
    """Many-to-many join of articles to mentioned tickers."""

    __tablename__ = "news_article_tickers"

    article_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("news_articles.article_id", ondelete="CASCADE"),
        primary_key=True,
    )
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    is_primary: Mapped[int] = mapped_column(Integer)

    __table_args__ = (Index("ix_news_article_tickers_ticker", "ticker", "article_id"),)


# ---------------------------------------------------------------------------
# Prediction markets — Qual3
# ---------------------------------------------------------------------------


class PredictionMarketContracts(Base):
    """Per-contract reference (slow-changing)."""

    __tablename__ = "prediction_market_contracts"

    contract_id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    resolution_date: Mapped[str | None] = mapped_column(Text)
    resolution_outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_prediction_market_contracts_platform_cat", "platform", "category"),
        Index("ix_prediction_market_contracts_resolution_date", "resolution_date"),
    )


class PredictionMarketSnapshots(Base):
    """Per-contract probability and liquidity over time."""

    __tablename__ = "prediction_market_snapshots"

    contract_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("prediction_market_contracts.contract_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    yes_probability: Mapped[float] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    bid: Mapped[float | None] = mapped_column(Float)
    ask: Mapped[float | None] = mapped_column(Float)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_prediction_market_snapshots_ts", "snapshot_ts"),)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class CollectionRuns(Base):
    """One row per collection-function invocation for ops visibility."""

    __tablename__ = "collection_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    collector: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    rows_written: Mapped[int | None] = mapped_column(Integer)
    error_summary: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_collection_runs_collector_started", "collector", "started_at"),)
