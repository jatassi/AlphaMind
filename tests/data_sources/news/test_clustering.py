"""Tests for news headline clustering — story ALP-245.

Covers the deterministic two-pass clustering pipeline that consumes
``news_articles`` rows and writes ``news_article_clusters`` rows plus the
``news_articles.cross_ticker_cluster_id`` linkage. The algorithmic
specification is in ``docs/design/03-analysis-layer/qualitative-research.md``
§ Headline clustering.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    NewsArticleClusters,
    NewsArticles,
    NewsArticleTickers,
)
from alphamind.persistence.session import make_engine, make_session_factory


def _article(
    article_id: str,
    headline_text: str,
    published_at: str,
    *,
    source: str = "rss",
    source_outlet: str | None = None,
    source_credibility_tier: str | None = "tier_2",
    topic_tags: str | None = None,
) -> NewsArticles:
    """Build a ``NewsArticles`` row with all required fields populated.

    Tests that exercise the clustering primitives operate over in-memory
    ``NewsArticles`` instances; the persistence-side caller is the only
    code that round-trips through SQL.
    """
    return NewsArticles(
        article_id=article_id,
        source=source,
        source_outlet=source_outlet,
        source_credibility_tier=source_credibility_tier,
        url=f"https://example.com/{article_id}",
        language="en",
        headline_text=headline_text,
        body_path=None,
        published_at=published_at,
        ingested_at=published_at,
        topic_tags=topic_tags,
    )


@pytest.fixture()
def db_factory() -> Iterator[sessionmaker[Session]]:
    """In-memory SQLite session factory with all tables created.

    Seeds a small ``asset_universe`` so the FK on
    ``news_article_tickers.ticker`` can be satisfied across tests.
    """
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = make_session_factory(engine)
    with factory() as sess:
        for ticker in ("NVDA", "AMD", "AAPL"):
            sess.add(
                AssetUniverse(
                    asset_id=f"au-{ticker}",
                    ticker=ticker,
                    full_name=ticker,
                    asset_class="equity",
                    asset_role="universe",
                    exchange="NASDAQ",
                    is_active=1,
                    added_date="2020-01-01",
                    last_updated="2020-01-01T00:00:00Z",
                )
            )
        sess.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def _persist_articles(
    factory: sessionmaker[Session],
    articles: list[NewsArticles],
    *,
    ticker_index: dict[str, frozenset[str]] | None = None,
) -> None:
    """Persist articles + per-ticker join rows for a refresh-test fixture."""
    with factory() as sess:
        sess.add_all(articles)
        sess.flush()
        for article in articles:
            tickers = (
                ticker_index.get(article.article_id, frozenset())
                if ticker_index is not None
                else frozenset()
            )
            primary_assigned = False
            for ticker in sorted(tickers):
                sess.add(
                    NewsArticleTickers(
                        article_id=article.article_id,
                        ticker=ticker,
                        is_primary=0 if primary_assigned else 1,
                    )
                )
                primary_assigned = True
        sess.commit()


class TestClusteringConstants:
    """Every threshold encoded as a named module constant per § Headline clustering."""

    def test_stage_1_levenshtein_ratio_min_value(self) -> None:
        from alphamind.data_sources.news import clustering

        assert clustering.STAGE_1_LEVENSHTEIN_RATIO_MIN == 0.90

    def test_stage_1_time_window_minutes_value(self) -> None:
        from alphamind.data_sources.news import clustering

        assert clustering.STAGE_1_TIME_WINDOW_MINUTES == 30

    def test_stage_2_simhash_jaccard_min_value(self) -> None:
        from alphamind.data_sources.news import clustering

        assert clustering.STAGE_2_SIMHASH_JACCARD_MIN == 0.60

    def test_stage_2_time_window_hours_value(self) -> None:
        from alphamind.data_sources.news import clustering

        assert clustering.STAGE_2_TIME_WINDOW_HOURS == 6

    def test_cluster_seal_hours_value(self) -> None:
        from alphamind.data_sources.news import clustering

        assert clustering.CLUSTER_SEAL_HOURS == 24


class TestStage1Canonicalization:
    """Stage 1 — source canonicalization collapses syndicated wire copies."""

    def test_identical_headlines_with_outlet_prefixes_collapse(self) -> None:
        """``BREAKING:``/``UPDATE:`` lead tokens and outlet differences merge.

        Per § Headline clustering, normalization strips the outlet-specific
        lead tokens before the Levenshtein comparison. Three headlines
        republishing the same Reuters wire item — one with ``BREAKING:``,
        one with ``UPDATE:``, one bare — collapse into a single source-
        canonical group keyed on the earliest ``article_id``.
        """
        from alphamind.data_sources.news.clustering import canonicalize_syndication

        articles = [
            _article(
                "art-002",
                "BREAKING: Fed leaves rates unchanged at September meeting",
                "2026-05-01T14:00:00Z",
                source_outlet="Yahoo",
            ),
            _article(
                "art-001",
                "Fed leaves rates unchanged at September meeting",
                "2026-05-01T13:55:00Z",
                source_outlet="Reuters",
            ),
            _article(
                "art-003",
                "UPDATE: Fed leaves rates unchanged at September meeting",
                "2026-05-01T14:10:00Z",
                source_outlet="MarketWatch",
            ),
        ]

        mapping = canonicalize_syndication(articles)

        # All three map to the earliest member's article_id (art-001).
        assert mapping == {
            "art-001": "art-001",
            "art-002": "art-001",
            "art-003": "art-001",
        }

    def test_headlines_outside_time_window_do_not_merge(self) -> None:
        """Headlines published > 30 minutes apart never collapse.

        Per § Headline clustering: "Compare every pair of headlines published
        within 30 minutes of each other." Two headlines 31 minutes apart
        with otherwise-identical text remain distinct source-canonicals,
        even though their Levenshtein ratio is 1.0.
        """
        from alphamind.data_sources.news.clustering import canonicalize_syndication

        articles = [
            _article(
                "art-001",
                "Fed leaves rates unchanged at September meeting",
                "2026-05-01T13:00:00Z",
                source_outlet="Reuters",
            ),
            _article(
                "art-002",
                "Fed leaves rates unchanged at September meeting",
                "2026-05-01T13:31:00Z",
                source_outlet="CNBC",
            ),
        ]

        mapping = canonicalize_syndication(articles)

        # Each article is its own source-canonical.
        assert mapping == {"art-001": "art-001", "art-002": "art-002"}


class TestStage2EventClustering:
    """Stage 2 — event clustering groups source-canonical headlines."""

    def test_shared_ticker_within_window_clusters(self) -> None:
        """Two same-event NVDA headlines hours apart cluster on shared ticker.

        Per § Headline clustering Stage 2: at least one shared ticker plus
        SimHash Jaccard ≥ 0.60 within 6 hours produces a single cluster
        identified by the earliest member's ``article_id``.
        """
        from alphamind.data_sources.news.clustering import cluster_events

        articles = [
            _article(
                "nvda-001",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T12:00:00Z",
                topic_tags="earnings_related",
            ),
            _article(
                "nvda-002",
                "Nvidia reports record Q3 revenue topping analyst estimates",
                "2026-05-01T16:00:00Z",
                topic_tags="earnings_related",
            ),
        ]
        ticker_index = {
            "nvda-001": frozenset({"NVDA"}),
            "nvda-002": frozenset({"NVDA"}),
        }

        clusters = cluster_events(articles, ticker_index)

        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster.cluster_id == "nvda-001"
        assert cluster.headline_count == 2
        assert "NVDA" in cluster.tickers_involved

    def test_no_shared_ticker_blocks_clustering(self) -> None:
        """High SimHash similarity alone is not enough — Stage 2 requires the
        shared-ticker predicate.

        Two near-identical earnings-beat headlines about different companies
        share zero tickers, so Stage 2 leaves them in separate clusters
        regardless of how high their SimHash Jaccard climbs.
        """
        from alphamind.data_sources.news.clustering import cluster_events

        articles = [
            _article(
                "nvda-001",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T12:00:00Z",
                topic_tags="earnings_related",
            ),
            _article(
                "amd-001",
                "AMD reports record Q3 revenue topping analyst estimates",
                "2026-05-01T16:00:00Z",
                topic_tags="earnings_related",
            ),
        ]
        ticker_index = {
            "nvda-001": frozenset({"NVDA"}),
            "amd-001": frozenset({"AMD"}),
        }

        clusters = cluster_events(articles, ticker_index)

        # Two single-member clusters, never merged.
        assert len(clusters) == 2
        cluster_ids = {c.cluster_id for c in clusters}
        assert cluster_ids == {"nvda-001", "amd-001"}
        for cluster in clusters:
            assert cluster.headline_count == 1

    def test_tickerless_macro_with_shared_topic_tag_clusters(self) -> None:
        """Ticker-less macro headlines cluster when their topic_tags overlap.

        Per § Headline clustering Stage 2: "Ticker-less headlines cluster
        only with other ticker-less headlines whose ``topic_tags`` intersect
        by at least one tag."
        """
        from alphamind.data_sources.news.clustering import cluster_events

        articles = [
            _article(
                "macro-001",
                "Federal Reserve holds rates steady at September meeting",
                "2026-05-01T12:00:00Z",
                topic_tags="macro_data",
            ),
            _article(
                "macro-002",
                "Federal Reserve holds rates steady at September policy meeting",
                "2026-05-01T13:00:00Z",
                topic_tags="macro_data",
            ),
        ]
        ticker_index: dict[str, frozenset[str]] = {
            "macro-001": frozenset(),
            "macro-002": frozenset(),
        }

        clusters = cluster_events(articles, ticker_index)

        assert len(clusters) == 1
        assert clusters[0].cluster_id == "macro-001"
        assert clusters[0].headline_count == 2

    def test_tickerless_without_topic_tag_overlap_does_not_cluster(self) -> None:
        """Ticker-less headlines without topic-tag overlap remain separate."""
        from alphamind.data_sources.news.clustering import cluster_events

        articles = [
            _article(
                "macro-001",
                "Federal Reserve holds rates steady at September meeting",
                "2026-05-01T12:00:00Z",
                topic_tags="macro_data",
            ),
            _article(
                "macro-002",
                "Federal Reserve holds rates steady at September policy meeting",
                "2026-05-01T13:00:00Z",
                topic_tags="geopolitical",
            ),
        ]
        ticker_index: dict[str, frozenset[str]] = {
            "macro-001": frozenset(),
            "macro-002": frozenset(),
        }

        clusters = cluster_events(articles, ticker_index)

        assert len(clusters) == 2
        for cluster in clusters:
            assert cluster.headline_count == 1


class TestRefreshNewsClusters:
    """``refresh_news_clusters`` is the persistence-layer top-level callable."""

    def test_writes_clusters_and_stamps_cross_ticker_id(
        self, db_factory: sessionmaker[Session]
    ) -> None:
        """End-to-end: a single refresh writes cluster rows and stamps members."""
        from alphamind.data_sources.news.clustering import refresh_news_clusters

        articles = [
            _article(
                "nvda-001",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T12:00:00Z",
                source_outlet="Reuters",
                source_credibility_tier="tier_1",
                topic_tags="earnings_related",
            ),
            _article(
                "nvda-002",
                "Nvidia reports record Q3 revenue topping analyst estimates",
                "2026-05-01T16:00:00Z",
                source_outlet="CNBC",
                source_credibility_tier="tier_2",
                topic_tags="earnings_related",
            ),
        ]
        _persist_articles(
            db_factory,
            articles,
            ticker_index={
                "nvda-001": frozenset({"NVDA"}),
                "nvda-002": frozenset({"NVDA"}),
            },
        )

        as_of = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        with db_factory() as sess:
            result = refresh_news_clusters(sess, as_of=as_of)

        assert result.articles_processed == 2
        assert result.clusters_written == 1
        assert result.articles_unclustered == 0

        with db_factory() as sess:
            clusters = sess.query(NewsArticleClusters).all()
            assert len(clusters) == 1
            cluster = clusters[0]
            assert cluster.cluster_id == "nvda-001"
            assert cluster.headline_count == 2
            assert "NVDA" in cluster.tickers_involved
            stamps = sess.execute(
                text(
                    "SELECT article_id, cross_ticker_cluster_id "
                    "FROM news_articles ORDER BY article_id"
                )
            ).all()
            assert {row[0]: row[1] for row in stamps} == {
                "nvda-001": "nvda-001",
                "nvda-002": "nvda-001",
            }

    def test_idempotent_double_refresh(self, db_factory: sessionmaker[Session]) -> None:
        """Running the refresh twice on the same input does not duplicate or churn rows."""
        from alphamind.data_sources.news.clustering import refresh_news_clusters

        articles = [
            _article(
                "nvda-001",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T12:00:00Z",
                source_outlet="Reuters",
                source_credibility_tier="tier_1",
                topic_tags="earnings_related",
            ),
            _article(
                "nvda-002",
                "Nvidia reports record Q3 revenue topping analyst estimates",
                "2026-05-01T16:00:00Z",
                source_outlet="CNBC",
                source_credibility_tier="tier_2",
                topic_tags="earnings_related",
            ),
        ]
        _persist_articles(
            db_factory,
            articles,
            ticker_index={
                "nvda-001": frozenset({"NVDA"}),
                "nvda-002": frozenset({"NVDA"}),
            },
        )

        as_of = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        with db_factory() as sess:
            refresh_news_clusters(sess, as_of=as_of)
        with db_factory() as sess:
            refresh_news_clusters(sess, as_of=as_of)

        with db_factory() as sess:
            cluster_rows = sess.query(NewsArticleClusters).all()
            assert len(cluster_rows) == 1
            assert cluster_rows[0].cluster_id == "nvda-001"
            stamps = sess.execute(
                text(
                    "SELECT article_id, cross_ticker_cluster_id "
                    "FROM news_articles ORDER BY article_id"
                )
            ).all()
            assert {row[0]: row[1] for row in stamps} == {
                "nvda-001": "nvda-001",
                "nvda-002": "nvda-001",
            }

    def test_cluster_id_is_stable_across_refreshes(self, db_factory: sessionmaker[Session]) -> None:
        """The cluster ID stays pinned to the earliest member's article_id.

        Inserting a same-event headline whose ``article_id`` sorts ahead of
        the earliest existing member must not flip the cluster id — the id
        is anchored on the earliest *publication time*, not lexical id.
        """
        from alphamind.data_sources.news.clustering import refresh_news_clusters

        first_pass = [
            _article(
                "z-nvda-001",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T12:00:00Z",
                source_outlet="Reuters",
                source_credibility_tier="tier_1",
                topic_tags="earnings_related",
            ),
            _article(
                "z-nvda-002",
                "Nvidia reports record Q3 revenue topping analyst estimates",
                "2026-05-01T13:00:00Z",
                source_outlet="CNBC",
                source_credibility_tier="tier_2",
                topic_tags="earnings_related",
            ),
        ]
        _persist_articles(
            db_factory,
            first_pass,
            ticker_index={
                "z-nvda-001": frozenset({"NVDA"}),
                "z-nvda-002": frozenset({"NVDA"}),
            },
        )

        as_of = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        with db_factory() as sess:
            refresh_news_clusters(sess, as_of=as_of)

        with db_factory() as sess:
            initial_cluster_id = sess.execute(select(NewsArticleClusters.cluster_id)).scalar_one()
            assert initial_cluster_id == "z-nvda-001"

        # A later headline whose article_id sorts ahead of "z-nvda-001"
        # joins the cluster but cannot displace the cluster id.
        with db_factory() as sess:
            sess.add(
                _article(
                    "a-nvda-003",
                    "Nvidia reports record Q3 revenue topping analyst estimates",
                    "2026-05-01T14:30:00Z",
                    source_outlet="WSJ",
                    source_credibility_tier="tier_2",
                    topic_tags="earnings_related",
                )
            )
            sess.flush()
            sess.add(
                NewsArticleTickers(
                    article_id="a-nvda-003",
                    ticker="NVDA",
                    is_primary=1,
                )
            )
            sess.commit()

        with db_factory() as sess:
            refresh_news_clusters(sess, as_of=as_of)

        with db_factory() as sess:
            cluster_rows = sess.query(NewsArticleClusters).all()
            assert len(cluster_rows) == 1
            assert cluster_rows[0].cluster_id == "z-nvda-001"
            assert cluster_rows[0].headline_count == 3

    def test_collector_schedule_entries_registered(self) -> None:
        """The ``news.clustering`` and ``news.clustering_premarket`` cadences both
        wire to the refresh callable.

        Per § Story scope: "the renderer (story 04a) reads cluster rows at
        qualitative-researcher invocation time without re-running the
        pipeline — clustering is the producer, the renderer is the consumer,
        and they run on independent cadences."
        """
        from alphamind.collector.scheduler import COLLECTORS

        assert "news.clustering" in COLLECTORS
        assert "news.clustering_premarket" in COLLECTORS
        assert callable(COLLECTORS["news.clustering"])
        assert callable(COLLECTORS["news.clustering_premarket"])

    def test_unclustered_headlines_remain_null(self, db_factory: sessionmaker[Session]) -> None:
        """Singleton headlines (no peer matches) keep ``cross_ticker_cluster_id`` null."""
        from alphamind.data_sources.news.clustering import refresh_news_clusters

        articles = [
            _article(
                "aapl-001",
                "Apple announces new iPad refresh for spring lineup",
                "2026-05-01T12:00:00Z",
                source_outlet="Reuters",
                source_credibility_tier="tier_1",
                topic_tags="product_launch",
            ),
        ]
        _persist_articles(
            db_factory,
            articles,
            ticker_index={"aapl-001": frozenset({"AAPL"})},
        )

        as_of = datetime(2026, 5, 1, 18, 0, tzinfo=UTC)
        with db_factory() as sess:
            result = refresh_news_clusters(sess, as_of=as_of)

        # Singleton — no peer to cluster with.
        assert result.clusters_written == 0
        assert result.articles_unclustered == 1
        with db_factory() as sess:
            cluster_rows = sess.query(NewsArticleClusters).all()
            assert cluster_rows == []
            stamp = sess.execute(
                text("SELECT cross_ticker_cluster_id FROM news_articles WHERE article_id = :a"),
                {"a": "aapl-001"},
            ).scalar_one()
            assert stamp is None
