"""Tests for the news-freshness diagnostic — ALP-567.

When a consumer queries ``news_articles`` over a window and gets zero rows,
:func:`alphamind.analysis.news_freshness.diagnose_empty_news` classifies the
cause so the digest, ``news_search`` tool, and per-sector input bundle can
surface a specific reason instead of letting the LLM agent re-diagnose
absence each invocation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.news_freshness import (
    NewsEmptyDiagnosis,
    NewsEmptyReason,
    diagnose_empty_news,
)
from alphamind.persistence.models import Base, NewsArticles
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.state.tables.invocations import InvocationRow

# Imported for the side-effect of registering ``invocations`` on ``Base.metadata`` —
# ``briefs.invocation_id`` FK resolves only when this module has been loaded.
del InvocationRow

AS_OF = datetime(2026, 5, 19, 3, 6, 54, tzinfo=UTC)
WINDOW_START = AS_OF - timedelta(hours=24)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_article(
    session: Session,
    *,
    article_id: str,
    published_at: datetime,
    ingested_at: datetime,
) -> None:
    session.add(
        NewsArticles(
            article_id=article_id,
            source="finnhub",
            source_outlet="Reuters",
            source_credibility_tier="tier_1",
            language="en",
            headline_text=f"headline-{article_id}",
            url=f"https://example.com/{article_id}",
            published_at=_iso(published_at),
            ingested_at=_iso(ingested_at),
            topic_tags=None,
            body_path=None,
            cross_ticker_cluster_id=None,
        )
    )


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def test_no_rows_in_db_classified_when_table_empty(session: Session) -> None:
    """An empty ``news_articles`` table classifies as ``NO_ROWS_IN_DB``."""
    diagnosis = diagnose_empty_news(session, window_start=WINDOW_START)
    assert diagnosis == NewsEmptyDiagnosis(
        reason=NewsEmptyReason.NO_ROWS_IN_DB,
        latest_ingested_at=None,
    )


def test_collector_inactive_when_latest_ingestion_is_before_window(session: Session) -> None:
    """A row ingested before ``window_start`` classifies as ``COLLECTOR_INACTIVE``."""
    latest = WINDOW_START - timedelta(hours=3)
    _add_article(
        session,
        article_id="old-row",
        published_at=latest - timedelta(hours=1),
        ingested_at=latest,
    )
    session.flush()

    diagnosis = diagnose_empty_news(session, window_start=WINDOW_START)
    assert diagnosis.reason is NewsEmptyReason.COLLECTOR_INACTIVE
    assert diagnosis.latest_ingested_at == latest


def test_no_headlines_in_window_when_latest_ingestion_is_in_window(session: Session) -> None:
    """A row ingested inside the window classifies as ``NO_HEADLINES_IN_WINDOW``."""
    latest = WINDOW_START + timedelta(hours=2)
    _add_article(
        session,
        article_id="recent-row",
        published_at=latest - timedelta(hours=1),
        ingested_at=latest,
    )
    session.flush()

    diagnosis = diagnose_empty_news(session, window_start=WINDOW_START)
    assert diagnosis.reason is NewsEmptyReason.NO_HEADLINES_IN_WINDOW
    assert diagnosis.latest_ingested_at == latest
