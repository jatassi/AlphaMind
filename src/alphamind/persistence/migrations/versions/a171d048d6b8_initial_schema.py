"""initial_schema

Revision ID: a171d048d6b8
Revises:
Create Date: 2026-04-26 17:38:37.276896

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a171d048d6b8"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "asset_universe",
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("asset_role", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("cik", sa.Text(), nullable=True),
        sa.Column("figi", sa.Text(), nullable=True),
        sa.Column("isin", sa.Text(), nullable=True),
        sa.Column("shares_outstanding", sa.Integer(), nullable=True),
        sa.Column("float_shares", sa.Integer(), nullable=True),
        sa.Column("market_cap_usd", sa.Float(), nullable=True),
        sa.Column("avg_daily_volume_shares", sa.Integer(), nullable=True),
        sa.Column("avg_daily_volume_notional_usd", sa.Float(), nullable=True),
        sa.Column("beta_spy", sa.Float(), nullable=True),
        sa.Column("analyst_count", sa.Integer(), nullable=True),
        sa.Column("options_chain_liquid", sa.Integer(), nullable=True),
        sa.Column("ipo_date", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Integer(), nullable=False),
        sa.Column("added_date", sa.Text(), nullable=False),
        sa.Column("removed_date", sa.Text(), nullable=True),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column("last_updated", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("ticker"),
    )
    op.create_index(
        "ix_asset_universe_role_active",
        "asset_universe",
        ["asset_role", "is_active"],
        unique=False,
    )
    op.create_table(
        "collection_runs",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("collector", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_written", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ix_collection_runs_collector_started",
        "collection_runs",
        ["collector", "started_at"],
        unique=False,
    )
    op.create_table(
        "macro_observations",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("observation_date", sa.Text(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("release_date", sa.Text(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("units", sa.Text(), nullable=True),
        sa.Column("frequency", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("source", "series_id", "observation_date", "revision_number"),
    )
    op.create_index(
        "ix_macro_observations_release_date",
        "macro_observations",
        ["release_date"],
        unique=False,
    )
    op.create_index(
        "ix_macro_observations_series_date",
        "macro_observations",
        ["series_id", "observation_date"],
        unique=False,
    )
    op.create_table(
        "news_articles",
        sa.Column("article_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_outlet", sa.Text(), nullable=True),
        sa.Column("source_credibility_tier", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("headline_text", sa.Text(), nullable=False),
        sa.Column("body_path", sa.Text(), nullable=True),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.Column("vendor_sentiment_score", sa.Float(), nullable=True),
        sa.Column("vendor_sentiment_label", sa.Text(), nullable=True),
        sa.Column("topic_tags", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("article_id"),
    )
    op.create_index(
        "ix_news_articles_published_at_desc",
        "news_articles",
        ["published_at"],
        unique=False,
    )
    op.create_index(
        "ix_news_articles_source_published",
        "news_articles",
        ["source", "published_at"],
        unique=False,
    )
    op.create_table(
        "prediction_market_contracts",
        sa.Column("contract_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("resolution_date", sa.Text(), nullable=True),
        sa.Column("resolution_outcome", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("contract_id"),
    )
    op.create_index(
        "ix_prediction_market_contracts_platform_cat",
        "prediction_market_contracts",
        ["platform", "category"],
        unique=False,
    )
    op.create_index(
        "ix_prediction_market_contracts_resolution_date",
        "prediction_market_contracts",
        ["resolution_date"],
        unique=False,
    )
    op.create_table(
        "treasury_auctions",
        sa.Column("auction_id", sa.Text(), nullable=False),
        sa.Column("tenor", sa.Text(), nullable=False),
        sa.Column("auction_date", sa.Text(), nullable=False),
        sa.Column("auction_yield_bp", sa.Float(), nullable=True),
        sa.Column("bid_to_cover", sa.Float(), nullable=True),
        sa.Column("tail_bp", sa.Float(), nullable=True),
        sa.Column("primary_dealer_pct", sa.Float(), nullable=True),
        sa.Column("indirect_pct", sa.Float(), nullable=True),
        sa.Column("direct_pct", sa.Float(), nullable=True),
        sa.Column("auction_size_usd", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("auction_id"),
    )
    op.create_index(
        "ix_treasury_auctions_date_tenor",
        "treasury_auctions",
        ["auction_date", "tenor"],
        unique=False,
    )
    op.create_table(
        "corporate_actions",
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("declaration_date", sa.Text(), nullable=True),
        sa.Column("ex_date", sa.Text(), nullable=False),
        sa.Column("record_date", sa.Text(), nullable=True),
        sa.Column("payable_date", sa.Text(), nullable=True),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("cash_amount_per_share", sa.Float(), nullable=True),
        sa.Column("new_ticker", sa.Text(), nullable=True),
        sa.Column("acquirer_ticker", sa.Text(), nullable=True),
        sa.Column("spin_off_ticker", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("action_id"),
    )
    op.create_index(
        "ix_corporate_actions_ticker_exdate",
        "corporate_actions",
        ["ticker", "ex_date"],
        unique=False,
    )
    op.create_index(
        "ix_corporate_actions_type_exdate",
        "corporate_actions",
        ["action_type", "ex_date"],
        unique=False,
    )
    op.create_table(
        "etf_membership",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("etf_ticker", sa.Text(), nullable=False),
        sa.Column("weight_as_of", sa.Text(), nullable=False),
        sa.Column("etf_name", sa.Text(), nullable=False),
        sa.Column("weight_pct", sa.Float(), nullable=False),
        sa.Column("is_top_10", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ticker", "etf_ticker", "weight_as_of"),
    )
    op.create_index(
        "ix_etf_membership_etf_date",
        "etf_membership",
        ["etf_ticker", "weight_as_of"],
        unique=False,
    )
    op.create_index("ix_etf_membership_ticker", "etf_membership", ["ticker"], unique=False)
    op.create_table(
        "event_calendar",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=True),
        sa.Column("scheduled_at", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.Column("last_updated", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_event_calendar_scheduled_at",
        "event_calendar",
        ["scheduled_at"],
        unique=False,
    )
    op.create_index(
        "ix_event_calendar_ticker_scheduled",
        "event_calendar",
        ["ticker", "scheduled_at"],
        unique=False,
    )
    op.create_index(
        "ix_event_calendar_type_scheduled",
        "event_calendar",
        ["event_type", "scheduled_at"],
        unique=False,
    )
    op.create_table(
        "news_article_tickers",
        sa.Column("article_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["article_id"], ["news_articles.article_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("article_id", "ticker"),
    )
    op.create_index(
        "ix_news_article_tickers_ticker",
        "news_article_tickers",
        ["ticker", "article_id"],
        unique=False,
    )
    op.create_table(
        "ohlcv_bars",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("adj_open", sa.Float(), nullable=False),
        sa.Column("adj_high", sa.Float(), nullable=False),
        sa.Column("adj_low", sa.Float(), nullable=False),
        sa.Column("adj_close", sa.Float(), nullable=False),
        sa.Column("adj_volume", sa.Integer(), nullable=False),
        sa.Column("adj_vwap", sa.Float(), nullable=True),
        sa.Column("unadj_open", sa.Float(), nullable=False),
        sa.Column("unadj_high", sa.Float(), nullable=False),
        sa.Column("unadj_low", sa.Float(), nullable=False),
        sa.Column("unadj_close", sa.Float(), nullable=False),
        sa.Column("unadj_volume", sa.Integer(), nullable=False),
        sa.Column("unadj_vwap", sa.Float(), nullable=True),
        sa.Column("trade_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ticker", "timeframe", "period_start"),
    )
    op.create_index(
        "ix_ohlcv_bars_timeframe_start",
        "ohlcv_bars",
        ["timeframe", "period_start"],
        unique=False,
    )
    op.create_table(
        "options_contracts",
        sa.Column("contract_ticker", sa.Text(), nullable=False),
        sa.Column("underlying_ticker", sa.Text(), nullable=False),
        sa.Column("expiration_date", sa.Text(), nullable=False),
        sa.Column("strike_price", sa.Float(), nullable=False),
        sa.Column("contract_type", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["underlying_ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("contract_ticker"),
    )
    op.create_index(
        "ix_options_contracts_underlying_expiry",
        "options_contracts",
        ["underlying_ticker", "expiration_date"],
        unique=False,
    )
    op.create_table(
        "prediction_market_snapshots",
        sa.Column("contract_id", sa.Text(), nullable=False),
        sa.Column("snapshot_ts", sa.Text(), nullable=False),
        sa.Column("yes_probability", sa.Float(), nullable=False),
        sa.Column("volume_24h_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["prediction_market_contracts.contract_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("contract_id", "snapshot_ts"),
    )
    op.create_index(
        "ix_prediction_market_snapshots_ts",
        "prediction_market_snapshots",
        ["snapshot_ts"],
        unique=False,
    )
    op.create_table(
        "sector_classification",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("alphamind_sector", sa.Text(), nullable=False),
        sa.Column("domain_researcher", sa.Text(), nullable=False),
        sa.Column("gics_sector", sa.Text(), nullable=True),
        sa.Column("gics_industry_group", sa.Text(), nullable=True),
        sa.Column("gics_industry", sa.Text(), nullable=True),
        sa.Column("gics_sub_industry", sa.Text(), nullable=True),
        sa.Column("sector_etf", sa.Text(), nullable=False),
        sa.Column("peer_group", sa.Text(), nullable=True),
        sa.Column("classification_source", sa.Text(), nullable=False),
        sa.Column("last_updated", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset_universe.asset_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ticker"], ["asset_universe.ticker"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("ticker"),
    )
    op.create_index(
        "ix_sector_classification_peer_group",
        "sector_classification",
        ["peer_group"],
        unique=False,
    )
    op.create_index(
        "ix_sector_classification_sector",
        "sector_classification",
        ["alphamind_sector"],
        unique=False,
    )
    op.create_table(
        "ticker_change_history",
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Text(), nullable=False),
        sa.Column("previous_ticker", sa.Text(), nullable=False),
        sa.Column("new_ticker", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["asset_universe.asset_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("asset_id", "effective_date"),
    )
    op.create_table(
        "earnings_event_details",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("fiscal_period", sa.Text(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("expected_call_time", sa.Text(), nullable=True),
        sa.Column("eps_consensus", sa.Float(), nullable=True),
        sa.Column("eps_actual", sa.Float(), nullable=True),
        sa.Column("revenue_consensus_usd", sa.Float(), nullable=True),
        sa.Column("revenue_actual_usd", sa.Float(), nullable=True),
        sa.Column("reported_at", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event_calendar.event_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_earnings_event_details_ticker_year_period",
        "earnings_event_details",
        ["ticker", "fiscal_year", "fiscal_period"],
        unique=False,
    )
    op.create_table(
        "options_contract_snapshots",
        sa.Column("snapshot_ts", sa.Text(), nullable=False),
        sa.Column("contract_ticker", sa.Text(), nullable=False),
        sa.Column("underlying_ticker", sa.Text(), nullable=False),
        sa.Column("open_interest", sa.Integer(), nullable=True),
        sa.Column("volume_today", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("bid", sa.Float(), nullable=True),
        sa.Column("ask", sa.Float(), nullable=True),
        sa.Column("implied_volatility", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("rho", sa.Float(), nullable=True),
        sa.Column("underlying_price", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contract_ticker"],
            ["options_contracts.contract_ticker"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("snapshot_ts", "contract_ticker"),
    )
    op.create_index(
        "ix_options_contract_snapshots_underlying_ts",
        "options_contract_snapshots",
        ["underlying_ticker", "snapshot_ts"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_options_contract_snapshots_underlying_ts",
        table_name="options_contract_snapshots",
    )
    op.drop_table("options_contract_snapshots")
    op.drop_index(
        "ix_earnings_event_details_ticker_year_period",
        table_name="earnings_event_details",
    )
    op.drop_table("earnings_event_details")
    op.drop_table("ticker_change_history")
    op.drop_index("ix_sector_classification_sector", table_name="sector_classification")
    op.drop_index("ix_sector_classification_peer_group", table_name="sector_classification")
    op.drop_table("sector_classification")
    op.drop_index(
        "ix_prediction_market_snapshots_ts",
        table_name="prediction_market_snapshots",
    )
    op.drop_table("prediction_market_snapshots")
    op.drop_index("ix_options_contracts_underlying_expiry", table_name="options_contracts")
    op.drop_table("options_contracts")
    op.drop_index("ix_ohlcv_bars_timeframe_start", table_name="ohlcv_bars")
    op.drop_table("ohlcv_bars")
    op.drop_index("ix_news_article_tickers_ticker", table_name="news_article_tickers")
    op.drop_table("news_article_tickers")
    op.drop_index("ix_event_calendar_type_scheduled", table_name="event_calendar")
    op.drop_index("ix_event_calendar_ticker_scheduled", table_name="event_calendar")
    op.drop_index("ix_event_calendar_scheduled_at", table_name="event_calendar")
    op.drop_table("event_calendar")
    op.drop_index("ix_etf_membership_ticker", table_name="etf_membership")
    op.drop_index("ix_etf_membership_etf_date", table_name="etf_membership")
    op.drop_table("etf_membership")
    op.drop_index("ix_corporate_actions_type_exdate", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_ticker_exdate", table_name="corporate_actions")
    op.drop_table("corporate_actions")
    op.drop_index("ix_treasury_auctions_date_tenor", table_name="treasury_auctions")
    op.drop_table("treasury_auctions")
    op.drop_index(
        "ix_prediction_market_contracts_resolution_date",
        table_name="prediction_market_contracts",
    )
    op.drop_index(
        "ix_prediction_market_contracts_platform_cat",
        table_name="prediction_market_contracts",
    )
    op.drop_table("prediction_market_contracts")
    op.drop_index("ix_news_articles_source_published", table_name="news_articles")
    op.drop_index("ix_news_articles_published_at_desc", table_name="news_articles")
    op.drop_table("news_articles")
    op.drop_index("ix_macro_observations_series_date", table_name="macro_observations")
    op.drop_index("ix_macro_observations_release_date", table_name="macro_observations")
    op.drop_table("macro_observations")
    op.drop_index("ix_collection_runs_collector_started", table_name="collection_runs")
    op.drop_table("collection_runs")
    op.drop_index("ix_asset_universe_role_active", table_name="asset_universe")
    op.drop_table("asset_universe")
