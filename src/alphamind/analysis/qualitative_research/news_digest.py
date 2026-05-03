"""News digest renderer for the qualitative researcher — ALP-248.

Reads clustered headlines from ``news_articles`` joined to
``news_article_clusters`` over a since-last-invocation window, applies the
design doc's three-step ranking algorithm (deduplication, composite scoring,
top-N selection per sector bucket), and produces the structured digest text
the qualitative researcher consumes as in-context input.

See ``docs/design/03-analysis-layer/qualitative-research.md`` § News digest
for the algorithmic and rendering specifications.

Public names
------------
- :class:`DigestEntry`
- :class:`NewsDigest`
- :func:`render_news_digest`
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.analysis._shared import Sector
from alphamind.data_sources._common import HeadlineType
from alphamind.persistence.models import (
    EarningsEventDetails,
    EventCalendar,
    NewsArticleClusters,
    NewsArticles,
    NewsArticleTickers,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Composite-score weights and section caps
# ---------------------------------------------------------------------------
#
# Every weight below is a named module constant per the parent issue's
# "no-magic-numbers" invariant. Each docstring quotes the design-doc line
# motivating the value so future tuning happens in this file rather than
# inline in the algorithm.

TIER_SCORE_BY_TIER: Mapping[str, float] = {
    "tier_1": 3.0,
    "tier_2": 2.0,
    "tier_3": 1.0,
}
"""Source-credibility tier scores per § Ranking algorithm § Step 2.

"Tier 1 = 3, Tier 2 = 2, Tier 3 = 1" — the highest-tier source on a cluster
wins the cluster's representative slot, and tier feeds Step 2's composite
score.
"""


RECENCY_WEIGHT: float = 1.0
"""Weight applied to the linear-decay recency factor per § Ranking algorithm
§ Step 2: "Recency: Linear decay from invocation time to last invocation
time." Recency contributes a factor in ``[0, 1]`` linearly, scaled by this
weight before summing into the composite score.
"""


UNIVERSE_TICKER_BOOST: float = 1.5
"""Multiplicative boost applied when the headline mentions a universe ticker
per § Ranking algorithm § Step 2: "Universe ticker mention: Headline
directly mentions a universe ticker = 1.5x boost."
"""


CLUSTER_SIZE_WEIGHT: float = 0.5
"""Log-scaled weight applied to the cluster's ``headline_count`` per
§ Ranking algorithm § Step 2: "Cluster size: More articles covering the
same event = higher score (log-scaled to prevent runaway dominance)."
"""


TOP_N_PER_SECTOR_BUCKET: int = 5
"""Maximum surviving entries per sector bucket per § Ranking algorithm
§ Step 3: "Take the top 5 per sector bucket by composite score." Fewer
entries are emitted when the bucket has fewer matches; no padding.
"""


HIGH_PRIORITY_MAX: int = 3
"""High-priority section cap per § Sections: "High-priority flags: Items
visible regardless of sector-bucket slots. Capped at 3."
"""


DIGEST_TOTAL_TOKEN_SOFT_CAP: int = 1200
"""Soft cap on the rendered digest's approximate token count per
§ Token budget: "~1,000-1,200 tokens under all market conditions." Renderer
emits a warning when the heuristic-estimated count exceeds this cap;
content is not truncated.
"""


# ---------------------------------------------------------------------------
# Section labels and reference-ID prefixes
# ---------------------------------------------------------------------------

_SECTION_HEADER_BY_BUCKET: Mapping[Sector | str, str] = {
    "macro": "MACRO / CROSS-SECTOR",
    Sector.TECH_SEMIS: "TECH / SEMIS",
    Sector.FINANCIALS: "FINANCIALS",
    Sector.ENERGY: "ENERGY",
}

_REFERENCE_PREFIX_BY_BUCKET: Mapping[Sector | str, str] = {
    "macro": "ND-M",
    Sector.TECH_SEMIS: "ND-T",
    Sector.FINANCIALS: "ND-F",
    Sector.ENERGY: "ND-E",
}

# Fixed render order for the four sector-style buckets.
_SECTOR_BUCKET_ORDER: tuple[Sector | str, ...] = (
    "macro",
    Sector.TECH_SEMIS,
    Sector.FINANCIALS,
    Sector.ENERGY,
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


_TIER_LITERAL = Literal["tier_1", "tier_2", "tier_3"]


class DigestEntry(BaseModel, frozen=True):
    """One rendered headline carrying its assigned reference ID and metadata."""

    reference_id: str = Field(min_length=1)
    cluster_id: str | None
    published_at: datetime
    tier: _TIER_LITERAL
    headline: str
    source_outlet: str
    tickers: tuple[str, ...]
    tags: tuple[HeadlineType, ...]


class NewsDigest(BaseModel, frozen=True):
    """Renderer output — structured entries plus the rendered text body."""

    as_of: datetime
    last_invocation_time: datetime
    total_collected: int = Field(ge=0)
    total_shown: int = Field(ge=0)
    entries: tuple[DigestEntry, ...]
    digest_text: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    text = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _format_iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_topic_tags(raw: str | None) -> tuple[HeadlineType, ...]:
    """Decode ``news_articles.topic_tags`` into typed ``HeadlineType`` values.

    Mirrors :func:`alphamind.data_sources.news.clustering._parse_topic_tags`
    so the digest's ``tags`` field round-trips through the same canonical
    taxonomy. JSON-list and comma-separated legacy shapes both decode.
    """
    if raw is None or not raw.strip():
        return ()
    stripped = raw.strip()
    candidates: list[str]
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return ()
        candidates = [str(v) for v in decoded if isinstance(v, str)]
    else:
        candidates = [token.strip() for token in stripped.split(",") if token.strip()]
    out: list[HeadlineType] = []
    for tag in candidates:
        try:
            out.append(HeadlineType(tag))
        except ValueError:
            continue
    return tuple(out)


def _normalize_tier(raw: str | None) -> _TIER_LITERAL:
    """Map a raw tier string onto the canonical literal; fall back to tier_3."""
    if raw == "tier_1":
        return "tier_1"
    if raw == "tier_2":
        return "tier_2"
    return "tier_3"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Pre-parsed view of one ``news_articles`` row plus its cluster context."""

    article_id: str
    cluster_id: str | None
    cluster_size: int
    published_at: datetime
    tier: _TIER_LITERAL
    headline: str
    source_outlet: str
    tickers: tuple[str, ...]
    tags: tuple[HeadlineType, ...]


