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
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from alphamind.distillation.calibration import CALIBRATION_STATE_VALUES


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
# Estimate revisions — Q5
# ---------------------------------------------------------------------------


class EarningsEstimateRevisions(Base):
    """
    Analyst-estimate revision history reconstructed by daily diffing of Finnhub consensus.

    One row per (revised_at, ticker, fiscal_year, fiscal_period, metric) change event.
    ``metric`` is ``'eps'`` / ``'revenue'`` / ``'ebitda'``.
    """

    __tablename__ = "earnings_estimate_revisions"

    revised_at: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    fiscal_period: Mapped[str] = mapped_column(Text, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    consensus_value: Mapped[float | None] = mapped_column(Float)
    prior_consensus_value: Mapped[float | None] = mapped_column(Float)
    num_analysts: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ix_earnings_estimate_revisions_ticker_period",
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
    vendor_sentiment_score: Mapped[float | None] = mapped_column(Float)
    vendor_sentiment_label: Mapped[str | None] = mapped_column(Text)

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
# Short selling — Q4
# ---------------------------------------------------------------------------


class ShortInterestSnapshot(Base):
    """
    Bi-monthly FINRA short interest.  One row per ticker per FINRA settlement date.

    Source: ``https://cdn.finra.org/equity/otcmarket/biweekly/shrt{YYYYMMDD}.csv``
    """

    __tablename__ = "short_interest_snapshots"

    settlement_date: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    current_short_shares: Mapped[int] = mapped_column(Integer)
    previous_short_shares: Mapped[int | None] = mapped_column(Integer)
    avg_daily_volume_shares: Mapped[int | None] = mapped_column(Integer)
    days_to_cover: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        Index("ix_short_interest_snapshots_ticker_date", "ticker", "settlement_date"),
    )


class ShortVolumeDaily(Base):
    """
    Daily FINRA Reg SHO short sale volume.  One row per (trade_date, ticker, market).

    Source: ``https://cdn.finra.org/equity/regsho/daily/{PREFIX}shvol{YYYYMMDD}.txt``
    """

    __tablename__ = "short_volume_daily"

    trade_date: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    market: Mapped[str] = mapped_column(Text, primary_key=True)
    short_volume: Mapped[int] = mapped_column(Integer)
    short_exempt_volume: Mapped[int] = mapped_column(Integer)
    total_volume: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_short_volume_daily_ticker_date", "ticker", "trade_date"),)


class BorrowCostDaily(Base):
    """EOD borrow cost and availability from iBorrowDesk.

    Primary key: (observation_date, ticker)
    """

    __tablename__ = "borrow_cost_daily"

    observation_date: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    fee_pct: Mapped[float | None] = mapped_column(Float)
    rebate_pct: Mapped[float | None] = mapped_column(Float)
    available_shares: Mapped[int | None] = mapped_column(Integer)
    intraday_high_fee_pct: Mapped[float | None] = mapped_column(Float)
    intraday_low_fee_pct: Mapped[float | None] = mapped_column(Float)
    intraday_high_available_shares: Mapped[int | None] = mapped_column(Integer)
    intraday_low_available_shares: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_borrow_cost_daily_ticker_date", "ticker", "observation_date"),)


class BorrowCostIntraday(Base):
    """Intraday borrow cost snapshots from iBorrowDesk (~16-min cadence).

    Primary key: (snapshot_at, ticker)
    Rows older than 90 days are pruned by an ops-time policy.
    """

    __tablename__ = "borrow_cost_intraday"

    snapshot_at: Mapped[str] = mapped_column(Text, primary_key=True)
    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    fee_pct: Mapped[float] = mapped_column(Float)
    available_shares: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (Index("ix_borrow_cost_intraday_ticker_ts", "ticker", "snapshot_at"),)


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


# ---------------------------------------------------------------------------
# Distillation state (Class B rolling baselines) — story 02-distillation/03
# ---------------------------------------------------------------------------
#
# CHECK-constraint value sets used across the distillation tables.  Listed
# centrally so the migration and tests reference the same source of truth.
# The ``calibration_state`` vocabulary lives in
# :mod:`alphamind.distillation.calibration` — the calibration framework is
# upstream of the schema, so the schema imports the tuple it must accept.

_CALIBRATION_STATES = CALIBRATION_STATE_VALUES
_BASELINE_KINDS = ("volume", "atr", "spread", "sentiment", "atm_iv")
_EVENT_KINDS = ("gap", "extended_hours")
_REGIME_LABELS = (
    "low_vol_compression",
    "vol_expansion",
    "crisis_spike",
    "vol_normalization",
)
_TRANSITION_STATES = ("stable", "early-weak", "early-strong", "confirmed")
_COMPOSITE_KINDS = ("funding_stress", "market_liquidity")


