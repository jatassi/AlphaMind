"""
Tests for src/alphamind/data_sources/marketaux/news.py

All HTTP calls are mocked via MarketauxClient injection.
Persistence uses an in-memory SQLite database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind.data_sources.marketaux.news import collect_news
from alphamind.persistence.models import Base, NewsArticles, NewsArticleTickers
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session() -> sessionmaker[Session]:
    """In-memory SQLite session with all tables created."""
    engine = make_engine(":memory:")
    # asset_universe is referenced by FK — insert a row
    from sqlalchemy import text

    Base.metadata.create_all(engine)
    session_factory: sessionmaker[Session] = make_session_factory(engine)
    with session_factory() as sess:
        sess.execute(
            text(
                "INSERT INTO asset_universe (asset_id, ticker, full_name, asset_class, "
                "asset_role, exchange, is_active, added_date, last_updated) "
                "VALUES ('au1', 'AAPL', 'Apple Inc', 'equity', 'universe', 'NASDAQ', "
                "1, '2020-01-01', '2020-01-01')"
            )
        )
        sess.commit()
    return session_factory


@pytest.fixture()
def fake_repo(db_session: sessionmaker[Session]) -> Any:
    """Minimal repo stub that track_run can call."""

    class _Repo:
        def __init__(self) -> None:
            self._runs: dict[str, dict[str, Any]] = {}

        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            self._runs[run_id] = {
                "status": "running",
                "collector": collector,
                "started_at": started_at,
                "rows_written": None,
                "error_summary": None,
            }

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            self._runs[run_id]["status"] = "success"
            self._runs[run_id]["rows_written"] = rows_written

        def update_failed(self, run_id: str, error_summary: str) -> None:
            self._runs[run_id]["status"] = "failed"
            self._runs[run_id]["error_summary"] = error_summary

    return _Repo()


def _article(
    uuid: str = "a1",
    title: str = "Headline",
    description: str = "Body text",
    url: str = "https://example.com/a1",
    published_at: str = "2024-01-15T10:00:00.000000Z",
    source: str = "Reuters",
    language: str = "en",
    entities: list[dict[str, Any]] | None = None,
    topics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "title": title,
        "description": description,
        "url": url,
        "published_at": published_at,
        "source": source,
        "language": language,
        "entities": entities or [{"symbol": "AAPL", "sentiment_score": 0.42}],
        "topics": topics or [],
    }


def _expected_article_id(url: str, published_at: str) -> str:
    raw = f"marketaux|{url}|{published_at}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# AC: collect_news writes news_articles rows with vendor_sentiment_score
# ---------------------------------------------------------------------------


class TestCollectNewsArticles:
    def test_writes_article_row(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article()
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            rows = sess.query(NewsArticles).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source == "marketaux"
        assert row.headline_text == "Headline"
        assert row.vendor_sentiment_score == pytest.approx(0.42)

    def test_vendor_sentiment_label_positive(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.42}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.vendor_sentiment_label == "positive"

    def test_vendor_sentiment_label_negative(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": -0.30}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.vendor_sentiment_label == "negative"

    def test_vendor_sentiment_label_neutral(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.05}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.vendor_sentiment_label == "neutral"


# ---------------------------------------------------------------------------
# AC: body_path populated; text persisted to disk
# ---------------------------------------------------------------------------


class TestBodyPersistence:
    def test_body_written_to_disk(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(description="Some body content")
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()

        assert row is not None
        assert row.body_path is not None
        body_file = Path(row.body_path)
        assert body_file.exists()
        assert body_file.read_text() == "Some body content"

    def test_body_path_format(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(published_at="2024-03-07T10:00:00.000000Z")
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-01T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()

        assert row is not None
        assert row.body_path is not None
        # Path must be under <body_dir>/2024/03/<article_id>.txt
        p = Path(row.body_path)
        assert p.parent.name == "03"
        assert p.parent.parent.name == "2024"
        assert p.suffix == ".txt"


# ---------------------------------------------------------------------------
# AC (story 06): topic_tags normalized through headline_tag_mapping.yaml
# ---------------------------------------------------------------------------


class TestTopicTagsNormalization:
    def test_topic_tags_canonical_after_normalization(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """Vendor topic names are normalized to canonical HeadlineType.value strings."""
        import json as _json

        article = _article(
            topics=[{"name": "earnings"}, {"name": "macro"}],
        )
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.topic_tags is not None
        tags = _json.loads(row.topic_tags)
        assert tags == ["earnings_related", "macro_data"]

    def test_unmapped_vendor_topics_dropped_silently(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """Unmapped vendor topics drop; if all topics drop, topic_tags is None."""
        article = _article(topics=[{"name": "unknown_vendor_topic"}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.topic_tags is None

    def test_no_topics_writes_none(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """An article with no topics array still writes None for topic_tags."""
        article = _article(topics=[])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.topic_tags is None


# ---------------------------------------------------------------------------
# AC: news_article_tickers populated
# ---------------------------------------------------------------------------


class TestArticleTickers:
    def test_primary_ticker_written(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.1}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            tickers = sess.query(NewsArticleTickers).all()
        assert len(tickers) == 1
        assert tickers[0].ticker == "AAPL"
        assert tickers[0].is_primary == 1

    def test_secondary_ticker_not_inserted_when_not_in_universe(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """Tickers not in asset_universe cannot satisfy FK — they are skipped."""
        article = _article(
            entities=[
                {"symbol": "AAPL", "sentiment_score": 0.1},
                {"symbol": "MSFT", "sentiment_score": 0.2},  # not in universe
            ]
        )
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            tickers = sess.query(NewsArticleTickers).all()
        # Only AAPL (which is in universe) should be inserted
        assert all(t.ticker == "AAPL" for t in tickers)

    def test_ticker_row_carries_vendor_sentiment_score(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """Each news_article_tickers row has vendor_sentiment_score from entities[]."""
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.55}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticleTickers).first()
        assert row is not None
        assert row.vendor_sentiment_score == pytest.approx(0.55)

    def test_ticker_row_carries_vendor_sentiment_label_positive(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.55}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticleTickers).first()
        assert row is not None
        assert row.vendor_sentiment_label == "positive"

    def test_ticker_row_vendor_sentiment_label_negative(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": -0.20}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticleTickers).first()
        assert row is not None
        assert row.vendor_sentiment_label == "negative"

    def test_ticker_row_vendor_sentiment_label_neutral(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(entities=[{"symbol": "AAPL", "sentiment_score": 0.05}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticleTickers).first()
        assert row is not None
        assert row.vendor_sentiment_label == "neutral"

    def test_ticker_row_null_sentiment_when_entity_has_no_score(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """When entity lacks sentiment_score, both fields are None."""
        article = _article(entities=[{"symbol": "AAPL"}])
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticleTickers).first()
        assert row is not None
        assert row.vendor_sentiment_score is None
        assert row.vendor_sentiment_label is None


# ---------------------------------------------------------------------------
# AC: source_credibility_tier stamped from news_outlets.yaml
# ---------------------------------------------------------------------------


class TestCredibilityTier:
    def test_known_outlet_stamped(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(source="Reuters")
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.source_credibility_tier == "tier_1"

    def test_unknown_outlet_is_none(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article(source="UnknownBlog")
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()
        assert row is not None
        assert row.source_credibility_tier is None


# ---------------------------------------------------------------------------
# AC: article_id synthesized as SHA-256 of (source='marketaux', url, published_at)
# ---------------------------------------------------------------------------


class TestArticleId:
    def test_article_id_is_sha256(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        url = "https://example.com/a1"
        published_at = "2024-01-15T10:00:00.000000Z"
        article = _article(url=url, published_at=published_at)
        client = MagicMock()
        client.get_news.return_value = [article]

        collect_news(
            ticker_scope=["AAPL"],
            since="2024-01-01T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        with db_session() as sess:
            row = sess.query(NewsArticles).first()

        assert row is not None
        expected = _expected_article_id(url, published_at)
        assert row.article_id == expected


# ---------------------------------------------------------------------------
# AC: idempotent — re-running on same window produces no duplicate rows
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_no_duplicates_on_second_run(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        article = _article()
        client = MagicMock()
        client.get_news.return_value = [article]

        for _ in range(2):
            collect_news(
                ticker_scope=["AAPL"],
                since="2024-01-15T00:00:00Z",
                _client=client,
                _session_factory=db_session,
                _repo=fake_repo,
                _body_dir=str(tmp_path),
            )

        with db_session() as sess:
            count = sess.query(NewsArticles).count()
        assert count == 1


# ---------------------------------------------------------------------------
# AC: On failure, collection_runs records 'failed'; no data rows written
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_failed_run_recorded(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        client = MagicMock()
        client.get_news.side_effect = RuntimeError("API down")

        with pytest.raises(RuntimeError):
            collect_news(
                ticker_scope=["AAPL"],
                since="2024-01-15T00:00:00Z",
                _client=client,
                _session_factory=db_session,
                _repo=fake_repo,
                _body_dir=str(tmp_path),
            )

        run = next(iter(fake_repo._runs.values()))
        assert run["status"] == "failed"
        assert "API down" in run["error_summary"]

    def test_no_data_rows_on_failure(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        client = MagicMock()
        client.get_news.side_effect = RuntimeError("API down")

        with pytest.raises(RuntimeError):
            collect_news(
                ticker_scope=["AAPL"],
                since="2024-01-15T00:00:00Z",
                _client=client,
                _session_factory=db_session,
                _repo=fake_repo,
                _body_dir=str(tmp_path),
            )

        with db_session() as sess:
            count = sess.query(NewsArticles).count()
        assert count == 0


# ---------------------------------------------------------------------------
# AC: market-wide query (no ticker scope) uses countries=us
# ---------------------------------------------------------------------------


class TestMarketWideQuery:
    def test_market_wide_calls_countries_us(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        client = MagicMock()
        client.get_news.return_value = []

        collect_news(
            ticker_scope=None,
            since="2024-01-15T00:00:00Z",
            _client=client,
            _session_factory=db_session,
            _repo=fake_repo,
            _body_dir=str(tmp_path),
        )

        client.get_news.assert_called_once()
        call_kwargs = client.get_news.call_args[1]
        assert call_kwargs.get("symbols") is None
        assert call_kwargs.get("countries") == "us"


class TestCollectNewsNoArgs:
    def test_callable_with_no_args(
        self, db_session: sessionmaker[Session], fake_repo: Any, tmp_path: Path
    ) -> None:
        """collect_news() is callable with no positional args (runner-registry contract)."""
        from unittest.mock import patch

        client = MagicMock()
        client.get_news.return_value = []

        from datetime import UTC, datetime

        with (
            patch(
                "alphamind.data_sources.marketaux.news.active_universe_tickers",
                return_value=["AAPL"],
            ),
            patch(
                "alphamind.data_sources.marketaux.news.resume_since",
                return_value=datetime(2024, 1, 15, tzinfo=UTC),
            ),
        ):
            collect_news(
                _client=client,
                _session_factory=db_session,
                _repo=fake_repo,
                _body_dir=str(tmp_path),
            )
