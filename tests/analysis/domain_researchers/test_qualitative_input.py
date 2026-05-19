"""Tests for the per-sector qualitative input loader — story ALP-189 parts F + G.

Builds an in-memory SQLite DB with the relevant news / event tables, asserts
that ``load_sector_qualitative_input`` returns a typed ``SectorQualitativeInput``
that selects, ranks, and groups data per the design contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.qualitative_input import (
    EventEntry,
    HeadlineEntry,
    SectorQualitativeInput,
    load_sector_qualitative_input,
)
from alphamind.analysis.news_freshness import NewsEmptyReason
from alphamind.data_sources._common import HeadlineType
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str, sector: Sector) -> None:
    """Register a ticker in asset_universe and sector_classification."""
    asset_id = f"asset-{ticker.lower()}"
    session.add(
        AssetUniverse(
            asset_id=asset_id,
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=asset_id,
            alphamind_sector=sector.value,
            domain_researcher=sector.value,
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-01-01T00:00:00Z",
        )
    )


def _add_article(
    session: Session,
    *,
    article_id: str,
    headline: str,
    published_at: datetime,
    source_outlet: str | None = "Reuters",
    tier: str | None = "tier_1",
    topic_tags: str | None = None,
    tickers: tuple[str, ...] = (),
    ingested_at: datetime | None = None,
) -> None:
    session.add(
        NewsArticles(
            article_id=article_id,
            source="test",
            source_outlet=source_outlet,
            source_credibility_tier=tier,
            language="en",
            headline_text=headline,
            published_at=published_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ingested_at=(ingested_at or published_at)
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            topic_tags=topic_tags,
        )
    )
    session.flush()
    for ticker in tickers:
        session.add(NewsArticleTickers(article_id=article_id, ticker=ticker, is_primary=0))


def _add_event(
    session: Session,
    *,
    event_id: str,
    event_type: str,
    description: str,
    scheduled_at: datetime,
    sectors: str | None = None,
    ticker: str | None = None,
    ingested_at: datetime | None = None,
) -> None:
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type=event_type,
            ticker=ticker,
            scheduled_at=scheduled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=description,
            status="scheduled",
            source="test",
            ingested_at=(ingested_at or scheduled_at)
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            last_updated="2026-04-30T00:00:00Z",
            sectors=sectors,
        )
    )


# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


class TestEmptyDatabase:
    def test_empty_db_returns_empty_input(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)

        assert isinstance(result, SectorQualitativeInput)
        assert result.sector is Sector.TECH_SEMIS
        assert result.as_of == as_of
        assert result.lookback_window_hours == 24
        assert result.headlines == ()
        assert result.events == ()
        # Documented choice: empty DB → freshness is the lookback floor.
        assert result.data_freshness == as_of - timedelta(hours=24)

    def test_empty_db_classifies_no_rows_in_db(self, session: Session) -> None:
        """When no headlines exist anywhere in news_articles, the loader
        records ``NO_ROWS_IN_DB`` so the input bundle can surface the cause
        to the LLM agent (ALP-567)."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)

        assert result.headlines_empty_diagnosis is not None
        assert result.headlines_empty_diagnosis.reason is NewsEmptyReason.NO_ROWS_IN_DB

    def test_collector_inactive_classified(self, session: Session) -> None:
        """Stale ingestion before window → ``COLLECTOR_INACTIVE``."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        stale = as_of - timedelta(hours=30)  # before 24h lookback
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="old",
            headline="old",
            published_at=stale,
            ingested_at=stale,
            tickers=("AAPL",),
        )
        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)

        assert result.headlines == ()
        assert result.headlines_empty_diagnosis is not None
        assert result.headlines_empty_diagnosis.reason is NewsEmptyReason.COLLECTOR_INACTIVE

    def test_filter_only_empty_does_not_emit_diagnosis(self, session: Session) -> None:
        """Window has rows, but the per-sector filter rejects them → no diagnosis.

        The freshness diagnostic surfaces data-layer state, not per-sector
        filter outcomes; the LLM agent already knows it is looking at a
        sector-scoped view, so a vendor/collector reason line would mislead.
        """
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        # Row inside the 24h window but tagged for a different sector — won't pass
        # the tech_semis filter, so headlines is empty.
        _add_ticker(session, "JPM", Sector.FINANCIALS)
        _add_article(
            session,
            article_id="other-sector",
            headline="JPM update",
            published_at=as_of - timedelta(hours=2),
            ingested_at=as_of - timedelta(hours=2),
            tickers=("JPM",),
        )
        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)

        assert result.headlines == ()
        assert result.headlines_empty_diagnosis is None

    def test_populated_input_has_no_diagnosis(self, session: Session) -> None:
        """When headlines is non-empty, the diagnosis is None."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="ok",
            headline="AAPL note",
            published_at=as_of - timedelta(hours=1),
            ingested_at=as_of - timedelta(hours=1),
            tickers=("AAPL",),
        )
        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)

        assert result.headlines
        assert result.headlines_empty_diagnosis is None


