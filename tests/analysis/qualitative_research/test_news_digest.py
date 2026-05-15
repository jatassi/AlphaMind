"""Tests for the qualitative-research news-digest renderer — ALP-248.

Each test exercises :func:`render_news_digest` against an in-memory SQLite
fixture so the read paths run for real. The acceptance criteria for the
story map one-to-one onto the test functions below.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.analysis._shared import Sector
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    EarningsEventDetails,
    EventCalendar,
    NewsArticleClusters,
    NewsArticles,
    NewsArticleTickers,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
LAST_INVOCATION = AS_OF - timedelta(hours=6)
INVOCATION_ID = "inv-test-0001"


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


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_ticker(
    session: Session, ticker: str, *, sector: str = "tech_semis", domain: str = "tech_semis"
) -> None:
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
    session.flush()
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=asset_id,
            alphamind_sector=sector,
            domain_researcher=domain,
            sector_etf="XLK",
            classification_source="test",
            last_updated="2026-01-01T00:00:00Z",
        )
    )


def _add_article(
    session: Session,
    article_id: str,
    *,
    headline: str,
    published_at: datetime,
    source_outlet: str = "Reuters",
    tier: str = "tier_2",
    topic_tags: tuple[str, ...] = (),
    cluster_id: str | None = None,
    tickers: tuple[str, ...] = (),
) -> None:
    session.add(
        NewsArticles(
            article_id=article_id,
            source="rss",
            source_outlet=source_outlet,
            source_credibility_tier=tier,
            url=f"https://example.com/{article_id}",
            language="en",
            headline_text=headline,
            body_path=None,
            published_at=_iso(published_at),
            ingested_at=_iso(published_at),
            topic_tags=json.dumps(list(topic_tags)) if topic_tags else None,
            cross_ticker_cluster_id=cluster_id,
        )
    )
    session.flush()
    for idx, ticker in enumerate(tickers):
        session.add(
            NewsArticleTickers(
                article_id=article_id,
                ticker=ticker,
                is_primary=1 if idx == 0 else 0,
            )
        )


def _add_cluster(
    session: Session,
    cluster_id: str,
    *,
    primary_theme: str,
    headline_count: int,
    first_seen: datetime,
    tickers: tuple[str, ...] = (),
) -> None:
    session.add(
        NewsArticleClusters(
            cluster_id=cluster_id,
            primary_theme=primary_theme,
            headline_count=headline_count,
            first_seen_at=_iso(first_seen),
            last_seen_at=_iso(first_seen),
            tickers_involved=json.dumps(list(tickers)),
            sealed_at=_iso(first_seen + timedelta(hours=24)),
        )
    )


_DEFAULT_ROSTER = {
    Sector.TECH_SEMIS: frozenset({"NVDA", "AMD", "AAPL"}),
    Sector.FINANCIALS: frozenset({"JPM", "GS"}),
    Sector.ENERGY: frozenset({"XOM", "CVX"}),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_imports_resolve() -> None:
    from alphamind.analysis.qualitative_research.news_digest import (
        DigestEntry,
        NewsDigest,
        render_news_digest,
    )

    assert callable(render_news_digest)
    assert NewsDigest is not None
    assert DigestEntry is not None


def test_renders_section_markers_for_populated_input(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_article(
        session,
        "art-tech-1",
        headline="NVDA unveils new GPU lineup",
        published_at=AS_OF - timedelta(hours=2),
        tickers=("NVDA",),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    text = digest.digest_text
    assert "=== NEWS DIGEST" in text
    assert "--- MACRO / CROSS-SECTOR" in text
    assert "--- TECH / SEMIS" in text
    assert "--- FINANCIALS" in text
    assert "--- ENERGY" in text
    assert "--- EARNINGS CALLS SINCE LAST INVOCATION" in text
    assert "--- HIGH-PRIORITY FLAGS" in text


def test_reference_ids_sequential_within_section(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_ticker(session, "AMD", sector="tech_semis", domain="tech_semis")
    _add_ticker(session, "AAPL", sector="tech_semis", domain="tech_semis")
    for i in range(3):
        _add_article(
            session,
            f"art-tech-{i}",
            headline=f"Tech update {i}",
            published_at=AS_OF - timedelta(hours=1, minutes=i * 10),
            tickers=("NVDA",),
        )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    text = digest.digest_text
    assert "[ND-T1]" in text
    assert "[ND-T2]" in text
    assert "[ND-T3]" in text
    # The fourth slot should not appear because there are only three.
    assert "[ND-T4]" not in text
    # Ordering: ND-T1 must precede ND-T2 must precede ND-T3 in the body.
    pos_t1 = text.index("[ND-T1]")
    pos_t2 = text.index("[ND-T2]")
    pos_t3 = text.index("[ND-T3]")
    assert pos_t1 < pos_t2 < pos_t3


def test_cluster_of_4_syndicated_headlines_yields_one_entry_at_top_tier(
    session: Session,
) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    cluster_id = "cluster-syndicated-1"
    _add_cluster(
        session,
        cluster_id,
        primary_theme="breaking",
        headline_count=4,
        first_seen=AS_OF - timedelta(hours=2),
        tickers=("NVDA",),
    )
    # Four near-identical syndicated copies at different tiers.
    _add_article(
        session,
        "art-syn-tier3",
        headline="NVDA blockbuster GPU launch announced",
        published_at=AS_OF - timedelta(hours=2),
        source_outlet="MarketWatch",
        tier="tier_3",
        cluster_id=cluster_id,
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-syn-tier2",
        headline="NVDA blockbuster GPU launch announced",
        published_at=AS_OF - timedelta(hours=2, minutes=1),
        source_outlet="CNBC",
        tier="tier_2",
        cluster_id=cluster_id,
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-syn-tier1",
        headline="NVDA blockbuster GPU launch announced",
        published_at=AS_OF - timedelta(hours=2, minutes=2),
        source_outlet="Reuters",
        tier="tier_1",
        cluster_id=cluster_id,
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-syn-tier3-b",
        headline="NVDA blockbuster GPU launch announced",
        published_at=AS_OF - timedelta(hours=2, minutes=3),
        source_outlet="Yahoo",
        tier="tier_3",
        cluster_id=cluster_id,
        tickers=("NVDA",),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    cluster_entries = [e for e in digest.entries if e.cluster_id == cluster_id]
    assert len(cluster_entries) == 1
    assert cluster_entries[0].tier == "tier_1"
    assert cluster_entries[0].source_outlet == "Reuters"


def test_tier_1_outranks_tier_2_when_else_equal(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    common_time = AS_OF - timedelta(hours=2)
    _add_article(
        session,
        "art-tier2",
        headline="Tech sector gains continue",
        published_at=common_time,
        tier="tier_2",
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-tier1",
        headline="Tech sector reports robust growth",
        published_at=common_time,
        tier="tier_1",
        tickers=("NVDA",),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    tech_entries = [e for e in digest.entries if e.reference_id.startswith("ND-T")]
    assert tech_entries[0].tier == "tier_1"
    assert tech_entries[1].tier == "tier_2"


def test_recency_is_monotonic_in_published_at(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    older = AS_OF - timedelta(hours=5)
    middle = AS_OF - timedelta(hours=3)
    newer = AS_OF - timedelta(minutes=30)
    _add_article(
        session, "art-old", headline="A older headline", published_at=older, tickers=("NVDA",)
    )
    _add_article(
        session, "art-mid", headline="B middle headline", published_at=middle, tickers=("NVDA",)
    )
    _add_article(
        session, "art-new", headline="C newer headline", published_at=newer, tickers=("NVDA",)
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    tech_entries = [e for e in digest.entries if e.reference_id.startswith("ND-T")]
    timestamps = [e.published_at for e in tech_entries]
    # Newest first — recency factor strictly decreases with elapsed lag.
    assert timestamps == sorted(timestamps, reverse=True)


def test_universe_ticker_mention_applies_boost(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import (
        UNIVERSE_TICKER_BOOST,
        render_news_digest,
    )

    # Both articles route to MACRO: one is ticker-less with a macro tag, the
    # other mentions a universe ticker (cross-sector → MACRO). The
    # boost makes the ticker-mention article win on score with all else equal.
    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_ticker(session, "JPM", sector="financials", domain="financials")
    common_time = AS_OF - timedelta(hours=2)
    _add_article(
        session,
        "art-no-ticker",
        headline="Macro inflation print at decade lows",
        published_at=common_time,
        tier="tier_2",
        topic_tags=("macro_data",),
        tickers=(),
    )
    _add_article(
        session,
        "art-with-ticker",
        headline="Cross-sector deal between NVDA and JPM unfolds",
        published_at=common_time,
        tier="tier_2",
        topic_tags=("breaking",),
        tickers=("NVDA", "JPM"),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    # Universe-mentioning article must outrank the ticker-less one in MACRO.
    macro_entries = [e for e in digest.entries if e.reference_id.startswith("ND-M")]
    assert macro_entries[0].headline == "Cross-sector deal between NVDA and JPM unfolds"
    assert macro_entries[1].headline == "Macro inflation print at decade lows"
    # The boost constant must be a non-trivial multiplier.
    assert UNIVERSE_TICKER_BOOST > 1.0


def test_high_priority_section_caps_at_three(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import (
        HIGH_PRIORITY_MAX,
        render_news_digest,
    )

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_ticker(session, "JPM", sector="financials", domain="financials")
    _add_ticker(session, "XOM", sector="energy", domain="energy")
    # Five tier-1 short-report headlines (eligible for high-priority).
    for i in range(5):
        _add_article(
            session,
            f"art-hp-{i}",
            headline=f"Activist short report {i}",
            published_at=AS_OF - timedelta(minutes=10 + i),
            source_outlet="Reuters",
            tier="tier_1",
            topic_tags=("short_report",),
            tickers=("NVDA",),
        )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    high_priority_entries = [e for e in digest.entries if e.reference_id.startswith("ND-HP")]
    assert len(high_priority_entries) == HIGH_PRIORITY_MAX
    assert HIGH_PRIORITY_MAX == 3


def test_high_priority_items_also_appear_in_sector_bucket(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    # One tier-1 short-report headline tagged into TECH_SEMIS via NVDA.
    _add_article(
        session,
        "art-dual",
        headline="Activist short report on NVDA",
        published_at=AS_OF - timedelta(minutes=15),
        source_outlet="Reuters",
        tier="tier_1",
        topic_tags=("short_report",),
        tickers=("NVDA",),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    # The headline must appear once in TECH/SEMIS (top-5) and once in HIGH-PRIORITY.
    tech_entries = [e for e in digest.entries if e.reference_id.startswith("ND-T")]
    hp_entries = [e for e in digest.entries if e.reference_id.startswith("ND-HP")]
    assert len(tech_entries) == 1
    assert len(hp_entries) == 1
    assert tech_entries[0].headline == hp_entries[0].headline
    # Distinct reference IDs but same source headline.
    assert tech_entries[0].reference_id != hp_entries[0].reference_id


def test_earnings_section_empty_when_no_universe_reports(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_article(
        session,
        "art-tech-1",
        headline="Tech sector update",
        published_at=AS_OF - timedelta(hours=1),
        tickers=("NVDA",),
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    # No earnings entries — section header is present but no ND-EC* lines.
    assert "EARNINGS CALLS SINCE LAST INVOCATION" in digest.digest_text
    assert "[ND-EC1]" not in digest.digest_text


def test_earnings_section_renders_when_universe_reports(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    reported_at = AS_OF - timedelta(hours=2)
    session.add(
        EventCalendar(
            event_id="evt-nvda-earnings",
            event_type="earnings",
            ticker=Symbol("NVDA"),
            scheduled_at=_iso(reported_at),
            description="NVDA Q1 earnings",
            status="completed",
            source="finnhub",
            ingested_at=_iso(AS_OF),
            last_updated=_iso(AS_OF),
        )
    )
    session.flush()
    session.add(
        EarningsEventDetails(
            event_id="evt-nvda-earnings",
            ticker=Symbol("NVDA"),
            fiscal_period="Q1",
            fiscal_year=2026,
            eps_consensus=4.25,
            eps_actual=4.50,
            revenue_consensus_usd=20_000_000_000.0,
            revenue_actual_usd=21_500_000_000.0,
            reported_at=_iso(reported_at),
            source="finnhub",
        )
    )
    session.commit()

    digest = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    text = digest.digest_text
    assert "[ND-EC1]" in text
    assert "NVDA reported" in text
    assert "$4.50" in text
    assert "$4.25" in text


def test_byte_identical_digest_text_on_identical_inputs(session: Session) -> None:
    from alphamind.analysis.qualitative_research.news_digest import render_news_digest

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    _add_ticker(session, "JPM", sector="financials", domain="financials")
    _add_ticker(session, "XOM", sector="energy", domain="energy")
    base_time = AS_OF - timedelta(hours=2)
    # Mix of buckets, plus an earnings row, plus a high-priority headline.
    _add_article(
        session,
        "art-nvda",
        headline="NVDA chip launch",
        published_at=base_time,
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-jpm",
        headline="JPM banking news",
        published_at=base_time + timedelta(minutes=15),
        tickers=("JPM",),
    )
    _add_article(
        session,
        "art-xom",
        headline="XOM oil moves",
        published_at=base_time + timedelta(minutes=30),
        tickers=("XOM",),
    )
    _add_article(
        session,
        "art-hp",
        headline="Activist short report on NVDA",
        published_at=base_time + timedelta(minutes=45),
        tier="tier_1",
        topic_tags=("short_report",),
        tickers=("NVDA",),
    )
    _add_article(
        session,
        "art-macro",
        headline="Macro inflation print arrives",
        published_at=base_time + timedelta(hours=1),
        topic_tags=("macro_data",),
    )
    session.add(
        EventCalendar(
            event_id="evt-nvda-earnings",
            event_type="earnings",
            ticker=Symbol("NVDA"),
            scheduled_at=_iso(base_time),
            description="NVDA Q1 earnings",
            status="completed",
            source="finnhub",
            ingested_at=_iso(AS_OF),
            last_updated=_iso(AS_OF),
        )
    )
    session.flush()
    session.add(
        EarningsEventDetails(
            event_id="evt-nvda-earnings",
            ticker=Symbol("NVDA"),
            fiscal_period="Q1",
            fiscal_year=2026,
            eps_consensus=4.25,
            eps_actual=4.50,
            revenue_consensus_usd=20_000_000_000.0,
            revenue_actual_usd=21_500_000_000.0,
            reported_at=_iso(base_time),
            source="finnhub",
        )
    )
    session.commit()

    first = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )
    second = render_news_digest(
        session,
        invocation_id=INVOCATION_ID,
        as_of=AS_OF,
        last_invocation_time=LAST_INVOCATION,
        sector_roster=_DEFAULT_ROSTER,
    )

    assert first.digest_text == second.digest_text
    # The bytes must match too — guards against subtle whitespace drift.
    assert first.digest_text.encode("utf-8") == second.digest_text.encode("utf-8")


def test_soft_cap_logs_warning_without_raising(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    from alphamind.analysis.qualitative_research import news_digest as nd_module
    from alphamind.analysis.qualitative_research.news_digest import (
        DIGEST_TOTAL_TOKEN_SOFT_CAP,
        render_news_digest,
    )

    _add_ticker(session, "NVDA", sector="tech_semis", domain="tech_semis")
    # Long headlines push the rendered text past the soft cap.
    long_text = "really very long headline padding " * 200
    for i in range(5):
        _add_article(
            session,
            f"art-long-{i}",
            headline=f"{long_text}{i}",
            published_at=AS_OF - timedelta(minutes=10 + i),
            tickers=("NVDA",),
        )
    session.commit()

    with caplog.at_level(logging.INFO, logger=nd_module.__name__):
        digest = render_news_digest(
            session,
            invocation_id=INVOCATION_ID,
            as_of=AS_OF,
            last_invocation_time=LAST_INVOCATION,
            sector_roster=_DEFAULT_ROSTER,
        )

    assert digest.total_shown >= 1
    # Warning must mention the cap and not blow up.
    cap_log_messages = [r.getMessage() for r in caplog.records if "soft cap" in r.getMessage()]
    assert cap_log_messages, "expected a soft-cap warning to be emitted"
    assert str(DIGEST_TOTAL_TOKEN_SOFT_CAP) in cap_log_messages[0]


def test_all_weights_and_caps_are_named_module_constants() -> None:
    """No magic numbers — every weight and cap must be a named module attribute."""
    from alphamind.analysis.qualitative_research import news_digest as nd_module

    required = (
        "TIER_SCORE_BY_TIER",
        "RECENCY_WEIGHT",
        "UNIVERSE_TICKER_BOOST",
        "CLUSTER_SIZE_WEIGHT",
        "TOP_N_PER_SECTOR_BUCKET",
        "HIGH_PRIORITY_MAX",
        "DIGEST_TOTAL_TOKEN_SOFT_CAP",
    )
    for name in required:
        assert hasattr(nd_module, name), f"missing module-level constant {name}"
    # Sanity-check the values match the design-doc § Step 2 weights.
    assert nd_module.TIER_SCORE_BY_TIER == {"tier_1": 3.0, "tier_2": 2.0, "tier_3": 1.0}
    assert nd_module.UNIVERSE_TICKER_BOOST == 1.5
    assert nd_module.TOP_N_PER_SECTOR_BUCKET == 5
    assert nd_module.HIGH_PRIORITY_MAX == 3
    assert nd_module.DIGEST_TOTAL_TOKEN_SOFT_CAP == 1200
