"""Per-sector qualitative input loader — story ALP-189 parts F + G.

Reads sector-specific qualitative slices (recent news headlines, near-term
events) from the data layer and returns a typed ``SectorQualitativeInput``
record. Story 08's input-bundle assembler renders the value object into the
LLM's user message.

Design contract: ``docs/design/03-analysis-layer/domain-researchers/`` per-sector
``Inputs`` sections name the qualitative-data slices each researcher consumes.
``docs/design/01-data-layer/external/qualitative.md`` § 6 names the
sector-relevant signal taxonomy used for the per-sector tag filter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.analysis._shared import _SECTOR_AUDIENCE_MAP, Sector
from alphamind.data_sources._common import HeadlineType
from alphamind.distillation.sector_assembly import (
    DOMAIN_RESEARCHER_BY_AUDIENCE,
    load_sector_roster,
)
from alphamind.persistence.models import (
    EarningsEventDetails,
    EventCalendar,
    NewsArticles,
    NewsArticleTickers,
)

# ---------------------------------------------------------------------------
# Per-sector relevant-tag sets — qualitative.md § 6.
# Adding a tag is a one-line change; the sets are intentionally small.
# ---------------------------------------------------------------------------

_TECH_SEMIS_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.SUPPLY_CHAIN,
        HeadlineType.PRODUCT_LAUNCH,
        HeadlineType.REGULATORY,
        HeadlineType.ANALYST_ACTION,
        HeadlineType.M_AND_A,
        HeadlineType.GUIDANCE,
        HeadlineType.EARNINGS_RELATED,
    }
)

_FINANCIALS_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.REGULATORY,
        HeadlineType.MACRO_DATA,
        HeadlineType.M_AND_A,
        HeadlineType.ANALYST_ACTION,
        HeadlineType.GUIDANCE,
        HeadlineType.EARNINGS_RELATED,
    }
)

_ENERGY_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.GEOPOLITICAL,
        HeadlineType.MACRO_DATA,
        HeadlineType.REGULATORY,
        HeadlineType.ANALYST_ACTION,
        HeadlineType.M_AND_A,
        HeadlineType.GUIDANCE,
        HeadlineType.EARNINGS_RELATED,
    }
)

_SECTOR_TAGS: dict[Sector, frozenset[HeadlineType]] = {
    Sector.TECH_SEMIS: _TECH_SEMIS_TAGS,
    Sector.FINANCIALS: _FINANCIALS_TAGS,
    Sector.ENERGY: _ENERGY_TAGS,
}

# ---------------------------------------------------------------------------
# Composite-score weights — qualitative.md § News digest ranking.
# ---------------------------------------------------------------------------

_TIER_SCORE: dict[str, float] = {"tier_1": 3.0, "tier_2": 2.0, "tier_3": 1.0}
_TIER_VALUES: frozenset[str] = frozenset(_TIER_SCORE)
_TIER_DEFAULT = "tier_3"
_TICKER_MENTION_BOOST = 1.5

# Forward event window — story body § Event selection.
_EVENT_FORWARD_HOURS = 72


# ---------------------------------------------------------------------------
# Pydantic records
# ---------------------------------------------------------------------------


class HeadlineEntry(BaseModel, frozen=True):
    """One headline row in the assembled qualitative input.

    ``tags`` is typed against the canonical taxonomy so ill-formed values
    fail at construction rather than at filter time downstream.
    ``tier`` is a Literal because the storage column is TEXT with these
    exact strings — coercing through an enum adds drift surface without
    buying type-safety the Literal doesn't already provide.
    """

    headline: str
    source_outlet: str
    tier: Literal["tier_1", "tier_2", "tier_3"]
    published_at: datetime
    tickers: tuple[str, ...]
    tags: tuple[HeadlineType, ...]


class EventEntry(BaseModel, frozen=True):
    """One event-calendar entry, with tickers aggregated across rows sharing event_id."""

    event_id: str
    event_name: str
    event_time: datetime
    sectors: frozenset[Sector]
    tickers: tuple[str, ...]
    consensus: str | None


class SectorQualitativeInput(BaseModel, frozen=True):
    """The full qualitative slice a domain researcher consumes for one invocation."""

    sector: Sector
    as_of: datetime
    lookback_window_hours: int
    headlines: tuple[HeadlineEntry, ...]
    events: tuple[EventEntry, ...]
    data_freshness: datetime


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_sector_qualitative_input(
    session: Session,
    sector: Sector,
    as_of: datetime,
    *,
    lookback_window_hours: int = 24,
    max_headlines: int = 30,
) -> SectorQualitativeInput:
    """Read the per-sector qualitative slice and return a typed ``SectorQualitativeInput``.

    Headlines are pulled from ``news_articles`` joined to ``news_article_tickers``,
    filtered to the ``[as_of - lookback_window_hours, as_of]`` window, and kept
    when either (a) at least one mentioned ticker is in the sector roster, or
    (b) at least one parsed canonical tag is in the sector's relevant-tag set.
    Composite-score ranking weights tier x recency x ticker-mention boost; the
    top ``max_headlines`` survive.

    Events come from ``event_calendar`` rows scheduled in
    ``[as_of, as_of + 72h]`` whose ``sectors`` column either parses to include
    this sector or is NULL/empty (cross-sector default). Rows sharing an
    ``event_id`` are grouped; tickers are aggregated. Earnings events join
    ``earnings_event_details`` for a rendered ``consensus`` string.
    """
    roster_set = frozenset(_load_roster(session, sector))
    relevant_tags = _SECTOR_TAGS[sector]

    headlines = _select_headlines(
        session,
        as_of=as_of,
        lookback_window_hours=lookback_window_hours,
        roster=roster_set,
        relevant_tags=relevant_tags,
        max_headlines=max_headlines,
    )
    events = _select_events(session, sector=sector, as_of=as_of)
    freshness = _data_freshness(session, as_of=as_of, lookback_window_hours=lookback_window_hours)

    return SectorQualitativeInput(
        sector=sector,
        as_of=as_of,
        lookback_window_hours=lookback_window_hours,
        headlines=headlines,
        events=events,
        data_freshness=freshness,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_roster(session: Session, sector: Sector) -> tuple[str, ...]:
    """Return the sector's ticker roster via the canonical reader."""
    audience = _SECTOR_AUDIENCE_MAP[sector]
    return load_sector_roster(session, DOMAIN_RESEARCHER_BY_AUDIENCE[audience])