# ---------------------------------------------------------------------------
# Headline selection
# ---------------------------------------------------------------------------


class TestHeadlineSelection:
    def test_outside_lookback_excluded(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="too-old",
            headline="Old news",
            published_at=as_of - timedelta(hours=48),
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert result.headlines == ()

    def test_null_topic_tags_does_not_crash(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="null-tags",
            headline="Ticker-only match",
            published_at=as_of - timedelta(hours=2),
            topic_tags=None,
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].tags == ()

    def test_malformed_topic_tags_does_not_crash(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="bad-json",
            headline="Bad json",
            published_at=as_of - timedelta(hours=2),
            topic_tags="not valid json",
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].tags == ()

    def test_sector_ticker_match_includes_regardless_of_tags(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="ticker-match",
            headline="Ticker-only match",
            published_at=as_of - timedelta(hours=2),
            topic_tags=json.dumps([HeadlineType.SECTOR_ROTATION.value]),
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].headline == "Ticker-only match"
        assert "AAPL" in result.headlines[0].tickers

    def test_sector_relevant_tag_match_includes_regardless_of_tickers(
        self, session: Session
    ) -> None:
        """An article with no sector ticker but a sector-relevant tag is included."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="tag-only",
            headline="Tag-only match",
            published_at=as_of - timedelta(hours=2),
            topic_tags=json.dumps([HeadlineType.SUPPLY_CHAIN.value]),
            tickers=(),  # no ticker join
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].headline == "Tag-only match"
        assert HeadlineType.SUPPLY_CHAIN in result.headlines[0].tags

    def test_vendor_raw_tags_parse_to_empty(self, session: Session) -> None:
        """Historical rows with vendor-raw (non-canonical) topic_tags resolve to empty tag tuple."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="legacy-vendor",
            headline="Legacy vendor tags",
            published_at=as_of - timedelta(hours=2),
            topic_tags=json.dumps(["earnings", "geopolitics"]),  # vendor names, not canonical
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].tags == ()

    def test_null_credibility_tier_defaults_to_tier_3(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_article(
            session,
            article_id="no-tier",
            headline="No tier",
            published_at=as_of - timedelta(hours=2),
            tier=None,
            topic_tags=json.dumps([HeadlineType.SUPPLY_CHAIN.value]),
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 1
        assert result.headlines[0].tier == "tier_3"

    def test_max_headlines_truncates(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        # 5 articles, all qualifying — request only 2.
        for i in range(5):
            _add_article(
                session,
                article_id=f"art-{i}",
                headline=f"Headline {i}",
                published_at=as_of - timedelta(hours=i + 1),
                tickers=("AAPL",),
                topic_tags=json.dumps([HeadlineType.SUPPLY_CHAIN.value]),
            )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of, max_headlines=2)
        assert len(result.headlines) == 2

    def test_composite_score_ranking(self, session: Session) -> None:
        """Tier x recency x ticker-mention boost: higher tier + recency + ticker outranks."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)

        # Old, tier_3, no ticker: lowest score.
        _add_article(
            session,
            article_id="low",
            headline="Low score",
            published_at=as_of - timedelta(hours=20),
            tier="tier_3",
            topic_tags=json.dumps([HeadlineType.SUPPLY_CHAIN.value]),
            tickers=(),
        )
        # Fresh, tier_1, with ticker: highest score.
        _add_article(
            session,
            article_id="high",
            headline="High score",
            published_at=as_of - timedelta(hours=1),
            tier="tier_1",
            topic_tags=json.dumps([HeadlineType.SUPPLY_CHAIN.value]),
            tickers=("AAPL",),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.headlines) == 2
        assert result.headlines[0].headline == "High score"
        assert result.headlines[1].headline == "Low score"


# ---------------------------------------------------------------------------
# Event selection
# ---------------------------------------------------------------------------


class TestEventSelection:
    def test_event_outside_72h_excluded(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_event(
            session,
            event_id="ev-far",
            event_type="macro",
            description="Far future",
            scheduled_at=as_of + timedelta(hours=80),
            sectors=None,
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert result.events == ()

    def test_null_sectors_treated_as_cross_sector(self, session: Session) -> None:
        """Events with sectors IS NULL apply to every sector."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_event(
            session,
            event_id="fomc",
            event_type="macro",
            description="FOMC rate decision",
            scheduled_at=as_of + timedelta(hours=24),
            sectors=None,
        )
        session.commit()

        for sector in (Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY):
            result = load_sector_qualitative_input(session, sector, as_of)
            assert len(result.events) == 1
            assert result.events[0].event_id == "fomc"
            assert result.events[0].sectors == frozenset()

    def test_sector_membership_includes(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_event(
            session,
            event_id="ev-1",
            event_type="macro",
            description="Tech-tagged event",
            scheduled_at=as_of + timedelta(hours=24),
            sectors="tech_semis,financials",
        )
        session.commit()

        tech = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        fin = load_sector_qualitative_input(session, Sector.FINANCIALS, as_of)
        energy = load_sector_qualitative_input(session, Sector.ENERGY, as_of)

        assert len(tech.events) == 1
        assert len(fin.events) == 1
        assert energy.events == ()
        assert tech.events[0].sectors == frozenset({Sector.TECH_SEMIS, Sector.FINANCIALS})

    def test_events_grouped_by_event_id_emit_one_per_id(self, session: Session) -> None:
        """The schema PKs ``event_id``: each id surfaces once with its row's ticker."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_ticker(session, "NVDA", Sector.TECH_SEMIS)
        _add_event(
            session,
            event_id="ev-aapl",
            event_type="earnings",
            description="AAPL earnings",
            scheduled_at=as_of + timedelta(hours=24),
            sectors="tech_semis",
            ticker=Symbol("AAPL"),
        )
        _add_event(
            session,
            event_id="ev-nvda",
            event_type="earnings",
            description="NVDA earnings",
            scheduled_at=as_of + timedelta(hours=36),
            sectors="tech_semis",
            ticker=Symbol("NVDA"),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.events) == 2
        ids = {e.event_id for e in result.events}
        assert ids == {"ev-aapl", "ev-nvda"}
        # Each entry carries its row's ticker.
        by_id = {e.event_id: e for e in result.events}
        assert by_id["ev-aapl"].tickers == ("AAPL",)
        assert by_id["ev-nvda"].tickers == ("NVDA",)

    def test_earnings_event_renders_consensus(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_event(
            session,
            event_id="earn-aapl-q1",
            event_type="earnings",
            description="AAPL Q1 earnings",
            scheduled_at=as_of + timedelta(hours=24),
            sectors="tech_semis",
            ticker=Symbol("AAPL"),
        )
        session.flush()
        session.add(
            EarningsEventDetails(
                event_id="earn-aapl-q1",
                ticker=Symbol("AAPL"),
                fiscal_period="Q1",
                fiscal_year=2026,
                eps_consensus=1.23,
                revenue_consensus_usd=50_000_000_000.0,
                source="test",
            )
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.events) == 1
        ev = result.events[0]
        assert ev.consensus is not None
        assert "1.23" in ev.consensus
        assert "50.0B" in ev.consensus

    def test_non_earnings_event_consensus_is_none(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_event(
            session,
            event_id="cpi",
            event_type="macro",
            description="CPI release",
            scheduled_at=as_of + timedelta(hours=24),
            sectors=None,
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.events) == 1
        assert result.events[0].consensus is None

    def test_metadata_less_other_events_filtered(self, session: Session) -> None:
        """type=other events without a ticker are dropped; type=other with a ticker survives."""
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)
        _add_event(
            session,
            event_id="ev-noise",
            event_type="other",
            description="Aperture AC",
            scheduled_at=as_of + timedelta(hours=24),
            sectors=None,
        )
        _add_event(
            session,
            event_id="ev-other-ticker",
            event_type="other",
            description="AAPL analyst day",
            scheduled_at=as_of + timedelta(hours=24),
            sectors="tech_semis",
            ticker=Symbol("AAPL"),
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        assert len(result.events) == 1
        assert result.events[0].event_id == "ev-other-ticker"


# ---------------------------------------------------------------------------
# data_freshness
# ---------------------------------------------------------------------------


class TestDataFreshness:
    def test_data_freshness_reflects_latest_ingestion(self, session: Session) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        _add_ticker(session, "AAPL", Sector.TECH_SEMIS)

        article_ingested = as_of - timedelta(hours=2)
        event_ingested = as_of - timedelta(minutes=30)
        _add_article(
            session,
            article_id="art",
            headline="An article",
            published_at=as_of - timedelta(hours=3),
            ingested_at=article_ingested,
            tickers=("AAPL",),
        )
        _add_event(
            session,
            event_id="ev",
            event_type="macro",
            description="An event",
            scheduled_at=as_of + timedelta(hours=10),
            sectors=None,
            ingested_at=event_ingested,
        )
        session.commit()

        result = load_sector_qualitative_input(session, Sector.TECH_SEMIS, as_of)
        # event was ingested most recently → that's the freshness.
        assert result.data_freshness == event_ingested.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Pydantic record shapes
# ---------------------------------------------------------------------------


class TestRecordShapes:
    def test_sector_qualitative_input_is_frozen(self) -> None:
        as_of = datetime(2026, 4, 30, 12, tzinfo=UTC)
        rec = SectorQualitativeInput(
            sector=Sector.TECH_SEMIS,
            as_of=as_of,
            lookback_window_hours=24,
            headlines=(),
            events=(),
            data_freshness=as_of,
        )
        with pytest.raises(Exception):  # noqa: B017
            rec.sector = Sector.FINANCIALS  # type: ignore[misc]

    def test_headline_entry_tags_are_typed(self) -> None:
        rec = HeadlineEntry(
            headline="hi",
            source_outlet="Reuters",
            tier="tier_1",
            published_at=datetime(2026, 4, 30, 12, tzinfo=UTC),
            tickers=("AAPL",),
            tags=(HeadlineType.MACRO_DATA,),
        )
        assert rec.tags == (HeadlineType.MACRO_DATA,)

    def test_event_entry_sectors_are_typed(self) -> None:
        rec = EventEntry(
            event_id="x",
            event_name="X",
            event_time=datetime(2026, 4, 30, 12, tzinfo=UTC),
            sectors=frozenset({Sector.TECH_SEMIS}),
            tickers=("AAPL",),
            consensus=None,
        )
        assert rec.sectors == frozenset({Sector.TECH_SEMIS})