def _check_in(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    """Build a portable ``column IN (...)`` CHECK constraint."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class DistillationTickerBaseline(Base):
    """Per-ticker rolling state for volume / ATR / spread / sentiment baselines.

    Composite key ``(ticker, baseline_kind, as_of)``.  Read-modify-write per
    Class B refresh (story 07).
    """

    __tablename__ = "distillation_ticker_baseline"

    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    baseline_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    mean: Mapped[float] = mapped_column(Float)
    stdev: Mapped[float] = mapped_column(Float)
    n_observations: Mapped[int] = mapped_column(Integer)
    window_days: Mapped[int] = mapped_column(Integer)
    calibration_state: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "baseline_kind",
            _BASELINE_KINDS,
            "ck_distillation_ticker_baseline_baseline_kind",
        ),
        _check_in(
            "calibration_state",
            _CALIBRATION_STATES,
            "ck_distillation_ticker_baseline_calibration_state",
        ),
        Index(
            "ix_distillation_ticker_baseline_ticker_kind_as_of",
            "ticker",
            "baseline_kind",
            "as_of",
        ),
    )


class DistillationPairLag(Base):
    """Per-pair lead-lag timing estimate.

    Composite key ``(lead_ticker, lag_ticker, as_of)``.
    """

    __tablename__ = "distillation_pair_lag"

    lead_ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    lag_ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    lead_lag_days_estimate: Mapped[float] = mapped_column(Float)
    n_pair_events: Mapped[int] = mapped_column(Integer)
    last_overdue_flag: Mapped[int] = mapped_column(Integer)
    calibration_state: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "calibration_state",
            _CALIBRATION_STATES,
            "ck_distillation_pair_lag_calibration_state",
        ),
        Index(
            "ix_distillation_pair_lag_lead_lag_as_of",
            "lead_ticker",
            "lag_ticker",
            "as_of",
        ),
    )


class DistillationContractHistory(Base):
    """Per-contract prediction-market trailing probability series.

    Composite key ``(contract_id, snapshot_ts)``.
    """

    __tablename__ = "distillation_contract_history"

    contract_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("prediction_market_contracts.contract_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    snapshot_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    yes_probability: Mapped[float] = mapped_column(Float)
    delta_pp_since_prior: Mapped[float] = mapped_column(Float)
    liquidity_usd: Mapped[float] = mapped_column(Float)
    calibration_state: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "calibration_state",
            _CALIBRATION_STATES,
            "ck_distillation_contract_history_calibration_state",
        ),
        Index(
            "ix_distillation_contract_history_contract_ts",
            "contract_id",
            "snapshot_ts",
        ),
    )


class DistillationEventHistory(Base):
    """Per-ticker gap-event and extended-hours-event records with outcomes.

    Composite key ``(ticker, event_kind, event_ts)``.  ``outcome_observed_at``
    is nullable: gap-fill and extended-hours confirmation outcomes resolve
    hours after the event row is first written.
    """

    __tablename__ = "distillation_event_history"

    ticker: Mapped[str] = mapped_column(
        Text,
        ForeignKey("asset_universe.ticker", ondelete="RESTRICT"),
        primary_key=True,
    )
    event_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    event_ts: Mapped[str] = mapped_column(Text, primary_key=True)
    direction: Mapped[str] = mapped_column(Text)
    magnitude_atr_multiple: Mapped[float] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(Text)
    outcome_observed_at: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "event_kind",
            _EVENT_KINDS,
            "ck_distillation_event_history_event_kind",
        ),
        Index(
            "ix_distillation_event_history_ticker_kind_ts",
            "ticker",
            "event_kind",
            "event_ts",
        ),
    )


class DistillationRegimeState(Base):
    """Forward-only volatility regime label per invocation.

    Primary key ``as_of`` (UTC ISO datetime).  See [external.md § 4](
    ../../../docs/design/02-distillation-layer/external.md#4-persistent-state-and-composites)
    for the four-tier ladder semantics.
    """

    __tablename__ = "distillation_regime_state"

    as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    regime_label: Mapped[str] = mapped_column(Text)
    vix_level: Mapped[float] = mapped_column(Float)
    term_structure_basis: Mapped[float] = mapped_column(Float)
    vvix_percentile: Mapped[float] = mapped_column(Float)
    realized_vol: Mapped[float] = mapped_column(Float)
    indicator_agreement_count: Mapped[int] = mapped_column(Integer)
    invocations_held: Mapped[int] = mapped_column(Integer)
    transition_state: Mapped[str] = mapped_column(Text)
    prior_label: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "regime_label",
            _REGIME_LABELS,
            "ck_distillation_regime_state_regime_label",
        ),
        _check_in(
            "transition_state",
            _TRANSITION_STATES,
            "ck_distillation_regime_state_transition_state",
        ),
        Index("ix_distillation_regime_state_as_of", "as_of"),
    )


class DistillationCompositeState(Base):
    """Funding-stress and market-liquidity composite trailing distributions.

    Composite key ``(composite_kind, as_of)``.  ``component_breakdown_json``
    is TEXT (JSON serialized) for audit, not query — component count differs
    per composite.
    """

    __tablename__ = "distillation_composite_state"

    composite_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    as_of: Mapped[str] = mapped_column(Text, primary_key=True)
    composite_value: Mapped[float] = mapped_column(Float)
    component_breakdown_json: Mapped[str] = mapped_column(Text)
    percentile_60d: Mapped[float] = mapped_column(Float)
    alert_active: Mapped[int] = mapped_column(Integer)
    calibration_state: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        _check_in(
            "composite_kind",
            _COMPOSITE_KINDS,
            "ck_distillation_composite_state_composite_kind",
        ),
        _check_in(
            "calibration_state",
            _CALIBRATION_STATES,
            "ck_distillation_composite_state_calibration_state",
        ),
        Index(
            "ix_distillation_composite_state_kind_as_of",
            "composite_kind",
            "as_of",
        ),
    )
