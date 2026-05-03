"""Deterministic two-pass headline clustering pipeline — story ALP-245.

Implements the news-digest clustering pipeline specified in
``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering:

- Stage 1 (``canonicalize_syndication``) collapses syndicated wire copies
  using normalized-Levenshtein matching within a 30-minute window.
- Stage 2 (``cluster_events``) groups source-canonical headlines about
  the same event using SimHash Jaccard similarity over 64-bit fingerprints
  plus a shared-ticker constraint within a 6-hour window.
- ``refresh_news_clusters`` is the persistence-layer top-level callable
  that runs both stages and writes ``news_article_clusters`` rows plus
  ``news_articles.cross_ticker_cluster_id`` updates inside one transaction.

Every threshold below is a definitional cutoff (not a Class A tunable);
the pattern mirrors ``alphamind.distillation.qualitative_derived
.NON_NEUTRAL_DOMINANCE_THRESHOLD``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from alphamind.data_sources._common import HeadlineType, decode_topic_tags
from alphamind.persistence.models import (
    NewsArticleClusters,
    NewsArticles,
    NewsArticleTickers,
)

# ---------------------------------------------------------------------------
# Definitional constants (not Class A tunables)
# ---------------------------------------------------------------------------

STAGE_1_LEVENSHTEIN_RATIO_MIN: float = 0.90
"""Stage 1 source-canonicalization threshold — minimum normalized-Levenshtein
ratio for two headlines to merge as syndicated copies of the same wire item.

Per ``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering: "Match condition: normalized-Levenshtein ratio of
``headline_text`` ≥ 0.90." Definitional cutoff, not a Class A tunable —
if paper trading reveals it is wrong, raise it as a future story rather
than tuning silently in code.
"""


STAGE_1_TIME_WINDOW_MINUTES: int = 30
"""Stage 1 source-canonicalization comparison window — only headlines published
within this many minutes of each other are eligible for syndicate merging.

Per ``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering: "Compare every pair of headlines published within 30 minutes
of each other." Definitional cutoff, not a Class A tunable.
"""


STAGE_2_SIMHASH_JACCARD_MIN: float = 0.60
"""Stage 2 event-clustering threshold — minimum SimHash bit-overlap (Jaccard
ratio over 64-bit fingerprints) for two source-canonical headlines to
cluster as covering the same event.

Per ``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering: "SimHash Jaccard similarity over 64-bit fingerprints of
``headline_text`` ≥ 0.60." Definitional cutoff, not a Class A tunable.
"""


STAGE_2_TIME_WINDOW_HOURS: int = 6
"""Stage 2 event-clustering comparison window — only source-canonical headlines
published within this many hours of each other (or of the cluster's
``first_seen_at``) cluster together.

Per ``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering: "Compare every pair of source-canonical headlines published
within 6 hours of each other." Definitional cutoff, not a Class A tunable.
"""


CLUSTER_SEAL_HOURS: int = 24
"""Cluster seal horizon — a cluster stops accepting new members at
``first_seen_at + CLUSTER_SEAL_HOURS``; matching headlines arriving past
the seal start a new cluster.