def _load_candidates(
    session: Session,
    *,
    last_invocation_time: datetime,
    as_of: datetime,
) -> list[_Candidate]:
    """Pull in-window ``news_articles`` rows and join cluster size + tickers."""
    range_start = _format_iso_utc(last_invocation_time)
    range_end = _format_iso_utc(as_of)

    article_rows = list(
        session.execute(
            select(NewsArticles)
            .where(
                NewsArticles.published_at >= range_start,
                NewsArticles.published_at <= range_end,
            )
            .order_by(NewsArticles.published_at, NewsArticles.article_id)
        )
        .scalars()
        .all()
    )
    if not article_rows:
        return []

    cluster_ids = sorted(
        {row.cross_ticker_cluster_id for row in article_rows if row.cross_ticker_cluster_id}
    )
    cluster_sizes: dict[str, int] = {}
    if cluster_ids:
        for cluster_id, headline_count in session.execute(
            select(NewsArticleClusters.cluster_id, NewsArticleClusters.headline_count).where(
                NewsArticleClusters.cluster_id.in_(tuple(cluster_ids))
            )
        ).all():
            cluster_sizes[cluster_id] = headline_count

    article_ids = [row.article_id for row in article_rows]
    ticker_rows = session.execute(
        select(
            NewsArticleTickers.article_id, NewsArticleTickers.ticker, NewsArticleTickers.is_primary
        )
        .where(NewsArticleTickers.article_id.in_(tuple(article_ids)))
        .order_by(NewsArticleTickers.is_primary.desc(), NewsArticleTickers.ticker)
    ).all()
    tickers_by_article: dict[str, list[str]] = {aid: [] for aid in article_ids}
    for article_id, ticker, _is_primary in ticker_rows:
        tickers_by_article[article_id].append(ticker)

    candidates: list[_Candidate] = []
    for row in article_rows:
        cluster_id = row.cross_ticker_cluster_id
        cluster_size = cluster_sizes.get(cluster_id, 1) if cluster_id else 1
        candidates.append(
            _Candidate(
                article_id=row.article_id,
                cluster_id=cluster_id,
                cluster_size=cluster_size,
                published_at=_parse_iso_utc(row.published_at),
                tier=_normalize_tier(row.source_credibility_tier),
                headline=row.headline_text,
                source_outlet=row.source_outlet or "",
                tickers=tuple(tickers_by_article.get(row.article_id, [])),
                tags=_decode_topic_tags(row.topic_tags),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Renderer entry point
# ---------------------------------------------------------------------------


def render_news_digest(
    session: Session,
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    sector_roster: Mapping[Sector, frozenset[str]],
) -> NewsDigest:
    """Render the deterministic news digest for one invocation.

    Pulls in-window ``news_articles`` rows, deduplicates per cluster, scores
    survivors via the composite-score weights at the top of this module,
    selects the top ``TOP_N_PER_SECTOR_BUCKET`` per sector bucket, and
    renders the design-doc § Format text body. Identical inputs produce a
    byte-identical ``digest_text``.
    """
    candidates = _load_candidates(session, last_invocation_time=last_invocation_time, as_of=as_of)
    total_collected = len(candidates)

    universe = _flatten_universe(sector_roster)
    deduplicated = _deduplicate_clusters(candidates)
    bucketed = _bucket_by_sector(deduplicated, sector_roster=sector_roster)
    selected = _select_top_per_bucket(
        bucketed,
        last_invocation_time=last_invocation_time,
        as_of=as_of,
        universe=universe,
    )

    high_priority = _select_high_priority(deduplicated)
    earnings_rows = _load_earnings_rows(
        session,
        last_invocation_time=last_invocation_time,
        as_of=as_of,
        universe=universe,
    )

    entries, sections = _assign_reference_ids_and_render_sections(
        bucket_selections=selected,
        high_priority=high_priority,
        earnings_rows=earnings_rows,
    )

    header = _render_header(
        invocation_id=invocation_id,
        as_of=as_of,
        last_invocation_time=last_invocation_time,
        total_collected=total_collected,
        total_shown=len(entries),
    )
    digest_text = header + "".join(sections)

    estimated_tokens = max(1, len(digest_text) // 4)
    if estimated_tokens > DIGEST_TOTAL_TOKEN_SOFT_CAP:
        logger.info(
            "news digest exceeds soft cap (estimated %d tokens, cap %d)",
            estimated_tokens,
            DIGEST_TOTAL_TOKEN_SOFT_CAP,
        )

    return NewsDigest(
        as_of=as_of,
        last_invocation_time=last_invocation_time,
        total_collected=total_collected,
        total_shown=len(entries),
        entries=tuple(entries),
        digest_text=digest_text,
    )


# ---------------------------------------------------------------------------
# Step 1 — deduplication
# ---------------------------------------------------------------------------


def _deduplicate_clusters(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """Step 1: retain the highest-credibility-tier member of each cluster.

    Headlines without a ``cluster_id`` are singleton clusters and pass through
    unchanged. Ties on tier resolve to the earliest ``published_at``; final
    tie-breaker is ``article_id`` for determinism.
    """
    by_cluster: dict[str, _Candidate] = {}
    singletons: list[_Candidate] = []
    for candidate in candidates:
        if candidate.cluster_id is None:
            singletons.append(candidate)
            continue
        existing = by_cluster.get(candidate.cluster_id)
        if existing is None or _is_stronger_representative(candidate, existing):
            by_cluster[candidate.cluster_id] = candidate
    return [*by_cluster.values(), *singletons]


def _is_stronger_representative(candidate: _Candidate, incumbent: _Candidate) -> bool:
    candidate_score = TIER_SCORE_BY_TIER[candidate.tier]
    incumbent_score = TIER_SCORE_BY_TIER[incumbent.tier]
    if candidate_score != incumbent_score:
        return candidate_score > incumbent_score
    if candidate.published_at != incumbent.published_at:
        return candidate.published_at < incumbent.published_at
    return candidate.article_id < incumbent.article_id


# ---------------------------------------------------------------------------
# Step 2 — bucket assignment
# ---------------------------------------------------------------------------


def _bucket_by_sector(
    candidates: Sequence[_Candidate],
    *,
    sector_roster: Mapping[Sector, frozenset[str]],
) -> dict[Sector | str, list[_Candidate]]:
    """Assign each deduplicated headline to one bucket.

    Per § Sections: single-sector headlines bucket to that sector;
    cross-sector and ticker-less headlines fall to MACRO.
    """
    buckets: dict[Sector | str, list[_Candidate]] = {bucket: [] for bucket in _SECTOR_BUCKET_ORDER}
    for candidate in candidates:
        buckets[_assign_bucket(candidate, sector_roster=sector_roster)].append(candidate)
    return buckets


def _assign_bucket(
    candidate: _Candidate,
    *,
    sector_roster: Mapping[Sector, frozenset[str]],
) -> Sector | str:
    matched = [
        sector
        for sector in (Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY)
        if any(t in sector_roster.get(sector, frozenset()) for t in candidate.tickers)
    ]
    if len(matched) == 1:
        return matched[0]
    return "macro"


# ---------------------------------------------------------------------------
# Step 2 — composite score
# ---------------------------------------------------------------------------


def _composite_score(
    candidate: _Candidate,
    *,
    last_invocation_time: datetime,
    as_of: datetime,
    universe: frozenset[str],
) -> float:
    tier_score = TIER_SCORE_BY_TIER[candidate.tier]
    recency = _recency_factor(candidate.published_at, last_invocation_time, as_of)
    cluster_factor = CLUSTER_SIZE_WEIGHT * math.log1p(max(0, candidate.cluster_size - 1))
    base = tier_score + RECENCY_WEIGHT * recency + cluster_factor
    if any(t in universe for t in candidate.tickers):
        return base * UNIVERSE_TICKER_BOOST
    return base


def _recency_factor(
    published_at: datetime,
    last_invocation_time: datetime,
    as_of: datetime,
) -> float:
    """Linear decay in ``[0, 1]`` from ``last_invocation_time`` to ``as_of``."""
    span = (as_of - last_invocation_time).total_seconds()
    if span <= 0:
        return 1.0
    elapsed = (published_at - last_invocation_time).total_seconds()
    if elapsed <= 0:
        return 0.0
    if elapsed >= span:
        return 1.0
    return elapsed / span


def _flatten_universe(sector_roster: Mapping[Sector, frozenset[str]]) -> frozenset[str]:
    """Union all sector rosters into a single universe ticker set."""
    universe: set[str] = set()
    for tickers in sector_roster.values():
        universe |= tickers
    return frozenset(universe)


# ---------------------------------------------------------------------------
# Step 3 — selection
# ---------------------------------------------------------------------------


def _select_top_per_bucket(
    bucketed: Mapping[Sector | str, list[_Candidate]],
    *,
    last_invocation_time: datetime,
    as_of: datetime,
    universe: frozenset[str],
) -> dict[Sector | str, list[_Candidate]]:
    out: dict[Sector | str, list[_Candidate]] = {}
    for bucket, members in bucketed.items():
        scored_members = [
            (
                _composite_score(
                    c,
                    last_invocation_time=last_invocation_time,
                    as_of=as_of,
                    universe=universe,
                ),
                c,
            )
            for c in members
        ]
        scored_members.sort(key=lambda pair: (-pair[0], pair[1].article_id))
        out[bucket] = [c for _score, c in scored_members[:TOP_N_PER_SECTOR_BUCKET]]
    return out


# ---------------------------------------------------------------------------
# High-priority eligibility
# ---------------------------------------------------------------------------


_HIGH_PRIORITY_TAGS: frozenset[HeadlineType] = frozenset(
    {
        HeadlineType.M_AND_A,
        HeadlineType.SHORT_REPORT,
        HeadlineType.ACTIVIST,
        HeadlineType.REGULATORY,
        HeadlineType.GEOPOLITICAL,
    }
)


def _is_high_priority(candidate: _Candidate) -> bool:
    """Return True when the headline is eligible for the high-priority section.

    Per § Scope § High-priority section: eligibility is the union of
    ``topic_tags`` ∈ ``HIGH_PRIORITY_TAGS`` plus ``tier == 'tier_1'`` plus
    a tag-specific rule. The ``m_and_a`` rule requires the headline text
    to indicate "rumor" rather than completed.
    """
    if not any(tag in _HIGH_PRIORITY_TAGS for tag in candidate.tags):
        return False
    if candidate.tier != "tier_1":
        return False
    # M&A headlines need a "rumor" signal — completed deals route through
    # the regular sector buckets, not the high-priority section.
    return not (
        HeadlineType.M_AND_A in candidate.tags and "rumor" not in candidate.headline.lower()
    )


def _select_high_priority(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    eligible = [c for c in candidates if _is_high_priority(c)]
    eligible.sort(key=lambda c: (-TIER_SCORE_BY_TIER[c.tier], c.published_at, c.article_id))
    return eligible[:HIGH_PRIORITY_MAX]


# ---------------------------------------------------------------------------
# Earnings section
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EarningsRow:
    ticker: str
    reported_at: datetime
    eps_actual: float | None
    eps_consensus: float | None
    revenue_actual_usd: float | None
    revenue_consensus_usd: float | None


def _load_earnings_rows(
    session: Session,
    *,
    last_invocation_time: datetime,
    as_of: datetime,
    universe: frozenset[str],
) -> list[_EarningsRow]:
    if not universe:
        return []
    range_start = _format_iso_utc(last_invocation_time)
    range_end = _format_iso_utc(as_of)
    rows = session.execute(
        select(
            EarningsEventDetails.ticker,
            EarningsEventDetails.reported_at,
            EarningsEventDetails.eps_actual,
            EarningsEventDetails.eps_consensus,
            EarningsEventDetails.revenue_actual_usd,
            EarningsEventDetails.revenue_consensus_usd,
        )
        .join(EventCalendar, EventCalendar.event_id == EarningsEventDetails.event_id)
        .where(
            EventCalendar.event_type == "earnings",
            EarningsEventDetails.reported_at.is_not(None),
            EarningsEventDetails.reported_at >= range_start,
            EarningsEventDetails.reported_at <= range_end,
            EarningsEventDetails.ticker.in_(tuple(sorted(universe))),
        )
        .order_by(EarningsEventDetails.reported_at, EarningsEventDetails.ticker)
    ).all()
    out: list[_EarningsRow] = []
    for ticker, reported_at, eps_actual, eps_consensus, rev_actual, rev_consensus in rows:
        out.append(
            _EarningsRow(
                ticker=ticker,
                reported_at=_parse_iso_utc(reported_at),
                eps_actual=eps_actual,
                eps_consensus=eps_consensus,
                revenue_actual_usd=rev_actual,
                revenue_consensus_usd=rev_consensus,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Reference-ID assignment + section rendering
# ---------------------------------------------------------------------------


def _make_entry(
    candidate: _Candidate,
    *,
    reference_id: str,
) -> DigestEntry:
    return DigestEntry(
        reference_id=reference_id,
        cluster_id=candidate.cluster_id,
        published_at=candidate.published_at,
        tier=candidate.tier,
        headline=candidate.headline,
        source_outlet=candidate.source_outlet,
        tickers=candidate.tickers,
        tags=candidate.tags,
    )


_TIER_DISPLAY_LABEL: Mapping[_TIER_LITERAL, str] = {
    "tier_1": "T1",
    "tier_2": "T2",
    "tier_3": "T3",
}


def _format_tier_label(tier: _TIER_LITERAL) -> str:
    return _TIER_DISPLAY_LABEL[tier]


def _format_tickers(tickers: Sequence[str]) -> str:
    return ", ".join(tickers) if tickers else "none"


def _format_tags(tags: Sequence[HeadlineType]) -> str:
    return ", ".join(t.value for t in tags) if tags else "none"


def _render_entry_lines(entry: DigestEntry) -> str:
    timestamp = _format_iso_utc(entry.published_at)
    tier_label = _format_tier_label(entry.tier)
    return (
        f"[{entry.reference_id}] {timestamp} [{tier_label}] {entry.headline}\n"
        f"  Source: {entry.source_outlet} | Tickers: {_format_tickers(entry.tickers)} "
        f"| Tags: {_format_tags(entry.tags)}\n"
    )


def _render_high_priority_entry_lines(entry: DigestEntry, *, priority_reason: str) -> str:
    timestamp = _format_iso_utc(entry.published_at)
    tier_label = _format_tier_label(entry.tier)
    return (
        f"[{entry.reference_id}] {timestamp} [{tier_label}] {entry.headline}\n"
        f"  Priority reason: {priority_reason}\n"
        f"  Source: {entry.source_outlet} | Tickers: {_format_tickers(entry.tickers)}\n"
    )


def _priority_reason_for(tags: Sequence[HeadlineType]) -> str:
    if HeadlineType.M_AND_A in tags:
        return "M&A_rumor"
    if HeadlineType.SHORT_REPORT in tags:
        return "short_report"
    if HeadlineType.REGULATORY in tags:
        return "surprise_regulatory"
    if HeadlineType.ACTIVIST in tags:
        return "activist_involvement"
    if HeadlineType.GEOPOLITICAL in tags:
        return "geopolitical_escalation"
    return "high_priority"


def _format_dollar_amount(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "n/a"


def _format_revenue_billions(value: float | None) -> str:
    return f"${value / 1e9:.2f}b" if value is not None else "n/a"


def _render_earnings_line(reference_id: str, row: _EarningsRow) -> str:
    timestamp = _format_iso_utc(row.reported_at)
    return (
        f"[{reference_id}] {row.ticker} reported {timestamp}: "
        f"EPS {_format_dollar_amount(row.eps_actual)} vs "
        f"{_format_dollar_amount(row.eps_consensus)} est, rev "
        f"{_format_revenue_billions(row.revenue_actual_usd)} vs "
        f"{_format_revenue_billions(row.revenue_consensus_usd)} est\n"
    )


def _assign_reference_ids_and_render_sections(
    *,
    bucket_selections: Mapping[Sector | str, list[_Candidate]],
    high_priority: Sequence[_Candidate],
    earnings_rows: Sequence[_EarningsRow],
) -> tuple[list[DigestEntry], list[str]]:
    entries: list[DigestEntry] = []
    sections: list[str] = []

    for bucket in _SECTOR_BUCKET_ORDER:
        members = bucket_selections.get(bucket, [])
        prefix = _REFERENCE_PREFIX_BY_BUCKET[bucket]
        header_label = _SECTION_HEADER_BY_BUCKET[bucket]
        sections.append(f"\n--- {header_label} (top {TOP_N_PER_SECTOR_BUCKET}) ---\n")
        for idx, candidate in enumerate(members, start=1):
            reference_id = f"{prefix}{idx}"
            entry = _make_entry(candidate, reference_id=reference_id)
            entries.append(entry)
            sections.append(_render_entry_lines(entry))

    sections.append("\n--- EARNINGS CALLS SINCE LAST INVOCATION (if any) ---\n")
    for idx, row in enumerate(earnings_rows, start=1):
        reference_id = f"ND-EC{idx}"
        sections.append(_render_earnings_line(reference_id, row))

    sections.append(
        f"\n--- HIGH-PRIORITY FLAGS (0-{HIGH_PRIORITY_MAX}, regardless of sector) ---\n"
    )
    for idx, candidate in enumerate(high_priority, start=1):
        reference_id = f"ND-HP{idx}"
        entry = _make_entry(candidate, reference_id=reference_id)
        entries.append(entry)
        sections.append(
            _render_high_priority_entry_lines(
                entry, priority_reason=_priority_reason_for(entry.tags)
            )
        )

    return entries, sections


def _render_header(
    *,
    invocation_id: str,
    as_of: datetime,
    last_invocation_time: datetime,
    total_collected: int,
    total_shown: int,
) -> str:
    return (
        f"=== NEWS DIGEST (invocation {invocation_id}, "
        f"covering {_format_iso_utc(last_invocation_time)} → {_format_iso_utc(as_of)}) ===\n"
        f"Headlines: {total_collected} collected, {total_shown} shown below\n"
    )


__all__ = [
    "CLUSTER_SIZE_WEIGHT",
    "DIGEST_TOTAL_TOKEN_SOFT_CAP",
    "HIGH_PRIORITY_MAX",
    "RECENCY_WEIGHT",
    "TIER_SCORE_BY_TIER",
    "TOP_N_PER_SECTOR_BUCKET",
    "UNIVERSE_TICKER_BOOST",
    "DigestEntry",
    "NewsDigest",
    "render_news_digest",
]
