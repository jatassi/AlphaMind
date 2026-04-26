"""
Persistence layer tests — story 03b.

Each test targets one acceptance criterion.  Round-trip and constraint tests
use an in-memory SQLite database.  Pragma tests use a temporary file-backed
database (WAL mode is not available on in-memory databases).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CollectionRuns,
    CorporateActions,
    EarningsEventDetails,
    EtfMembership,
    EventCalendar,
    MacroObservations,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
    TickerChangeHistory,
    TreasuryAuctions,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with all pragmas and tables."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def file_engine(tmp_path):
    """File-backed SQLite engine — needed for WAL-mode pragma tests."""
    db_path = str(tmp_path / "test.db")
    eng = make_engine(db_path)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    """Session bound to the in-memory engine."""
    session_factory = make_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_universe(session):
    """Insert a minimal AssetUniverse row so FK constraints can be satisfied."""
    row = AssetUniverse(
        asset_id="asset-aapl",
        ticker="AAPL",
        full_name="Apple Inc.",
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2020-01-01",
        last_updated="2026-04-26T00:00:00Z",
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# AC: PRAGMA settings
# ---------------------------------------------------------------------------


class TestPragmas:
    def test_journal_mode_is_wal(self, file_engine):
        """WAL mode requires a file-backed database."""
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert result == "wal"

    def test_foreign_keys_on(self, file_engine):
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1

    def test_busy_timeout(self, file_engine):
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert result == 5000


# ---------------------------------------------------------------------------
# AC: Round-trip insert + select for every table
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_asset_universe(self, session, seeded_universe):
        fetched = session.get(AssetUniverse, "asset-aapl")
        assert fetched is not None
        assert fetched.ticker == "AAPL"
        assert fetched.asset_class == "equity"

    def test_sector_classification(self, session, seeded_universe):
        row = SectorClassification(
            ticker="AAPL",
            asset_id="asset-aapl",
            alphamind_sector="tech",
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="polygon",
            last_updated="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(SectorClassification, "AAPL")
        assert fetched is not None
        assert fetched.alphamind_sector == "tech"

    def test_etf_membership(self, session, seeded_universe):
        row = EtfMembership(
            ticker="AAPL",
            etf_ticker="XLK",
            etf_name="Tech Select Sector SPDR",
            weight_pct=22.5,
            weight_as_of="2026-04-01",
            is_top_10=1,
        )
        session.add(row)
        session.commit()
        fetched = session.get(EtfMembership, ("AAPL", "XLK", "2026-04-01"))
        assert fetched is not None
        assert fetched.weight_pct == 22.5

    def test_ticker_change_history(self, session, seeded_universe):
        row = TickerChangeHistory(
            asset_id="asset-aapl",
            previous_ticker="AAPL",
            new_ticker="AAPL2",
            effective_date="2026-01-01",
            reason="rebrand",
        )
        session.add(row)
        session.commit()
        fetched = session.get(TickerChangeHistory, ("asset-aapl", "2026-01-01"))
        assert fetched is not None
        assert fetched.reason == "rebrand"

    def test_ohlcv_bars(self, session, seeded_universe):
        row = OhlcvBars(
            ticker="AAPL",
            timeframe="1d",
            period_start="2026-04-25T09:30:00Z",
            period_end="2026-04-25T16:00:00Z",
            session="regular",
            adj_open=170.0,
            adj_high=175.0,
            adj_low=169.0,
            adj_close=174.0,
            adj_volume=50000000,
            unadj_open=170.0,
            unadj_high=175.0,
            unadj_low=169.0,
            unadj_close=174.0,
            unadj_volume=50000000,
            source="polygon",
            ingested_at="2026-04-25T20:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(OhlcvBars, ("AAPL", "1d", "2026-04-25T09:30:00Z"))
        assert fetched is not None
        assert fetched.adj_close == 174.0

    def test_corporate_actions(self, session, seeded_universe):
        row = CorporateActions(
            action_id="act-001",
            ticker="AAPL",
            action_type="split",
            ex_date="2026-03-01",
            source="polygon",
            ingested_at="2026-04-01T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(CorporateActions, "act-001")
        assert fetched is not None
        assert fetched.action_type == "split"

    def test_options_contracts(self, session, seeded_universe):
        row = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker="AAPL",
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(row)
        session.commit()
        fetched = session.get(OptionsContracts, "O:AAPL250117C00200000")
        assert fetched is not None
        assert fetched.strike_price == 200.0

    def test_options_contract_snapshots(self, session, seeded_universe):
        # Requires parent options contract
        contract = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker="AAPL",
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(contract)
        session.flush()
        row = OptionsContractSnapshots(
            snapshot_ts="2026-04-26T15:00:00Z",
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker="AAPL",
            source="polygon",
            ingested_at="2026-04-26T15:01:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            OptionsContractSnapshots,
            ("2026-04-26T15:00:00Z", "O:AAPL250117C00200000"),
        )
        assert fetched is not None
        assert fetched.underlying_ticker == "AAPL"

    def test_macro_observations(self, session):
        row = MacroObservations(
            source="fred",
            series_id="DGS10",
            observation_date="2026-04-25",
            revision_number=0,
            value=4.5,
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(MacroObservations, ("fred", "DGS10", "2026-04-25", 0))
        assert fetched is not None
        assert fetched.value == 4.5

    def test_treasury_auctions(self, session):
        row = TreasuryAuctions(
            auction_id="2026-03-15_10Y",
            tenor="10Y",
            auction_date="2026-03-15",
            source="treasury",
            ingested_at="2026-03-15T20:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(TreasuryAuctions, "2026-03-15_10Y")
        assert fetched is not None
        assert fetched.tenor == "10Y"

    def test_event_calendar(self, session):
        row = EventCalendar(
            event_id="evt-001",
            event_type="fomc",
            scheduled_at="2026-05-01T14:00:00Z",
            status="scheduled",
            source="manual",
            ingested_at="2026-04-01T00:00:00Z",
            last_updated="2026-04-01T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(EventCalendar, "evt-001")
        assert fetched is not None
        assert fetched.event_type == "fomc"

    def test_earnings_event_details(self, session):
        # Requires parent event_calendar row
        parent = EventCalendar(
            event_id="evt-earn-001",
            event_type="earnings",
            scheduled_at="2026-04-30T16:30:00Z",
            status="scheduled",
            source="finnhub",
            ingested_at="2026-04-01T00:00:00Z",
            last_updated="2026-04-01T00:00:00Z",
        )
        session.add(parent)
        session.flush()
        detail = EarningsEventDetails(
            event_id="evt-earn-001",
            ticker="AAPL",
            fiscal_period="Q1",
            fiscal_year=2026,
            source="finnhub",
        )
        session.add(detail)
        session.commit()
        fetched = session.get(EarningsEventDetails, "evt-earn-001")
        assert fetched is not None
        assert fetched.fiscal_period == "Q1"

    def test_news_articles(self, session):
        row = NewsArticles(
            article_id="art-001",
            source="finnhub",
            language="en",
            headline_text="AAPL beats estimates",
            published_at="2026-04-26T12:00:00Z",
            ingested_at="2026-04-26T12:05:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(NewsArticles, "art-001")
        assert fetched is not None
        assert fetched.headline_text == "AAPL beats estimates"

    def test_news_article_tickers(self, session, seeded_universe):
        article = NewsArticles(
            article_id="art-002",
            source="marketaux",
            language="en",
            headline_text="Markets rally",
            published_at="2026-04-26T10:00:00Z",
            ingested_at="2026-04-26T10:05:00Z",
        )
        session.add(article)
        session.flush()
        link = NewsArticleTickers(
            article_id="art-002",
            ticker="AAPL",
            is_primary=1,
        )
        session.add(link)
        session.commit()
        fetched = session.get(NewsArticleTickers, ("art-002", "AAPL"))
        assert fetched is not None
        assert fetched.is_primary == 1

    def test_prediction_market_contracts(self, session):
        row = PredictionMarketContracts(
            contract_id="pm-001",
            platform="polymarket",
            description="Fed cuts in May?",
            category="monetary_policy",
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(PredictionMarketContracts, "pm-001")
        assert fetched is not None
        assert fetched.platform == "polymarket"

    def test_prediction_market_snapshots(self, session):
        contract = PredictionMarketContracts(
            contract_id="pm-002",
            platform="kalshi",
            description="Rate cut Q2?",
            category="monetary_policy",
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
        session.add(contract)
        session.flush()
        snap = PredictionMarketSnapshots(
            contract_id="pm-002",
            snapshot_ts="2026-04-26T14:00:00Z",
            yes_probability=0.65,
            ingested_at="2026-04-26T14:01:00Z",
        )
        session.add(snap)
        session.commit()
        fetched = session.get(PredictionMarketSnapshots, ("pm-002", "2026-04-26T14:00:00Z"))
        assert fetched is not None
        assert fetched.yes_probability == 0.65

    def test_collection_runs(self, session):
        row = CollectionRuns(
            run_id="run-001",
            collector="polygon.equity",
            started_at="2026-04-26T09:30:00Z",
            status="success",
        )
        session.add(row)
        session.commit()
        fetched = session.get(CollectionRuns, "run-001")
        assert fetched is not None
        assert fetched.collector == "polygon.equity"


# ---------------------------------------------------------------------------
# AC: Composite-key uniqueness
# ---------------------------------------------------------------------------


class TestCompositeKeyUniqueness:
    def test_ohlcv_bars_duplicate_raises(self, session, seeded_universe):
        def make_bar():
            return OhlcvBars(
                ticker="AAPL",
                timeframe="1d",
                period_start="2026-04-25T09:30:00Z",
                period_end="2026-04-25T16:00:00Z",
                session="regular",
                adj_open=170.0,
                adj_high=175.0,
                adj_low=169.0,
                adj_close=174.0,
                adj_volume=50000000,
                unadj_open=170.0,
                unadj_high=175.0,
                unadj_low=169.0,
                unadj_close=174.0,
                unadj_volume=50000000,
                source="polygon",
                ingested_at="2026-04-25T20:00:00Z",
            )

        session.add(make_bar())
        session.commit()
        session.add(make_bar())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_macro_observations_duplicate_raises(self, session):
        def make_obs():
            return MacroObservations(
                source="fred",
                series_id="DGS10",
                observation_date="2026-04-25",
                revision_number=0,
                value=4.5,
                ingested_at="2026-04-26T00:00:00Z",
            )

        session.add(make_obs())
        session.commit()
        session.add(make_obs())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_options_contract_snapshots_duplicate_raises(self, session, seeded_universe):
        contract = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker="AAPL",
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(contract)
        session.commit()

        def make_snap():
            return OptionsContractSnapshots(
                snapshot_ts="2026-04-26T15:00:00Z",
                contract_ticker="O:AAPL250117C00200000",
                underlying_ticker="AAPL",
                source="polygon",
                ingested_at="2026-04-26T15:01:00Z",
            )

        session.add(make_snap())
        session.commit()
        session.add(make_snap())
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# AC: Foreign-key constraint enforcement
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    def test_sector_classification_fk_ticker(self, session):
        """ticker must exist in asset_universe."""
        row = SectorClassification(
            ticker="NONEXISTENT",
            asset_id="asset-xxx",
            alphamind_sector="tech",
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="polygon",
            last_updated="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_ohlcv_bars_fk_ticker(self, session):
        """ticker must exist in asset_universe."""
        row = OhlcvBars(
            ticker="NONEXISTENT",
            timeframe="1d",
            period_start="2026-04-25T09:30:00Z",
            period_end="2026-04-25T16:00:00Z",
            session="regular",
            adj_open=170.0,
            adj_high=175.0,
            adj_low=169.0,
            adj_close=174.0,
            adj_volume=50000000,
            unadj_open=170.0,
            unadj_high=175.0,
            unadj_low=169.0,
            unadj_close=174.0,
            unadj_volume=50000000,
            source="polygon",
            ingested_at="2026-04-25T20:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_news_article_tickers_cascade_delete(self, engine, seeded_universe):
        """
        Deleting an article via raw SQL should cascade to news_article_tickers
        (ON DELETE CASCADE declared in FK).  Uses a fresh session so the ORM
        identity map doesn't shadow the DB-level delete.
        """
        session_factory = make_session_factory(engine)
        with session_factory() as sess:
            # Insert parent universe row for FK satisfaction
            sess.merge(seeded_universe)
            article = NewsArticles(
                article_id="art-cascade",
                source="finnhub",
                language="en",
                headline_text="Test cascade",
                published_at="2026-04-26T12:00:00Z",
                ingested_at="2026-04-26T12:05:00Z",
            )
            sess.add(article)
            sess.flush()
            link = NewsArticleTickers(
                article_id="art-cascade",
                ticker="AAPL",
                is_primary=1,
            )
            sess.add(link)
            sess.commit()

            # Delete via raw SQL to exercise DB-level cascade
            sess.execute(text("DELETE FROM news_articles WHERE article_id = 'art-cascade'"))
            sess.commit()

        # Open a fresh session — no identity map interference
        with session_factory() as sess2:
            fetched = sess2.get(NewsArticleTickers, ("art-cascade", "AAPL"))
            assert fetched is None
