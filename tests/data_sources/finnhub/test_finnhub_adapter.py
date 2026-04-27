"""
Tests for the Finnhub vendor adapter — story 05f.

All Finnhub SDK calls are mocked.  Tests use an in-memory SQLite database
seeded with a minimal AssetUniverse row so FK constraints are satisfied.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from datetime import date as date_type
from unittest.mock import patch

import pytest

from alphamind.persistence.models import (
    Base,
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def session(session_factory):
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_tickers(session):
    """Insert minimal AssetUniverse rows for tickers we use in tests."""
    from alphamind.persistence.models import AssetUniverse

    for ticker in ("AAPL", "MSFT"):
        session.add(
            AssetUniverse(
                asset_id=f"asset-{ticker.lower()}",
                ticker=ticker,
                full_name=f"{ticker} Inc.",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
    session.commit()


@pytest.fixture()
def fake_repo(session_factory):
    """A thin in-memory repo that delegates to the real SQLite session."""

    class _Repo:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
            self.rows[run_id] = {
                "status": "running",
                "collector": collector,
                "rows_written": None,
                "error_summary": None,
            }

        def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
            self.rows[run_id].update(status="success", rows_written=rows_written)

        def update_failed(self, run_id: str, error_summary: str) -> None:
            self.rows[run_id].update(status="failed", error_summary=error_summary)

    return _Repo()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _article_id(url: str, published_at: str) -> str:
    return hashlib.sha256(f"finnhub\x00{url}\x00{published_at}".encode()).hexdigest()


class TestFinnhubClient:
    def test_verify_connectivity_calls_market_status(self) -> None:
        """verify_connectivity() calls the Finnhub SDK and returns without error."""
        with patch("finnhub.Client") as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.market_status.return_value = {"exchange": "US", "isOpen": True}

            from alphamind.data_sources.finnhub.client import FinnhubClient

            client = FinnhubClient(api_key="test-key", _rate_limiter=None)
            client.verify_connectivity()
            mock_instance.market_status.assert_called_once()

    def test_client_exposes_sdk_instance(self) -> None:
        """The FinnhubClient wraps the finnhub.Client and exposes it."""
        with patch("finnhub.Client"):
            from alphamind.data_sources.finnhub.client import FinnhubClient

            client = FinnhubClient(api_key="test-key", _rate_limiter=None)
            assert client.sdk is not None

    def test_rate_limiter_acquire_called_before_sdk(self) -> None:
        """acquire() is called on the rate limiter before each SDK method."""
        from alphamind.data_sources._common import RateLimiter

        limiter = RateLimiter()
        limiter.set_limit("finnhub", rate_per_minute=60)

        with patch("finnhub.Client") as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.market_status.return_value = {"exchange": "US", "isOpen": True}

            from alphamind.data_sources.finnhub.client import FinnhubClient

            acquire_calls: list[str] = []
            original_acquire = limiter.acquire

            def tracking_acquire(provider: str) -> None:
                acquire_calls.append(provider)
                return original_acquire(provider)

            limiter.acquire = tracking_acquire  # type: ignore[method-assign]

            client = FinnhubClient(api_key="test-key", _rate_limiter=limiter)
            client.verify_connectivity()

            assert "finnhub" in acquire_calls


class TestCollectNews:
    def _run_collect_news(
        self,
        engine,
        session_factory,
        fake_repo,
        ticker_scope: list[str],
        company_news_map: dict[str, list[dict]],
        general_news: list[dict],
        seeded_tickers=None,
    ) -> None:
        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value

            def company_news_side_effect(symbol, _from, to):
                return company_news_map.get(symbol, [])

            mock_sdk.company_news.side_effect = company_news_side_effect
            mock_sdk.general_news.return_value = general_news

            from alphamind.data_sources.finnhub.news import collect_news

            collect_news(
                ticker_scope=ticker_scope,
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

    def test_collect_news_writes_article_row(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """One company-news item produces one news_articles row."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL hits all-time high",
                "summary": "Apple stock reached...",
                "url": "https://example.com/aapl-ath",
                "datetime": 1745625600,  # 2026-04-26T00:00:00Z
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        rows = session.query(NewsArticles).all()
        assert len(rows) == 1
        assert rows[0].headline_text == "AAPL hits all-time high"
        assert rows[0].source == "finnhub"

    def test_collect_news_synthesizes_article_id(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """article_id is a SHA-256 of (source='finnhub', url, published_at)."""
        url = "https://example.com/aapl-ath"
        published_unix = 1745625600
        published_iso = datetime.fromtimestamp(published_unix, tz=UTC).isoformat()
        expected_id = _article_id(url, published_iso)

        articles = [
            {
                "id": 0,
                "headline": "AAPL hits all-time high",
                "summary": "Body text here",
                "url": url,
                "datetime": published_unix,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.article_id == expected_id

    def test_collect_news_stamps_credibility_tier(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """source_credibility_tier is stamped from news_outlets.yaml lookup."""
        articles = [
            {
                "id": 0,
                "headline": "Reuters article",
                "summary": "Body",
                "url": "https://reuters.com/article/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.source_credibility_tier == "tier_1"

    def test_collect_news_unknown_outlet_tier_is_none(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """Unknown outlet produces source_credibility_tier=None."""
        articles = [
            {
                "id": 0,
                "headline": "Unknown outlet article",
                "summary": "Body",
                "url": "https://unknown-blog.com/1",
                "datetime": 1745625600,
                "source": "UnknownBlog",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.source_credibility_tier is None

    def test_collect_news_vendor_sentiment_null(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """vendor_sentiment_score and vendor_sentiment_label are null for Finnhub."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL headline",
                "summary": "Body",
                "url": "https://example.com/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.vendor_sentiment_score is None
        assert row.vendor_sentiment_label is None

    def test_collect_news_body_persisted_to_disk(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """Body text is written to disk and body_path populated."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL headline",
                "summary": "This is the article body.",
                "url": "https://example.com/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.body_path is not None
        from pathlib import Path

        assert Path(row.body_path).exists()
        assert Path(row.body_path).read_text() == "This is the article body."

    def test_collect_news_no_summary_body_path_null(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """When summary is empty/missing, body_path is null."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL headline",
                "summary": "",
                "url": "https://example.com/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        row = session.query(NewsArticles).first()
        assert row is not None
        assert row.body_path is None

    def test_collect_news_writes_news_article_tickers(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """news_article_tickers rows are written for in-scope tickers."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL headline",
                "summary": "Body",
                "url": "https://example.com/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=["AAPL"],
                company_news_map={"AAPL": articles},
                general_news=[],
                seeded_tickers=seeded_tickers,
            )

        links = session.query(NewsArticleTickers).all()
        assert len(links) == 1
        assert links[0].ticker == "AAPL"
        assert links[0].is_primary == 1

    def test_collect_news_idempotent_no_duplicates(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """Re-running collect_news on the same window produces no duplicate rows."""
        articles = [
            {
                "id": 0,
                "headline": "AAPL headline",
                "summary": "Body",
                "url": "https://example.com/1",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            for _ in range(2):
                self._run_collect_news(
                    engine,
                    session_factory,
                    fake_repo,
                    ticker_scope=["AAPL"],
                    company_news_map={"AAPL": articles},
                    general_news=[],
                    seeded_tickers=seeded_tickers,
                )

        rows = session.query(NewsArticles).all()
        assert len(rows) == 1

    def test_collect_news_failure_records_failed_run(
        self, engine, session_factory, fake_repo, tmp_path
    ) -> None:
        """On SDK failure, the run is marked failed and no articles written."""
        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.company_news.side_effect = RuntimeError("API down")
            mock_sdk.general_news.side_effect = RuntimeError("API down")

            from alphamind.data_sources.finnhub.news import collect_news

            env_patch = patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)})
            with pytest.raises(RuntimeError), env_patch:
                collect_news(
                    ticker_scope=["AAPL"],
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        # Run was recorded as failed
        run = next(iter(fake_repo.rows.values()))
        assert run["status"] == "failed"

    def test_collect_news_general_news_no_ticker_link(
        self, engine, session_factory, session, seeded_tickers, fake_repo, tmp_path
    ) -> None:
        """General (market-wide) news creates news_articles but no ticker links."""
        general = [
            {
                "id": 999,
                "headline": "Markets rally broadly",
                "summary": "All sectors up.",
                "url": "https://marketwatch.com/market-rally",
                "datetime": 1745625600,
                "source": "MarketWatch",
                "related": "",
            }
        ]

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            self._run_collect_news(
                engine,
                session_factory,
                fake_repo,
                ticker_scope=[],
                company_news_map={},
                general_news=general,
                seeded_tickers=seeded_tickers,
            )

        articles = session.query(NewsArticles).all()
        assert len(articles) == 1
        links = session.query(NewsArticleTickers).all()
        assert len(links) == 0


class TestEarningsCalendar:
    def _make_earnings_item(
        self,
        ticker: str = "AAPL",
        date: str = "2026-04-30",
        hour: str = "amc",
        quarter: int = 2,
        year: int = 2026,
        **extra,
    ) -> dict:
        return {
            "symbol": ticker,
            "date": date,
            "hour": hour,
            "epsEstimate": extra.get("eps_estimate", 1.5),
            "epsActual": extra.get("eps_actual"),
            "revenueEstimate": extra.get("revenue_estimate", 90_000_000_000.0),
            "revenueActual": extra.get("revenue_actual"),
            "quarter": quarter,
            "year": year,
        }

    def test_collect_earnings_calendar_writes_event_calendar_row(
        self, engine, session_factory, session, seeded_tickers, fake_repo
    ) -> None:
        """One earnings item produces an event_calendar row with event_type='earnings'."""
        items = [self._make_earnings_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.earnings_calendar.return_value = {"earningsCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

            collect_earnings_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "earnings"
        assert events[0].source == "finnhub"

    def test_collect_earnings_calendar_writes_earnings_details(
        self, engine, session_factory, session, seeded_tickers, fake_repo
    ) -> None:
        """earnings_event_details is written with correct fields."""
        items = [self._make_earnings_item(eps_estimate=1.5, revenue_estimate=90e9)]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.earnings_calendar.return_value = {"earningsCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

            collect_earnings_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        details = session.query(EarningsEventDetails).all()
        assert len(details) == 1
        assert details[0].eps_consensus == 1.5
        assert details[0].revenue_consensus_usd == 90e9
        assert details[0].expected_call_time == "amc"

    def test_collect_earnings_calendar_maps_hour_to_call_time(
        self, engine, session_factory, session, seeded_tickers, fake_repo
    ) -> None:
        """Finnhub hour field maps: bmo->bmo, amc->amc, dmh->dmh."""
        items = [
            self._make_earnings_item(ticker="AAPL", date="2026-04-30", hour="bmo"),
            self._make_earnings_item(ticker="MSFT", date="2026-05-01", hour="amc"),
        ]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.earnings_calendar.return_value = {"earningsCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

            collect_earnings_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        all_details = session.query(EarningsEventDetails).all()
        details = {d.ticker: d.expected_call_time for d in all_details}
        assert details["AAPL"] == "bmo"
        assert details["MSFT"] == "amc"

    def test_collect_earnings_calendar_idempotent(
        self, engine, session_factory, session, seeded_tickers, fake_repo
    ) -> None:
        """Re-running on same window produces no duplicate rows."""
        items = [self._make_earnings_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.earnings_calendar.return_value = {"earningsCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

            for _ in range(2):
                collect_earnings_calendar(
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_bootstrap_earnings_calendar_uses_90_day_window(
        self, engine, session_factory, fake_repo
    ) -> None:
        """bootstrap_earnings_calendar() requests a 90-day forward window."""
        captured: list[dict] = []

        def fake_earnings_calendar(_from, to, symbol, international=False):
            captured.append({"from": _from, "to": to})
            return {"earningsCalendar": []}

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.earnings_calendar.side_effect = fake_earnings_calendar

            from alphamind.data_sources.finnhub.calendar import bootstrap_earnings_calendar

            bootstrap_earnings_calendar(
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        assert len(captured) == 1
        from_date = date_type.fromisoformat(captured[0]["from"])
        to_date = date_type.fromisoformat(captured[0]["to"])
        delta = (to_date - from_date).days
        assert delta >= 89  # approximately 90 days


class TestEconomicCalendar:
    def _make_eco_item(
        self,
        event: str = "CPI",
        date: str = "2026-05-01",
        time: str = "08:30",
        country: str = "US",
    ) -> dict:
        return {
            "event": event,
            "time": f"{date}T{time}:00",
            "country": country,
            "actual": None,
            "estimate": None,
            "prev": None,
            "unit": None,
        }

    def test_collect_economic_calendar_maps_cpi_to_event_type(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """CPI event maps to event_type='cpi_release'."""
        items = [self._make_eco_item(event="CPI")]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.calendar_economic.return_value = {"economicCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

            collect_economic_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "cpi_release"

    def test_collect_economic_calendar_maps_fomc_to_event_type(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """FOMC Statement maps to event_type='fomc'."""
        items = [self._make_eco_item(event="FOMC Statement")]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.calendar_economic.return_value = {"economicCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

            collect_economic_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "fomc"

    def test_collect_economic_calendar_unknown_event_maps_to_other(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """Unknown event names fall back to event_type='other'."""
        items = [self._make_eco_item(event="Some Obscure Index")]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.calendar_economic.return_value = {"economicCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

            collect_economic_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "other"

    def test_collect_economic_calendar_idempotent(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_eco_item(event="CPI")]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.calendar_economic.return_value = {"economicCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

            for _ in range(2):
                collect_economic_calendar(
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_bootstrap_economic_calendar_uses_90_day_window(
        self, engine, session_factory, fake_repo
    ) -> None:
        """bootstrap_economic_calendar() requests a 90-day forward window."""
        captured: list[dict] = []

        def fake_eco_calendar(_from=None, to=None):
            captured.append({"from": _from, "to": to})
            return {"economicCalendar": []}

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.calendar_economic.side_effect = fake_eco_calendar

            from alphamind.data_sources.finnhub.calendar import bootstrap_economic_calendar

            bootstrap_economic_calendar(
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        assert len(captured) == 1
        from_date = date_type.fromisoformat(captured[0]["from"])
        to_date = date_type.fromisoformat(captured[0]["to"])
        delta = (to_date - from_date).days
        assert delta >= 89


class TestIpoCalendar:
    def _make_ipo_item(
        self, name: str = "NewCo Inc.", date: str = "2026-05-10", ticker: str = "NEWC"
    ) -> dict:
        return {
            "symbol": ticker,
            "name": name,
            "date": date,
            "offerPrice": 15.0,
            "totalSharesValue": 500_000_000.0,
            "exchange": "NASDAQ",
        }

    def test_collect_ipo_calendar_writes_event_calendar_row(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """An IPO item produces an event_calendar row."""
        items = [self._make_ipo_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.ipo_calendar.return_value = {"ipoCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_ipo_calendar

            collect_ipo_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].source == "finnhub"

    def test_collect_ipo_calendar_idempotent(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_ipo_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.ipo_calendar.return_value = {"ipoCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_ipo_calendar

            for _ in range(2):
                collect_ipo_calendar(
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        events = session.query(EventCalendar).all()
        assert len(events) == 1


class TestFdaCalendar:
    def _make_fda_item(self, date: str = "2026-05-15", drug_name: str = "TestDrug") -> dict:
        return {
            "date": date,
            "drug_name": drug_name,
            "sponsor": "BigPharma Inc.",
            "status": "Scheduled",
        }

    def test_collect_fda_calendar_writes_event_calendar_row(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """An FDA item produces an event_calendar row with event_type='fda_advisory'."""
        items = [self._make_fda_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.fda_calendar.return_value = {"fdaCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

            collect_fda_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "fda_advisory"

    def test_collect_fda_calendar_idempotent(
        self, engine, session_factory, session, fake_repo
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_fda_item()]

        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.fda_calendar.return_value = {"fdaCalendar": items}

            from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

            for _ in range(2):
                collect_fda_calendar(
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_collect_fda_calendar_failure_records_failed_run(
        self, engine, session_factory, fake_repo
    ) -> None:
        """On SDK failure, the run is marked failed."""
        with patch("finnhub.Client") as mock_client:
            mock_sdk = mock_client.return_value
            mock_sdk.fda_calendar.side_effect = RuntimeError("API down")

            from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

            with pytest.raises(RuntimeError):
                collect_fda_calendar(
                    since=datetime(2026, 4, 25, tzinfo=UTC),
                    _engine=engine,
                    _session_factory=session_factory,
                    _repo=fake_repo,
                )

        run = next(iter(fake_repo.rows.values()))
        assert run["status"] == "failed"