Per ``docs/design/03-analysis-layer/qualitative-research.md`` § Headline
clustering: "Cluster sealing: a cluster seals at ``first_seen_at + 24h``.
Headlines arriving past the seal that match the cluster's similarity
profile start a new cluster." Definitional cutoff, not a Class A tunable.
"""


# ---------------------------------------------------------------------------
# Headline normalization
# ---------------------------------------------------------------------------

# Outlet-specific lead tokens stripped during normalization. Per § Headline
# clustering: "drop outlet-specific lead tokens (``BREAKING:``, ``UPDATE:``,
# ``EXCLUSIVE:``)." The regex anchors at the start of the (already-lowercased)
# headline and tolerates either ``:`` or whitespace after the keyword.
_LEAD_TOKEN_PATTERN = re.compile(r"^(?:breaking|update|exclusive)[\s:]+")

# Match ``$AAPL`` anywhere in the text and strip the leading ``$``. The
# normalization step keeps the bare ticker so cross-outlet references like
# ``$AAPL`` and ``AAPL`` collapse to the same token before the Levenshtein
# ratio is computed.
_TICKER_PREFIX_PATTERN = re.compile(r"\$([A-Za-z]{1,5})\b")

_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _normalize_headline(headline: str) -> str:
    """Normalize a headline string per § Headline clustering Stage 1.

    Lowercase, strip punctuation, normalize whitespace, normalize ticker
    references (``$AAPL`` → ``aapl``), and drop outlet-specific lead tokens.
    """
    lowered = headline.lower()
    detickered = _TICKER_PREFIX_PATTERN.sub(lambda m: m.group(1).lower(), lowered)
    delede = _LEAD_TOKEN_PATTERN.sub("", detickered)
    depunctuated = _PUNCTUATION_PATTERN.sub(" ", delede)
    return _WHITESPACE_PATTERN.sub(" ", depunctuated).strip()


# ---------------------------------------------------------------------------
# Time-arithmetic helpers
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp (``Z``-suffix tolerated)."""
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _format_iso_utc(dt: datetime) -> str:
    """Render a tz-aware datetime as an ISO 8601 ``Z``-suffixed UTC string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Stage 1 — source canonicalization
# ---------------------------------------------------------------------------


def canonicalize_syndication(articles: Sequence[NewsArticles]) -> Mapping[str, str]:
    """Collapse syndicated wire copies into source-canonical groups.

    Per § Headline clustering Stage 1: compare every pair of headlines
    published within ``STAGE_1_TIME_WINDOW_MINUTES`` of each other; merge
    pairs whose normalized-Levenshtein ratio is at least
    ``STAGE_1_LEVENSHTEIN_RATIO_MIN``. The canonical representative for
    each merged group is the earliest member's ``article_id``.

    Single-link agglomeration: a headline joins any group it matches at
    least one current member of. Articles without a matching peer map to
    themselves.

    Returns a mapping from each input ``article_id`` to its source-canonical
    ``article_id``.
    """
    sorted_articles = sorted(articles, key=lambda a: (a.published_at, a.article_id))
    window = timedelta(minutes=STAGE_1_TIME_WINDOW_MINUTES)

    # parent[article_id] = canonical article_id; canonical articles map to
    # themselves. New entries default to themselves and only flip to an
    # earlier id when a Stage-1 match is found.
    parent: dict[str, str] = {}
    normalized: dict[str, str] = {}
    timestamps: dict[str, datetime] = {}
    for article in sorted_articles:
        article_id = article.article_id
        parent[article_id] = article_id
        normalized[article_id] = _normalize_headline(article.headline_text)
        timestamps[article_id] = _parse_iso_utc(article.published_at)

    for i, candidate in enumerate(sorted_articles):
        candidate_id = candidate.article_id
        candidate_norm = normalized[candidate_id]
        candidate_ts = timestamps[candidate_id]
        for prior in sorted_articles[:i]:
            prior_id = prior.article_id
            if candidate_ts - timestamps[prior_id] > window:
                continue
            ratio = SequenceMatcher(None, normalized[prior_id], candidate_norm).ratio()
            if ratio >= STAGE_1_LEVENSHTEIN_RATIO_MIN:
                # Adopt prior's canonical (which itself may have been
                # remapped — single-link agglomeration).
                parent[candidate_id] = parent[prior_id]
                break

    return dict(parent)


# ---------------------------------------------------------------------------
# Stage 2 — SimHash and Jaccard helpers
# ---------------------------------------------------------------------------

_SIMHASH_BITS = 64

# A 64-bit mask used to confine the MD5 digest to its low 64 bits. Expressed
# as ``(1 << _SIMHASH_BITS) - 1`` rather than a literal so the bit-width is
# the single source of truth.
_SIMHASH_MASK = (1 << _SIMHASH_BITS) - 1


def _simhash(text: str) -> int:
    """Return the 64-bit SimHash fingerprint of ``text``'s token set.

    Tokens are extracted from the post-Stage-1 normalized text. Each token's
    MD5 digest contributes ±1 per bit position to a per-bit accumulator;
    the final fingerprint sets bit ``i`` when the accumulator is strictly
    positive. The resulting integer's bits are independent of token order.
    """
    tokens = _normalize_headline(text).split()
    accumulator = [0] * _SIMHASH_BITS
    for token in tokens:
        digest = int.from_bytes(hashlib.md5(token.encode()).digest()[:8], "big")
        for bit in range(_SIMHASH_BITS):
            if (digest >> bit) & 1:
                accumulator[bit] += 1
            else:
                accumulator[bit] -= 1
    fingerprint = 0
    for bit in range(_SIMHASH_BITS):
        if accumulator[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def _jaccard_64bit(a: int, b: int) -> float:
    """Return the bit-Jaccard ratio over two 64-bit fingerprints.

    Defined as ``popcount(a & b) / popcount(a | b)``. Both fingerprints
    all-zero collapses to a defined ``1.0`` (the degenerate case of two
    empty token sets — the caller guards Stage 2 with the shared-ticker /
    topic-tag predicate, so headlines with no extractable tokens cluster
    only with each other when that predicate is satisfied).
    """
    intersection = ((a & b) & _SIMHASH_MASK).bit_count()
    union = ((a | b) & _SIMHASH_MASK).bit_count()
    if union == 0:
        return 1.0
    return intersection / union


# ---------------------------------------------------------------------------
# Headline cluster record (in-memory shape)
# ---------------------------------------------------------------------------


class HeadlineCluster(BaseModel, frozen=True):
    """In-memory representation of one ``news_article_clusters`` row.

    Mirrors the schema-level shape documented in
    ``docs/design/01-data-layer/schema/news_sentiment.py`` § ``HeadlineCluster``
    and the runtime table defined by the migration this story adds:

    - ``cluster_id`` is the ``article_id`` of the earliest source-canonical
      member.
    - ``primary_theme`` is the modal :class:`HeadlineType` across cluster
      members; ties resolve to the modal member's first ``topic_tags`` entry.
    - ``headline_count`` is the number of source-canonical members
      (post-Stage-1, distinct outlets only).
    - ``first_seen_at`` / ``last_seen_at`` bound cluster activity within
      the seal window.
    - ``tickers_involved`` is the union of ``(primary_ticker,
      tickers_mentioned)`` across members.
    - ``sealed_at`` = ``first_seen_at + CLUSTER_SEAL_HOURS``.
    - ``member_article_ids`` is the per-member back-reference the
      persistence-layer caller uses to stamp
      ``news_articles.cross_ticker_cluster_id``.
    """

    cluster_id: str = Field(min_length=1)
    primary_theme: HeadlineType
    headline_count: int = Field(ge=1)
    first_seen_at: datetime
    last_seen_at: datetime
    tickers_involved: tuple[str, ...]
    sealed_at: datetime
    member_article_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Stage 2 — event clustering
# ---------------------------------------------------------------------------


def _modal_theme(article_themes: Sequence[tuple[HeadlineType, ...]]) -> HeadlineType:
    """Return the modal :class:`HeadlineType` across the cluster's members.

    Per § Cluster metadata: "modal ``HeadlineType`` across cluster members;
    ties resolve to the modal member's first ``topic_tags`` entry." When
    every member is tag-less the cluster falls back to
    :attr:`HeadlineType.BREAKING` — the catch-all bucket for new wire items.
    """
    counts: Counter[HeadlineType] = Counter()
    for tags in article_themes:
        for tag in tags:
            counts[tag] += 1
    if not counts:
        return HeadlineType.BREAKING
    top_count = max(counts.values())
    for tags in article_themes:
        for tag in tags:
            if counts[tag] == top_count:
                return tag
    raise AssertionError("unreachable: top_count came from a tag in counts")


@dataclass(frozen=True, slots=True)
class _CandidateProfile:
    """Pre-computed data the Stage 2 inner loop reads for one candidate."""

    article_id: str
    fingerprint: int
    timestamp: datetime
    tickers: frozenset[str]
    themes: tuple[HeadlineType, ...]


@dataclass(frozen=True, slots=True)
class _ClusteringContext:
    """Per-article state ``cluster_events`` shares across the inner loops."""

    fingerprints: Mapping[str, int]
    timestamps: Mapping[str, datetime]
    ticker_index: Mapping[str, frozenset[str]]
    themes: Mapping[str, tuple[HeadlineType, ...]]
    article_window: timedelta


def _candidate_matches_member(
    candidate: _CandidateProfile,
    member_id: str,
    ctx: _ClusteringContext,
) -> bool:
    """Return True when the Stage 2 candidate may join the given member.

    Encodes the three predicates from § Headline clustering Stage 2 — time
    window, SimHash Jaccard floor, and the shared-ticker / topic-tag rule.
    """
    if candidate.timestamp - ctx.timestamps[member_id] > ctx.article_window:
        return False
    if (
        _jaccard_64bit(candidate.fingerprint, ctx.fingerprints[member_id])
        < STAGE_2_SIMHASH_JACCARD_MIN
    ):
        return False
    return _members_share_predicate(
        candidate_tickers=candidate.tickers,
        member_tickers=ctx.ticker_index.get(member_id, frozenset()),
        candidate_themes=candidate.themes,
        member_themes=ctx.themes[member_id],
    )


def cluster_events(
    canonical_articles: Sequence[NewsArticles],
    ticker_index: Mapping[str, frozenset[str]],
) -> list[HeadlineCluster]:
    """Group source-canonical headlines into event clusters.

    Per § Headline clustering Stage 2 — all three conditions required:

    1. SimHash Jaccard similarity ≥ ``STAGE_2_SIMHASH_JACCARD_MIN`` over
       64-bit fingerprints of normalized ``headline_text``.
    2. At least one shared ticker between the headlines'
       ``ticker_index`` entries; ticker-less headlines cluster only with
       other ticker-less headlines whose ``topic_tags`` intersect by at
       least one tag.
    3. Both members fall within ``STAGE_2_TIME_WINDOW_HOURS`` of the
       cluster's ``first_seen_at``.

    The cluster ``cluster_id`` is the earliest source-canonical member's
    ``article_id``. Single-link agglomeration: a headline joins an existing
    cluster if it matches any current member.

    Cluster sealing is realized by emitting a fresh cluster whenever a
    candidate falls outside the active cluster's
    ``first_seen_at + CLUSTER_SEAL_HOURS`` horizon, even when its similarity
    profile would otherwise match.
    """
    sorted_articles = sorted(canonical_articles, key=lambda a: (a.published_at, a.article_id))
    ctx = _ClusteringContext(
        fingerprints={a.article_id: _simhash(a.headline_text) for a in sorted_articles},
        timestamps={a.article_id: _parse_iso_utc(a.published_at) for a in sorted_articles},
        ticker_index=ticker_index,
        themes={a.article_id: decode_topic_tags(a.topic_tags) for a in sorted_articles},
        article_window=timedelta(hours=STAGE_2_TIME_WINDOW_HOURS),
    )
    seal_window = timedelta(hours=CLUSTER_SEAL_HOURS)

    # cluster_id (earliest member's article_id) → ordered list of member article_ids.
    members_by_cluster: dict[str, list[str]] = {}
    # ``cluster_order`` is naturally sorted by ``first_seen_at`` ASC: outer-loop
    # candidates arrive in ``published_at`` ASC order, and a freshly-seeded
    # cluster's ``first_seen_at`` is the candidate's own timestamp.
    cluster_order: list[str] = []
    cluster_first_seen: dict[str, datetime] = {}
    # Index of the leftmost cluster still potentially in the Stage-2 window.
    # Monotonically non-decreasing across the outer loop because candidate
    # timestamps are non-decreasing — once a cluster falls behind the window
    # for one candidate, no later candidate can resurrect it.
    start_idx = 0

    for article in sorted_articles:
        candidate = _CandidateProfile(
            article_id=article.article_id,
            fingerprint=ctx.fingerprints[article.article_id],
            timestamp=ctx.timestamps[article.article_id],
            tickers=ctx.ticker_index.get(article.article_id, frozenset()),
            themes=ctx.themes[article.article_id],
        )
        # Advance ``start_idx`` past clusters whose ``first_seen_at`` precedes
        # the candidate by more than Stage 2's comparison window — per the
        # design spec a new member must fall within ``STAGE_2_TIME_WINDOW_HOURS``
        # of the cluster's ``first_seen_at``, so older clusters cannot match.
        # The ``CLUSTER_SEAL_HOURS`` horizon is strictly looser than the
        # Stage-2 window, so this advance also enforces the seal.
        while start_idx < len(cluster_order) and (
            candidate.timestamp - cluster_first_seen[cluster_order[start_idx]] > ctx.article_window
        ):
            start_idx += 1
        candidate_cluster_ids = cluster_order[start_idx:]
        joined = _find_joinable_cluster(candidate, candidate_cluster_ids, members_by_cluster, ctx)
        if joined is not None:
            members_by_cluster[joined].append(candidate.article_id)
        else:
            members_by_cluster[candidate.article_id] = [candidate.article_id]
            cluster_order.append(candidate.article_id)
            cluster_first_seen[candidate.article_id] = candidate.timestamp

    return _build_cluster_records(members_by_cluster, ctx, seal_window)


def _find_joinable_cluster(
    candidate: _CandidateProfile,
    candidate_cluster_ids: Sequence[str],
    members_by_cluster: Mapping[str, Sequence[str]],
    ctx: _ClusteringContext,
) -> str | None:
    """Return the cluster id ``candidate`` joins via single-link agglomeration.

    ``candidate_cluster_ids`` is the caller-bounded slice of clusters within
    Stage-2's comparison window of ``candidate``; older clusters are skipped
    by the caller so this function never sees them. Within the slice, returns
    the first cluster (in ``first_seen_at`` ASC order) that has any member
    matching ``candidate`` per :func:`_candidate_matches_member`.
    """
    for cluster_id in candidate_cluster_ids:
        for member_id in members_by_cluster[cluster_id]:
            if _candidate_matches_member(candidate, member_id, ctx):
                return cluster_id
    return None


def _build_cluster_records(
    clusters: Mapping[str, Sequence[str]],
    ctx: _ClusteringContext,
    seal_window: timedelta,
) -> list[HeadlineCluster]:
    """Materialize the agglomeration result as :class:`HeadlineCluster` records."""
    out: list[HeadlineCluster] = []
    for cluster_id, member_ids in clusters.items():
        first_seen = min(ctx.timestamps[mid] for mid in member_ids)
        last_seen = max(ctx.timestamps[mid] for mid in member_ids)
        modal = _modal_theme([ctx.themes[mid] for mid in member_ids])
        union: set[str] = set()
        for mid in member_ids:
            union |= ctx.ticker_index.get(mid, frozenset())
        out.append(
            HeadlineCluster(
                cluster_id=cluster_id,
                primary_theme=modal,
                headline_count=len(member_ids),
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                tickers_involved=tuple(sorted(union)),
                sealed_at=first_seen + seal_window,
                member_article_ids=tuple(member_ids),
            )
        )
    out.sort(key=lambda c: c.first_seen_at)
    return out


def _members_share_predicate(
    *,
    candidate_tickers: frozenset[str],
    member_tickers: frozenset[str],
    candidate_themes: tuple[HeadlineType, ...],
    member_themes: tuple[HeadlineType, ...],
) -> bool:
    """Return True if Stage 2's shared-ticker / topic-tag predicate holds.

    Per § Headline clustering Stage 2: "At least one shared ticker between
    the headlines' ticker sets. Ticker-less headlines cluster only with
    other ticker-less headlines whose ``topic_tags`` intersect by at least
    one tag."
    """
    if candidate_tickers and member_tickers:
        return bool(candidate_tickers & member_tickers)
    if not candidate_tickers and not member_tickers:
        return bool(set(candidate_themes) & set(member_themes))
    # One side has tickers, the other doesn't — they cannot share a ticker
    # and the ticker-less side cannot satisfy the topic-tag fallback (which
    # is reserved for two ticker-less headlines).
    return False


# ---------------------------------------------------------------------------
# Persistence-layer entry point
# ---------------------------------------------------------------------------


# Default lookback window for ``refresh_news_clusters``. Bounded by the
# Stage-2 6-hour comparison window plus the 24-hour seal — anything older
# than the seal cannot accept a new member, so 48 hours is a generous
# margin that covers boundary effects (an ``as_of`` close to a 24-hour
# ago seal) without scanning the full archive every refresh.
_DEFAULT_LOOKBACK_HOURS = CLUSTER_SEAL_HOURS + STAGE_2_TIME_WINDOW_HOURS * 4


class RefreshResult(BaseModel, frozen=True):
    """Observability summary returned by :func:`refresh_news_clusters`."""

    articles_processed: int = Field(ge=0)
    clusters_written: int = Field(ge=0)
    clusters_updated: int = Field(ge=0)
    articles_unclustered: int = Field(ge=0)


def _load_ticker_index(session: Session, article_ids: Sequence[str]) -> dict[str, frozenset[str]]:
    """Build the ``article_id`` → ticker-set map from ``news_article_tickers``."""
    if not article_ids:
        return {}
    rows = session.execute(
        select(NewsArticleTickers.article_id, NewsArticleTickers.ticker).where(
            NewsArticleTickers.article_id.in_(tuple(article_ids))
        )
    ).all()
    by_article: dict[str, set[str]] = {aid: set() for aid in article_ids}
    for article_id, ticker in rows:
        by_article.setdefault(article_id, set()).add(ticker)
    return {aid: frozenset(tickers) for aid, tickers in by_article.items()}


def refresh_news_clusters(
    session: Session,
    *,
    as_of: datetime,
    lookback_window_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> RefreshResult:
    """Run Stage 1 + Stage 2 over the trailing window and persist the output.

    Reads ``news_articles`` rows where
    ``published_at >= as_of - lookback_window_hours``, runs
    :func:`canonicalize_syndication` followed by :func:`cluster_events`, and
    writes ``news_article_clusters`` rows plus
    ``news_articles.cross_ticker_cluster_id`` updates inside one transaction.

    Idempotent: re-running on the same window does not change cluster IDs
    (a cluster's ID is the earliest member's ``article_id``, which is
    stable). Existing rows are upserted in place — the headline count and
    seal timestamp refresh with each run as new members join.
    """
    range_start = _format_iso_utc(as_of - timedelta(hours=lookback_window_hours))
    range_end = _format_iso_utc(as_of)

    articles = list(
        session.execute(
            select(NewsArticles)
            .where(
                NewsArticles.published_at >= range_start,
                NewsArticles.published_at <= range_end,
            )
            .order_by(NewsArticles.published_at)
        )
        .scalars()
        .all()
    )
    if not articles:
        return RefreshResult(
            articles_processed=0,
            clusters_written=0,
            clusters_updated=0,
            articles_unclustered=0,
        )

    canonical_map = canonicalize_syndication(articles)
    canonical_ids = {canonical_map[a.article_id] for a in articles}
    canonical_articles = [a for a in articles if a.article_id in canonical_ids]

    ticker_index = _load_ticker_index(session, [a.article_id for a in canonical_articles])
    clusters = cluster_events(canonical_articles, ticker_index)

    # Only event clusters with two or more source-canonical members are
    # persisted — singleton "clusters" are unclustered headlines.
    multi_member_clusters = [c for c in clusters if c.headline_count >= 2]

    existing_rows: dict[str, NewsArticleClusters] = {}
    if multi_member_clusters:
        existing_rows = {
            row.cluster_id: row
            for row in session.execute(
                select(NewsArticleClusters).where(
                    NewsArticleClusters.cluster_id.in_(
                        tuple(c.cluster_id for c in multi_member_clusters)
                    )
                )
            )
            .scalars()
            .all()
        }

    clusters_written = 0
    clusters_updated = 0
    members_per_cluster: dict[str, list[str]] = {}
    for cluster in multi_member_clusters:
        canonical_members = set(cluster.member_article_ids)
        # Each Stage-1 syndicate copy inherits its canonical's cluster id.
        all_members = [
            article_id
            for article_id, canonical_id in canonical_map.items()
            if canonical_id in canonical_members
        ]
        members_per_cluster[cluster.cluster_id] = all_members

        primary_theme = cluster.primary_theme.value
        first_seen = _format_iso_utc(cluster.first_seen_at)
        last_seen = _format_iso_utc(cluster.last_seen_at)
        sealed = _format_iso_utc(cluster.sealed_at)
        tickers_blob = json.dumps(list(cluster.tickers_involved))

        existing = existing_rows.get(cluster.cluster_id)
        if existing is None:
            session.add(
                NewsArticleClusters(
                    cluster_id=cluster.cluster_id,
                    primary_theme=primary_theme,
                    headline_count=cluster.headline_count,
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    tickers_involved=tickers_blob,
                    sealed_at=sealed,
                )
            )
            clusters_written += 1
        else:
            existing.primary_theme = primary_theme
            existing.headline_count = cluster.headline_count
            existing.first_seen_at = first_seen
            existing.last_seen_at = last_seen
            existing.tickers_involved = tickers_blob
            existing.sealed_at = sealed
            clusters_updated += 1

    member_count = 0
    for cluster_id, member_ids in members_per_cluster.items():
        if not member_ids:
            continue
        member_count += len(member_ids)
        session.execute(
            update(NewsArticles)
            .where(NewsArticles.article_id.in_(tuple(member_ids)))
            .values(cross_ticker_cluster_id=cluster_id)
        )

    session.commit()

    return RefreshResult(
        articles_processed=len(articles),
        clusters_written=clusters_written,
        clusters_updated=clusters_updated,
        articles_unclustered=len(articles) - member_count,
    )


__all__ = [
    "CLUSTER_SEAL_HOURS",
    "STAGE_1_LEVENSHTEIN_RATIO_MIN",
    "STAGE_1_TIME_WINDOW_MINUTES",
    "STAGE_2_SIMHASH_JACCARD_MIN",
    "STAGE_2_TIME_WINDOW_HOURS",
    "HeadlineCluster",
    "RefreshResult",
    "canonicalize_syndication",
    "cluster_events",
    "refresh_news_clusters",
]
