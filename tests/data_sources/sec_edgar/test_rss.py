"""
Tests for src/alphamind/data_sources/sec_edgar/rss.py — story 05h.

All HTTP calls are mocked. Database uses SQLite in-memory. File I/O uses tmp_path.
"""

from __future__ import annotations

import json
import logging
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from alphamind.data_sources.sec_edgar.rss import collect_8k_filings
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    NewsArticles,
    NewsArticleTickers,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Sample RSS feed XML
# ---------------------------------------------------------------------------

_ACCESSION = "0001234567-24-000001"
_CIK = "0001234567"
_FILING_DATE = "2024-03-15"
_ITEM_DESC = "Item 2.02: Results of Operations and Financial Condition"
_PRIMARY_DOC_URL = "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000001/filing.htm"

_SAMPLE_RSS = textwrap.dedent(f"""\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:edgar="https://www.sec.gov/Archives/edgar">
  <channel>
    <title>SEC EDGAR 8-K Filings</title>
    <item>
      <title>8-K - ACME Corp (0001234567) - {_FILING_DATE}</title>
      <link>{_PRIMARY_DOC_URL}</link>
      <description>{_ITEM_DESC}</description>
      <pubDate>Fri, 15 Mar 2024 08:30:00 EST</pubDate>
      <edgar:companyName>ACME Corp</edgar:companyName>
      <edgar:CIK>{_CIK}</edgar:CIK>
      <edgar:accessionNumber>{_ACCESSION}</edgar:accessionNumber>
      <edgar:formType>8-K</edgar:formType>
      <edgar:dateFiled>{_FILING_DATE}</edgar:dateFiled>
    </item>
  </channel>
</rss>
""")

_SAMPLE_BODY_HTML = "<html><body><p>Filing body content here.</p></body></html>"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session_factory() -> sessionmaker[Session]:
    """In-memory SQLite with all tables created."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


@pytest.fixture()
def db_with_ticker(db_session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """DB with one universe ticker that has a CIK."""
    with db_session_factory() as sess:
        sess.add(
            AssetUniverse(
                asset_id="asset-acme-001",
                ticker="ACME",
                full_name="ACME Corp",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                cik=_CIK,
                is_active=1,
                added_date="2024-01-01",
                last_updated="2024-01-01",
            )
        )
        sess.commit()
    return db_session_factory


@pytest.fixture()
def db_with_ticker_no_cik(db_session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """DB with one universe ticker that has NO CIK."""
    with db_session_factory() as sess:
        sess.add(
            AssetUniverse(
                asset_id="asset-nocik-001",
                ticker="NOCIK",
                full_name="No CIK Corp",
                asset_class="equity",
                asset_role="universe",
                exchange="NYSE",
                cik=None,
                is_active=1,
                added_date="2024-01-01",
                last_updated="2024-01-01",
            )
        )
        sess.commit()
    return db_session_factory


class _FakeRunRepo:
    """In-memory run-tracking repository."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        self.rows[run_id] = {
            "status": "running",
            "rows_written": None,
            "error_summary": None,
        }

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        self.rows[run_id].update(status="success", rows_written=rows_written)

    def update_failed(self, run_id: str, error_summary: str) -> None:
        self.rows[run_id].update(status="failed", error_summary=error_summary)


