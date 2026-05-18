"""
Tests for the Finnhub vendor adapter — story 05f.

All Finnhub SDK calls are routed through FakeFinnhubSDK.  Tests use an
in-memory SQLite database seeded with a minimal AssetUniverse row so FK
constraints are satisfied.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import alphamind.state.tables.invocations  # noqa: F401  # register briefs.invocation_id FK target
from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    Base,
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
)
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.finnhub import FakeFinnhubSDK
from tests.data_sources._fakes.run_repo import FakeRunRepo

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
def session_factory(engine: Engine) -> sessionmaker[Session]:
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


@pytest.fixture()
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_tickers(session: Session) -> None:
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
def fake_repo() -> FakeRunRepo:
    return FakeRunRepo()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _article_id(url: str, published_at: str) -> str:
    return hashlib.sha256(f"finnhub\x00{url}\x00{published_at}".encode()).hexdigest()


class TestFinnhubClient:
    def test_verify_connectivity_calls_market_status(self) -> None:
        """verify_connectivity() calls the Finnhub SDK and returns without error."""
        from alphamind.data_sources.finnhub.client import FinnhubClient

        sdk = FakeFinnhubSDK()
        client = FinnhubClient.__new__(FinnhubClient)
        client._sdk = sdk
        client._limiter = None
        client.verify_connectivity()

        assert sdk.market_status_calls == [{"exchange": "US"}]

    def test_client_exposes_sdk_instance(self) -> None:
        """The FinnhubClient wraps the finnhub.Client and exposes it."""
        from alphamind.data_sources.finnhub.client import FinnhubClient

        sdk = FakeFinnhubSDK()
        client = FinnhubClient.__new__(FinnhubClient)
        client._sdk = sdk
        client._limiter = None
        assert client.sdk is sdk

    def test_rate_limiter_acquire_called_before_sdk(self) -> None:
        """acquire() is called on the rate limiter before each SDK method."""
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.finnhub.client import FinnhubClient

        limiter = RateLimiter()
        limiter.set_limit("finnhub", rate_per_minute=60)

        acquire_calls: list[str] = []
        original_acquire = limiter.acquire

        def tracking_acquire(provider: str) -> None:
            acquire_calls.append(provider)
            original_acquire(provider)

        limiter.acquire = tracking_acquire  # type: ignore[method-assign]  # mock-method assignment

        sdk = FakeFinnhubSDK()
        client = FinnhubClient.__new__(FinnhubClient)
        client._sdk = sdk
        client._limiter = limiter
        client.verify_connectivity()

        assert "finnhub" in acquire_calls


class TestCollectNews:
    def _run_collect_news(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        fake_repo: FakeRunRepo,
        ticker_scope: list[str],
        company_news_map: dict[str, list[dict[str, Any]]],
        general_news: list[dict[str, Any]],
        seeded_tickers: Any = None,
    ) -> None:
        sdk = FakeFinnhubSDK(
            company_news_by_symbol=company_news_map,
            general_news_response=general_news,
        )

        from alphamind.data_sources.finnhub.news import collect_news

        collect_news(
            ticker_scope=ticker_scope,
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

    def test_collect_news_writes_article_row(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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

        assert Path(row.body_path).exists()
        assert Path(row.body_path).read_text() == "This is the article body."

    def test_collect_news_no_summary_body_path_null(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        fake_repo: FakeRunRepo,
        tmp_path: Path,
    ) -> None:
        """On SDK failure, the run is marked failed and no articles written."""
        sdk = FakeFinnhubSDK(
            company_news_error=RuntimeError("API down"),
            general_news_error=RuntimeError("API down"),
        )

        from alphamind.data_sources.finnhub.news import collect_news

        env_patch = patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)})
        with pytest.raises(RuntimeError), env_patch:
            collect_news(
                ticker_scope=["AAPL"],
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        assert fake_repo.failed()

    def test_collect_news_isolates_per_ticker_502(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A FinnhubAPIException(502) on one ticker is isolated.

        Other tickers' articles still persist; the run completes with
        status='success' and an ``error_summary`` naming the failed ticker.
        """
        from finnhub.exceptions import FinnhubAPIException

        # Skip retry sleep — vendor_outage_extended would otherwise burn 65s.
        monkeypatch.setattr("alphamind.data_sources._common.retry.time.sleep", lambda _: None)

        class _FakeResponse:
            status_code = 502
            text = "Bad Gateway"

            def json(self) -> dict[str, str]:
                return {"error": "Bad Gateway"}

        aapl_articles = [
            {
                "id": 0,
                "headline": "AAPL up",
                "summary": "Body",
                "url": "https://example.com/aapl",
                "datetime": 1745625600,
                "source": "Reuters",
                "related": "AAPL",
            }
        ]

        def handler(symbol: str, **_: Any) -> list[dict[str, Any]]:
            if symbol == "MSFT":
                raise FinnhubAPIException(_FakeResponse())
            return list(aapl_articles) if symbol == "AAPL" else []

        sdk = FakeFinnhubSDK(company_news_handler=handler)

        from alphamind.data_sources.finnhub.news import collect_news

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            collect_news(
                ticker_scope=["AAPL", "MSFT"],
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        # AAPL's article still persisted.
        rows = session.query(NewsArticles).all()
        assert len(rows) == 1
        assert rows[0].headline_text == "AAPL up"

        # Run is success, with error_summary naming MSFT.
        assert fake_repo.succeeded()
        row = fake_repo.latest()
        assert row["error_summary"] is not None
        assert "MSFT" in row["error_summary"]

    def test_collect_news_retries_failing_ticker_per_vendor_outage_extended(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 502 on a single ticker is retried per ``vendor_outage_extended`` (4 attempts)."""
        from finnhub.exceptions import FinnhubAPIException

        monkeypatch.setattr("alphamind.data_sources._common.retry.time.sleep", lambda _: None)

        class _FakeResponse:
            status_code = 502
            text = "Bad Gateway"

            def json(self) -> dict[str, str]:
                return {"error": "Bad Gateway"}

        def handler(symbol: str, **_: Any) -> list[dict[str, Any]]:
            raise FinnhubAPIException(_FakeResponse())

        sdk = FakeFinnhubSDK(company_news_handler=handler)

        from alphamind.data_sources.finnhub.news import collect_news

        with patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}):
            collect_news(
                ticker_scope=["AAPL"],
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        # vendor_outage_extended = 4 attempts total
        aapl_calls = [c for c in sdk.company_news_calls if c["symbol"] == "AAPL"]
        assert len(aapl_calls) == 4

    def test_collect_news_general_news_no_ticker_link(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
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

    def test_collect_news_callable_with_no_args(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
        tmp_path: Path,
    ) -> None:
        """collect_news() is callable with no positional args (runner-registry contract)."""
        sdk = FakeFinnhubSDK()  # default empty

        with (
            patch(
                "alphamind.data_sources.finnhub.news.active_universe_tickers",
                return_value=["AAPL"],
            ),
            patch(
                "alphamind.data_sources.finnhub.news.resume_since",
                return_value=datetime(2026, 4, 25, tzinfo=UTC),
            ),
            patch.dict(os.environ, {"ALPHAMIND_NEWS_DIR": str(tmp_path)}),
        ):
            from alphamind.data_sources.finnhub.news import collect_news

            collect_news(
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )


class TestEarningsCalendar:
    def _make_earnings_item(
        self,
        ticker: str = "AAPL",
        date: str = "2026-04-30",
        hour: str = "amc",
        quarter: int = 2,
        year: int = 2026,
        **extra: Any,
    ) -> dict[str, Any]:
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
    ) -> None:
        """One earnings item produces an event_calendar row with event_type='earnings'."""
        items = [self._make_earnings_item()]
        sdk = FakeFinnhubSDK(earnings_calendar_response={"earningsCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

        collect_earnings_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "earnings"
        assert events[0].source == "finnhub"

    def test_collect_earnings_calendar_writes_earnings_details(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
    ) -> None:
        """earnings_event_details is written with correct fields."""
        items = [self._make_earnings_item(eps_estimate=1.5, revenue_estimate=90e9)]
        sdk = FakeFinnhubSDK(earnings_calendar_response={"earningsCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

        collect_earnings_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        details = session.query(EarningsEventDetails).all()
        assert len(details) == 1
        assert details[0].eps_consensus == 1.5
        assert details[0].revenue_consensus_usd == 90e9
        assert details[0].expected_call_time == "amc"

    def test_collect_earnings_calendar_maps_hour_to_call_time(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Finnhub hour field maps: bmo->bmo, amc->amc, dmh->dmh."""
        items = [
            self._make_earnings_item(ticker=Symbol("AAPL"), date="2026-04-30", hour="bmo"),
            self._make_earnings_item(ticker=Symbol("MSFT"), date="2026-05-01", hour="amc"),
        ]
        sdk = FakeFinnhubSDK(earnings_calendar_response={"earningsCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

        collect_earnings_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        all_details = session.query(EarningsEventDetails).all()
        details = {d.ticker: d.expected_call_time for d in all_details}
        assert details["AAPL"] == "bmo"
        assert details["MSFT"] == "amc"

    def test_collect_earnings_calendar_idempotent(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        seeded_tickers: None,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Re-running on same window produces no duplicate rows."""
        items = [self._make_earnings_item()]
        sdk = FakeFinnhubSDK(earnings_calendar_response={"earningsCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_earnings_calendar

        for _ in range(2):
            collect_earnings_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_bootstrap_earnings_calendar_uses_90_day_window(
        self, engine: Engine, session_factory: sessionmaker[Session], fake_repo: FakeRunRepo
    ) -> None:
        """bootstrap_earnings_calendar() requests a 90-day forward window."""
        sdk = FakeFinnhubSDK()  # default: empty earningsCalendar

        from alphamind.data_sources.finnhub.calendar import bootstrap_earnings_calendar

        bootstrap_earnings_calendar(
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        assert len(sdk.earnings_calendar_calls) == 1
        first = sdk.earnings_calendar_calls[0]
        from_date = date_type.fromisoformat(first["_from"])
        to_date = date_type.fromisoformat(first["to"])
        delta = (to_date - from_date).days
        assert delta >= 89  # approximately 90 days


class TestEconomicCalendar:
    def _make_eco_item(
        self,
        event: str = "CPI",
        date: str = "2026-05-01",
        time: str = "08:30",
        country: str = "US",
    ) -> dict[str, Any]:
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
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """CPI event maps to event_type='cpi_release'."""
        items = [self._make_eco_item(event="CPI")]
        sdk = FakeFinnhubSDK(economic_calendar_response={"economicCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

        collect_economic_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "cpi_release"

    def test_collect_economic_calendar_maps_fomc_to_event_type(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """FOMC Statement maps to event_type='fomc'."""
        items = [self._make_eco_item(event="FOMC Statement")]
        sdk = FakeFinnhubSDK(economic_calendar_response={"economicCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

        collect_economic_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "fomc"

    def test_collect_economic_calendar_unknown_event_maps_to_other(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Unknown event names fall back to event_type='other'."""
        items = [self._make_eco_item(event="Some Obscure Index")]
        sdk = FakeFinnhubSDK(economic_calendar_response={"economicCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

        collect_economic_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "other"

    def test_collect_economic_calendar_idempotent(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_eco_item(event="CPI")]
        sdk = FakeFinnhubSDK(economic_calendar_response={"economicCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_economic_calendar

        for _ in range(2):
            collect_economic_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_bootstrap_economic_calendar_uses_90_day_window(
        self, engine: Engine, session_factory: sessionmaker[Session], fake_repo: FakeRunRepo
    ) -> None:
        """bootstrap_economic_calendar() requests a 90-day forward window."""
        sdk = FakeFinnhubSDK()

        from alphamind.data_sources.finnhub.calendar import bootstrap_economic_calendar

        bootstrap_economic_calendar(
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        assert len(sdk.economic_calendar_calls) == 1
        first = sdk.economic_calendar_calls[0]
        from_date = date_type.fromisoformat(first["_from"])
        to_date = date_type.fromisoformat(first["to"])
        delta = (to_date - from_date).days
        assert delta >= 89


class TestIpoCalendar:
    def _make_ipo_item(
        self, name: str = "NewCo Inc.", date: str = "2026-05-10", ticker: str = "NEWC"
    ) -> dict[str, Any]:
        return {
            "symbol": ticker,
            "name": name,
            "date": date,
            "offerPrice": 15.0,
            "totalSharesValue": 500_000_000.0,
            "exchange": "NASDAQ",
        }

    def test_collect_ipo_calendar_writes_event_calendar_row(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """An IPO item produces an event_calendar row."""
        items = [self._make_ipo_item()]
        sdk = FakeFinnhubSDK(ipo_calendar_response={"ipoCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_ipo_calendar

        collect_ipo_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].source == "finnhub"

    def test_collect_ipo_calendar_idempotent(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_ipo_item()]
        sdk = FakeFinnhubSDK(ipo_calendar_response={"ipoCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_ipo_calendar

        for _ in range(2):
            collect_ipo_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1


class TestFdaCalendar:
    def _make_fda_item(
        self, date: str = "2026-05-15", drug_name: str = "TestDrug"
    ) -> dict[str, Any]:
        return {
            "date": date,
            "drug_name": drug_name,
            "sponsor": "BigPharma Inc.",
            "status": "Scheduled",
        }

    def test_collect_fda_calendar_writes_event_calendar_row(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """An FDA item produces an event_calendar row with event_type='fda_advisory'."""
        items = [self._make_fda_item()]
        sdk = FakeFinnhubSDK(fda_calendar_response={"fdaCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

        collect_fda_calendar(
            since=datetime(2026, 4, 25, tzinfo=UTC),
            _engine=engine,
            _session_factory=session_factory,
            _repo=fake_repo,
            _sdk=sdk,
        )

        events = session.query(EventCalendar).all()
        assert len(events) == 1
        assert events[0].event_type == "fda_advisory"

    def test_collect_fda_calendar_idempotent(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        session: Session,
        fake_repo: FakeRunRepo,
    ) -> None:
        """Re-running on same window produces no duplicates."""
        items = [self._make_fda_item()]
        sdk = FakeFinnhubSDK(fda_calendar_response={"fdaCalendar": items})

        from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

        for _ in range(2):
            collect_fda_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        events = session.query(EventCalendar).all()
        assert len(events) == 1

    def test_collect_fda_calendar_failure_records_failed_run(
        self, engine: Engine, session_factory: sessionmaker[Session], fake_repo: FakeRunRepo
    ) -> None:
        """On SDK failure, the run is marked failed."""
        sdk = FakeFinnhubSDK(fda_calendar_error=RuntimeError("API down"))

        from alphamind.data_sources.finnhub.calendar import collect_fda_calendar

        with pytest.raises(RuntimeError):
            collect_fda_calendar(
                since=datetime(2026, 4, 25, tzinfo=UTC),
                _engine=engine,
                _session_factory=session_factory,
                _repo=fake_repo,
                _sdk=sdk,
            )

        assert fake_repo.failed()