def _parse_topic_tags(raw: str | None) -> tuple[HeadlineType, ...]:
    """Parse JSON-encoded topic_tags into canonical HeadlineType members.

    Defensive against malformed JSON (historical rows may carry vendor-raw
    text) and against strings that don't resolve to a known HeadlineType.
    """
    if raw is None:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    out: list[HeadlineType] = []
    for tag in parsed:
        if not isinstance(tag, str):
            continue
        try:
            out.append(HeadlineType(tag))
        except ValueError:
            continue
    return tuple(out)


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str) -> datetime:
    """Parse a stored timestamp, treating ``Z`` as UTC and naive values as UTC."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _select_headlines(
    session: Session,
    *,
    as_of: datetime,
    lookback_window_hours: int,
    roster: frozenset[str],
    relevant_tags: frozenset[HeadlineType],
    max_headlines: int,
) -> tuple[HeadlineEntry, ...]:
    """Select, score, rank, and truncate the per-sector headlines."""
    window_start = as_of - timedelta(hours=lookback_window_hours)
    window_start_iso = _format_iso(window_start)
    as_of_iso = _format_iso(as_of)

    # Pull every article in the lookback window; SQL-side narrowing on the
    # tag/ticker overlap is impractical with topic_tags stored as JSON-text.
    # Filter and score in Python — the lookback window is bounded (default
    # 24h) so the row count stays small.
    article_rows = session.execute(
        select(
            NewsArticles.article_id,
            NewsArticles.headline_text,
            NewsArticles.source_outlet,
            NewsArticles.source_credibility_tier,
            NewsArticles.published_at,
            NewsArticles.topic_tags,
        ).where(
            NewsArticles.published_at >= window_start_iso,
            NewsArticles.published_at <= as_of_iso,
        )
    ).all()

    if not article_rows:
        return ()

    # One round-trip for tickers; loader keeps only roster matches.
    article_ids = [row.article_id for row in article_rows]
    ticker_rows = session.execute(
        select(NewsArticleTickers.article_id, NewsArticleTickers.ticker).where(
            NewsArticleTickers.article_id.in_(article_ids)
        )
    ).all()
    tickers_by_article: dict[str, list[str]] = {}
    for article_id, ticker in ticker_rows:
        tickers_by_article.setdefault(article_id, []).append(ticker)

    scored: list[tuple[float, datetime, HeadlineEntry]] = []
    for row in article_rows:
        article_tickers = tuple(tickers_by_article.get(row.article_id, ()))
        ticker_mention_match = any(t in roster for t in article_tickers)
        tags = _parse_topic_tags(row.topic_tags)
        relevant_tag_match = bool(set(tags) & relevant_tags)
        if not ticker_mention_match and not relevant_tag_match:
            continue

        published_at = _parse_iso(row.published_at)
        raw_tier = row.source_credibility_tier
        tier = cast(
            Literal["tier_1", "tier_2", "tier_3"],
            raw_tier if raw_tier in _TIER_VALUES else _TIER_DEFAULT,
        )
        score = _composite_score(
            tier=tier,
            published_at=published_at,
            as_of=as_of,
            lookback_window_hours=lookback_window_hours,
            ticker_mention_match=ticker_mention_match,
        )
        entry = HeadlineEntry(
            headline=row.headline_text,
            source_outlet=row.source_outlet or "",
            tier=tier,
            published_at=published_at,
            tickers=article_tickers,
            tags=tags,
        )
        scored.append((score, published_at, entry))

    # Sort: composite descending, then published_at descending as tiebreaker.
    scored.sort(key=lambda item: (-item[0], -item[1].timestamp()))
    return tuple(entry for _, _, entry in scored[:max_headlines])


def _composite_score(
    *,
    tier: str,
    published_at: datetime,
    as_of: datetime,
    lookback_window_hours: int,
    ticker_mention_match: bool,
) -> float:
    """Score = (tier + recency) x ticker-mention multiplier."""
    tier_score = _TIER_SCORE.get(tier, _TIER_SCORE["tier_3"])
    age_hours = max((as_of - published_at).total_seconds() / 3600.0, 0.0)
    recency = max(1.0 - (age_hours / lookback_window_hours), 0.0)
    base = tier_score + recency
    return base * _TICKER_MENTION_BOOST if ticker_mention_match else base


@dataclass
class _EventBucket:
    """Mutable accumulator while grouping ``event_calendar`` rows by event_id."""

    event_type: str
    description: str
    scheduled_at: datetime
    sectors: frozenset[Sector]
    tickers: list[str] = field(default_factory=list)


def _select_events(session: Session, *, sector: Sector, as_of: datetime) -> tuple[EventEntry, ...]:
    """Select events in the 72h forward window; group by event_id; aggregate tickers."""
    window_start_iso = _format_iso(as_of)
    window_end_iso = _format_iso(as_of + timedelta(hours=_EVENT_FORWARD_HOURS))

    rows = session.execute(
        select(
            EventCalendar.event_id,
            EventCalendar.event_type,
            EventCalendar.ticker,
            EventCalendar.scheduled_at,
            EventCalendar.description,
            EventCalendar.sectors,
        ).where(
            EventCalendar.scheduled_at >= window_start_iso,
            EventCalendar.scheduled_at <= window_end_iso,
        )
    ).all()

    grouped: dict[str, _EventBucket] = {}
    for row in rows:
        sectors_set = _parse_event_sectors(row.sectors)
        # Cross-sector (NULL/empty) rows surface to every sector; sector-tagged
        # rows must include this sector.
        if sectors_set and sector not in sectors_set:
            continue
        bucket = grouped.setdefault(
            row.event_id,
            _EventBucket(
                event_type=row.event_type,
                description=row.description or "",
                scheduled_at=_parse_iso(row.scheduled_at),
                sectors=sectors_set,
            ),
        )
        if row.ticker and row.ticker not in bucket.tickers:
            bucket.tickers.append(row.ticker)

    if not grouped:
        return ()

    earnings_consensus = _earnings_consensus_map(session, list(grouped))

    entries = [
        EventEntry(
            event_id=event_id,
            event_name=bucket.description,
            event_time=bucket.scheduled_at,
            sectors=bucket.sectors,
            tickers=tuple(bucket.tickers),
            consensus=(
                earnings_consensus.get(event_id) if bucket.event_type == "earnings" else None
            ),
        )
        for event_id, bucket in grouped.items()
    ]
    entries.sort(key=lambda e: e.event_time)
    return tuple(entries)


def _parse_event_sectors(raw: str | None) -> frozenset[Sector]:
    """Parse the comma-separated sectors column into a frozenset of ``Sector``.

    Empty / NULL → empty set (cross-sector). Unknown labels are silently
    skipped (defensive — a stray comma or whitespace token won't crash).
    """
    if not raw:
        return frozenset()
    out: set[Sector] = set()
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            out.add(Sector(cleaned))
        except ValueError:
            continue
    return frozenset(out)


def _earnings_consensus_map(session: Session, event_ids: list[str]) -> dict[str, str]:
    """Return ``{event_id: rendered consensus string}`` for earnings events."""
    if not event_ids:
        return {}
    rows = session.execute(
        select(
            EarningsEventDetails.event_id,
            EarningsEventDetails.eps_consensus,
            EarningsEventDetails.revenue_consensus_usd,
        ).where(EarningsEventDetails.event_id.in_(event_ids))
    ).all()
    return {
        row.event_id: _render_consensus(row.eps_consensus, row.revenue_consensus_usd)
        for row in rows
    }


def _render_consensus(eps: float | None, revenue: float | None) -> str:
    """Render an earnings consensus string, e.g. ``"EPS $1.23, revenue $50.0B"``."""
    parts: list[str] = []
    if eps is not None:
        parts.append(f"EPS ${eps:.2f}")
    if revenue is not None:
        parts.append(f"revenue ${revenue / 1e9:.1f}B")
    return ", ".join(parts) if parts else ""


def _data_freshness(session: Session, *, as_of: datetime, lookback_window_hours: int) -> datetime:
    """Latest ``ingested_at`` across news_articles and event_calendar.

    When neither table has rows, the documented choice is to return the
    lookback floor — gives the caller an honest read on data age in the
    empty-DB case rather than a sentinel.
    """
    article_max = session.execute(select(func.max(NewsArticles.ingested_at))).scalar()
    event_max = session.execute(select(func.max(EventCalendar.ingested_at))).scalar()
    candidates = [v for v in (article_max, event_max) if v]
    if not candidates:
        return as_of - timedelta(hours=lookback_window_hours)
    return max(_parse_iso(v) for v in candidates)