def _make_http_transport(rss_xml: str, body_html: str) -> httpx.MockTransport:
    """Return a transport that serves RSS for EDGAR searches and HTML for filing docs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "efts.sec.gov" in url or "full-index" in url or "cgi-bin/browse-edgar" in url:
            return httpx.Response(
                200, text=rss_xml, headers={"content-type": "application/rss+xml"}
            )
        return httpx.Response(200, text=body_html, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# AC: collect_8k_filings writes news_articles rows
# ---------------------------------------------------------------------------


class TestCollect8kFilingsWritesRows:
    def test_writes_news_article_row(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """collect_8k_filings writes one news_articles row per unique 8-K filing."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)
        run_repo = _FakeRunRepo()

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=run_repo,
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            articles = sess.execute(select(NewsArticles)).scalars().all()
        assert len(articles) == 1
        art = articles[0]
        assert art.source == "sec_edgar_8k_rss"
        assert art.source_outlet == "SEC EDGAR"
        assert art.language == "en"

    def test_article_id_is_accession_number(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """article_id must equal the EDGAR accession number."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            article = sess.execute(select(NewsArticles)).scalars().first()
        assert article is not None
        assert article.article_id == _ACCESSION

    def test_headline_text_synthesized(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """headline_text is '<ticker> 8-K filed <date> — <item description>'."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            article = sess.execute(select(NewsArticles)).scalars().first()
        assert article is not None
        assert "ACME" in article.headline_text
        assert "8-K" in article.headline_text
        assert _FILING_DATE in article.headline_text

    def test_topic_tags_canonical_after_normalization(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """topic_tags JSON contains canonical HeadlineType.value for each item code.

        Item 2.02 maps to ``earnings_related``; the synthetic ``"8k"`` tag is
        no longer prepended (downstream consumers detect 8-K via event_type).
        """
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            article = sess.execute(select(NewsArticles)).scalars().first()
        assert article is not None
        tags = json.loads(article.topic_tags or "[]")
        assert tags == ["earnings_related"]
        assert "8k" not in tags

    def test_topic_tags_multiple_item_codes_normalized(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Item codes 2.02 and 5.02 normalize to ``earnings_related`` + ``insider_activity``."""
        rss_with_two_items = _SAMPLE_RSS.replace(
            "Item 2.02: Results of Operations and Financial Condition",
            "Item 2.02: Results of Operations and Item 5.02: Departure of Officers",
        )
        transport = _make_http_transport(rss_with_two_items, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            article = sess.execute(select(NewsArticles)).scalars().first()
        assert article is not None
        tags = json.loads(article.topic_tags or "[]")
        assert tags == ["earnings_related", "insider_activity"]


# ---------------------------------------------------------------------------
# AC: news_article_tickers populated via CIK resolution
# ---------------------------------------------------------------------------


class TestNewsArticleTickers:
    def test_ticker_row_written_via_cik(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """news_article_tickers gets one row with is_primary=1 for the matched ticker."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            rows = sess.execute(select(NewsArticleTickers)).scalars().all()
        assert len(rows) == 1
        assert rows[0].ticker == "ACME"
        assert rows[0].is_primary == 1

    def test_ticker_without_cik_skipped(
        self,
        db_with_ticker_no_cik: sessionmaker[Session],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Filings for tickers without a CIK produce no rows (warn logged)."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        with caplog.at_level(logging.WARNING):
            collect_8k_filings(
                since=since,
                session_factory=db_with_ticker_no_cik,
                user_agent="AlphaMind test@test.com",
                _transport=transport,
                _sleep=lambda _: None,
                _repo=_FakeRunRepo(),
                body_dir=tmp_path,
            )

        with db_with_ticker_no_cik() as sess:
            articles = sess.execute(select(NewsArticles)).scalars().all()
        # Filing CIK _CIK is not in universe → no articles written
        assert len(articles) == 0


# ---------------------------------------------------------------------------
# AC: body text persisted to disk; body_path populated
# ---------------------------------------------------------------------------


class TestBodyPersistence:
    def test_body_stored_to_disk(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Primary document is fetched, stripped to text, and saved; body_path set."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)

        collect_8k_filings(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        with db_with_ticker() as sess:
            article = sess.execute(select(NewsArticles)).scalars().first()
        assert article is not None
        assert article.body_path is not None
        body_file = Path(article.body_path)
        assert body_file.exists()
        assert "Filing body content" in body_file.read_text()


# ---------------------------------------------------------------------------
# AC: duplicate suppression
# ---------------------------------------------------------------------------


class TestDuplicateSuppression:
    def test_rerun_produces_no_duplicates(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Running collect_8k_filings twice on the same window yields one row."""
        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        since = datetime(2024, 3, 1, tzinfo=UTC)
        kwargs: dict[str, Any] = dict(
            since=since,
            session_factory=db_with_ticker,
            user_agent="AlphaMind test@test.com",
            _transport=transport,
            _sleep=lambda _: None,
            _repo=_FakeRunRepo(),
            body_dir=tmp_path,
        )

        collect_8k_filings(**kwargs)
        collect_8k_filings(**kwargs)

        with db_with_ticker() as sess:
            articles = sess.execute(select(NewsArticles)).scalars().all()
        assert len(articles) == 1


# ---------------------------------------------------------------------------
# AC: on failure, collection_runs records 'failed'; no data rows
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_failed_run_recorded_no_data_rows(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """When the RSS fetch raises, collection_runs is marked failed and no articles written."""

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = httpx.MockTransport(boom)
        since = datetime(2024, 3, 1, tzinfo=UTC)
        run_repo = _FakeRunRepo()

        with pytest.raises(httpx.ConnectError):
            collect_8k_filings(
                since=since,
                session_factory=db_with_ticker,
                user_agent="AlphaMind test@test.com",
                _transport=transport,
                _sleep=lambda _: None,
                _repo=run_repo,
                body_dir=tmp_path,
            )

        assert any(r["status"] == "failed" for r in run_repo.rows.values())

        with db_with_ticker() as sess:
            articles = sess.execute(select(NewsArticles)).scalars().all()
        assert len(articles) == 0


# ---------------------------------------------------------------------------
# AC: collect_8k_filings callable with no positional args (runner-registry)
# ---------------------------------------------------------------------------


class TestCollect8kFilingsNoArgs:
    def test_callable_with_no_args(
        self, db_with_ticker: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """collect_8k_filings() is callable with no positional args."""
        from unittest.mock import patch

        transport = _make_http_transport(_SAMPLE_RSS, _SAMPLE_BODY_HTML)
        run_repo = _FakeRunRepo()

        with (
            patch(
                "alphamind.data_sources.sec_edgar.rss.resume_since",
                return_value=datetime(2024, 3, 1, tzinfo=UTC),
            ),
            patch(
                "alphamind.data_sources.sec_edgar.rss.default_session_factory",
                return_value=db_with_ticker,
            ),
        ):
            collect_8k_filings(
                session_factory=db_with_ticker,
                user_agent="AlphaMind test@test.com",
                _transport=transport,
                _sleep=lambda _: None,
                _repo=run_repo,
                body_dir=tmp_path,
            )
