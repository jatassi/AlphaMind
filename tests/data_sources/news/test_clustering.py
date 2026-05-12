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
        from alphamind.analysis.news_clustering import clustering

        assert clustering.STAGE_1_LEVENSHTEIN_RATIO_MIN == 0.90

    def test_stage_1_time_window_minutes_value(self) -> None:
        from alphamind.analysis.news_clustering import clustering

        assert clustering.STAGE_1_TIME_WINDOW_MINUTES == 30

    def test_stage_2_simhash_jaccard_min_value(self) -> None:
        from alphamind.analysis.news_clustering import clustering

        assert clustering.STAGE_2_SIMHASH_JACCARD_MIN == 0.60

    def test_stage_2_time_window_hours_value(self) -> None:
        from alphamind.analysis.news_clustering import clustering

        assert clustering.STAGE_2_TIME_WINDOW_HOURS == 6

    def test_cluster_seal_hours_value(self) -> None:
        from alphamind.analysis.news_clustering import clustering

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
        from alphamind.analysis.news_clustering.clustering import canonicalize_syndication

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
        from alphamind.analysis.news_clustering.clustering import canonicalize_syndication

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
        from alphamind.analysis.news_clustering.clustering import cluster_events

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
        from alphamind.analysis.news_clustering.clustering import cluster_events

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
        from alphamind.analysis.news_clustering.clustering import cluster_events

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
        from alphamind.analysis.news_clustering.clustering import cluster_events

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

    def test_candidate_outside_first_seen_window_seeds_new_cluster(self) -> None:
        """Candidates farther than ``STAGE_2_TIME_WINDOW_HOURS`` from a cluster's
        ``first_seen_at`` must seed a fresh cluster, even when they match a
        recent member's SimHash + ticker.

        Per § Headline clustering Stage 2, criterion 3 requires "both members
        fall within ``STAGE_2_TIME_WINDOW_HOURS`` of the cluster's
        ``first_seen_at``" — not "within Stage-2 window of any member". A
        cluster seeded at ``T-8h`` whose latest member is at ``T-3h`` cannot
        absorb a candidate at ``T+0`` (8h from ``first_seen_at``), regardless
        of how similar that candidate is to the recent member.
        """
        from alphamind.analysis.news_clustering.clustering import cluster_events

        articles = [
            _article(
                "seed-old",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T04:00:00Z",  # T-8h relative to candidate
                topic_tags="earnings_related",
            ),
            _article(
                "seed-recent",
                "Nvidia reports record Q3 revenue beating analyst estimates",
                "2026-05-01T09:00:00Z",  # T-3h, within 6h of seed-old
                topic_tags="earnings_related",
            ),
            _article(
                "cand-now",
                "Nvidia reports record Q3 revenue topping analyst estimates",
                "2026-05-01T12:00:00Z",  # T+0; 3h from seed-recent, 8h from seed-old
                topic_tags="earnings_related",
            ),
        ]
        ticker_index: dict[str, frozenset[str]] = {
            "seed-old": frozenset({"NVDA"}),
            "seed-recent": frozenset({"NVDA"}),
            "cand-now": frozenset({"NVDA"}),
        }

        clusters = cluster_events(articles, ticker_index)

        # seed-old + seed-recent merge (3h apart). cand-now is 8h past
        # seed-old's first_seen_at, so it must seed a NEW cluster rather than
        # joining via the recent-member back-channel.
        cluster_by_id = {c.cluster_id: c for c in clusters}
        assert "seed-old" in cluster_by_id
        assert "cand-now" in cluster_by_id
        old_cluster = cluster_by_id["seed-old"]
        new_cluster = cluster_by_id["cand-now"]
        assert "cand-now" not in old_cluster.member_article_ids
        assert new_cluster.member_article_ids == ("cand-now",)

    def test_inner_loop_skips_clusters_outside_stage_2_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stage 2 must not iterate clusters older than ``STAGE_2_TIME_WINDOW_HOURS``.

        On a busy news day with hundreds of articles, naive O(n²) iteration of
        every existing cluster's every member dominates the 30-minute refresh
        cron. Per § Headline clustering, two members must fall within
        ``STAGE_2_TIME_WINDOW_HOURS`` of the cluster's ``first_seen_at``, so
        clusters whose ``first_seen_at`` is older than the candidate by more
        than that window cannot match by definition — iterating them is
        wasted work.

        Counts ``_candidate_matches_member`` invocations directly (rather than
        wrapping ``_find_joinable_cluster``) so the test stays stable against
        signature refactors to the inner search. With N out-of-window cluster
        anchors seeded before a candidate at hour 37.5 and one in-window
        anchor at hour 36, the count of per-member checks attributable to the
        candidate must be independent of N — only the in-window anchor's lone
        member should be inspected.
        """
        from alphamind.analysis.news_clustering import clustering

        def _seeds(n_old: int) -> tuple[list[NewsArticles], dict[str, frozenset[str]]]:
            # n_old anchors spaced 30 minutes apart starting 22h before the
            # candidate. Each is > 6h before the candidate (outside the
            # Stage-2 window) but < 24h before (inside the seal window) — so
            # the OLD pre-perf-#6 code would walk every one's members for the
            # candidate, while the new code skips them via the start_idx
            # advance. The in-window anchor seed-36 is 1.5h before the
            # candidate.
            specs: list[tuple[str, str]] = []
            for i in range(n_old):
                # Hours 14..14 + n_old*0.5 on day 1, all > 6h before day 2 13:30.
                hour = 14 + i // 2
                minute = (i % 2) * 30
                specs.append((f"seed-old-{i:02d}", f"2026-05-01T{hour:02d}:{minute:02d}:00Z"))
            articles = [
                _article(
                    aid,
                    f"Company unique-{aid} reports record Q3 revenue beating analyst",
                    ts,
                    topic_tags="earnings_related",
                )
                for aid, ts in specs
            ]
            articles.append(
                _article(
                    "seed-36",
                    "Company NVDA reports record Q3 revenue beating analyst estimates",
                    "2026-05-02T12:00:00Z",
                    topic_tags="earnings_related",
                )
            )
            articles.append(
                _article(
                    "cand-36",
                    "Company NVDA reports record Q3 revenue topping analyst estimates",
                    "2026-05-02T13:30:00Z",  # 1.5h after seed-36, > 6h but < 24h after seed-old-*
                    topic_tags="earnings_related",
                )
            )
            ticker_index: dict[str, frozenset[str]] = {
                aid: frozenset({f"TIC{i:02d}"}) for i, (aid, _) in enumerate(specs)
            }
            ticker_index["seed-36"] = frozenset({"NVDA"})
            ticker_index["cand-36"] = frozenset({"NVDA"})
            return articles, ticker_index

        def _count_matches_under_candidate(n_old: int) -> int:
            articles, ticker_index = _seeds(n_old)
            original = clustering._candidate_matches_member
            counts_by_candidate: dict[str, int] = {}

            def counter(
                candidate: clustering._CandidateProfile,
                member_id: str,
                ctx: clustering._ClusteringContext,
            ) -> bool:
                counts_by_candidate[candidate.article_id] = (
                    counts_by_candidate.get(candidate.article_id, 0) + 1
                )
                return original(candidate, member_id, ctx)

            monkeypatch.setattr(clustering, "_candidate_matches_member", counter)
            try:
                clusters = clustering.cluster_events(articles, ticker_index)
            finally:
                monkeypatch.setattr(clustering, "_candidate_matches_member", original)

            joined = next(c for c in clusters if c.cluster_id == "seed-36")
            assert "cand-36" in joined.member_article_ids
            return counts_by_candidate.get("cand-36", 0)

        # Count of per-member checks for the candidate must be independent of
        # how many out-of-window cluster anchors precede it: only the lone
        # in-window anchor is eligible, so each invocation should observe a
        # single per-member check.
        small = _count_matches_under_candidate(n_old=1)
        large = _count_matches_under_candidate(n_old=8)
        assert small == large == 1, (
            f"_candidate_matches_member invoked {small} times with 1 old cluster "
            f"and {large} times with 8 — expected 1 in both, since only the "
            "in-window anchor falls within STAGE_2_TIME_WINDOW_HOURS."
        )


class TestRefreshNewsClusters:
    """``refresh_news_clusters`` is the persistence-layer top-level callable."""

    def test_writes_clusters_and_stamps_cross_ticker_id(
        self, db_factory: sessionmaker[Session]
    ) -> None:
        """End-to-end: a single refresh writes cluster rows and stamps members."""
        from alphamind.analysis.news_clustering.clustering import refresh_news_clusters

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
        from alphamind.analysis.news_clustering.clustering import refresh_news_clusters

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
        from alphamind.analysis.news_clustering.clustering import refresh_news_clusters

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
        from alphamind.analysis.news_clustering.clustering import refresh_news_clusters

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
