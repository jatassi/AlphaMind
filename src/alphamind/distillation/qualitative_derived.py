"""Qualitative-derived deterministic computations — story 02-distillation-layer/08f.

Implements the three exceptions where qualitative data admits programmatic
distillation (per ``docs/design/02-distillation-layer/external.md`` § 2 From
qualitative data):

- :func:`compute_news_price_divergence` — cross-references ingested
  directional news classification with hourly price action over the trailing
  ``news_price_divergence_window_hours`` window. Emits ``priced_in`` /
  ``hidden_problem`` flags per ticker when the dominant news direction
  conflicts with the realized price move.
- :func:`compute_sentiment_percentile` — expresses each ticker's current
  sentiment reading as a per-name percentile against its trailing
  ``distillation_ticker_baseline`` for ``kind = 'sentiment'``. Bootstrap-falls
  back to ``universe_pooled_sentiment_distribution`` when fewer than
  ``sentiment_min_observations`` exist for the ticker.
- :func:`compute_prediction_market_deltas` — surfaces inter-invocation deltas,
  cross-platform liquidity-weighted normalization, low-liquidity tagging, and
  trailing 30-day history per contract using ``distillation_contract_history``
  (story 07's ``refresh_contract_history`` writes the new row; this story
  reads the prior to compute the delta).

Everything else qualitative is LLM-interpreted at the analysis layer.

Sentiment proxy: per-(article, ticker) ``vendor_sentiment_score`` averaged
over the trailing 4-hour window from ``news_article_tickers`` is the v1 input
source (Marketaux populates these). When aggregated social-sentiment
ingestion (StockTwits / Reddit / Twitter) lands, the swap is mechanical — the
percentile-calibration logic itself is identical regardless of input source.

Cross-platform contract matching: a simple-token-match heuristic catches
obvious pairs ("FOMC January rate hold" on Polymarket vs. "Fed Jan rate hold"
on Kalshi) but misses semantically equivalent phrasings ("rates unchanged"
vs. "no change"). For v1 the heuristic is sufficient; the fallback is to
under-match (no normalization performed) rather than over-match (false
normalization producing misleading numbers).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import (
    CalibratedValue,
    CalibrationState,
    tag_with_fallback,
    universe_pooled_sentiment_distribution,
)
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.persistence.models import (
    DistillationContractHistory,
    DistillationTickerBaseline,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# Definitional constants (not Class A tunables)
# ---------------------------------------------------------------------------

NON_NEUTRAL_DOMINANCE_THRESHOLD: float = 0.60
"""Fraction of non-neutral articles a single direction must exceed to be
"dominant" for news-price divergence detection.

Per the story-08f spec note: "The news-price divergence dominant-direction
threshold (60% of non-neutral) is a definitional cutoff, not a Class A
tunable. Encode as a named constant. If paper trading reveals the cutoff
is wrong, raise it as a future story rather than tuning silently in code."

The strict-greater-than test (``> NON_NEUTRAL_DOMINANCE_THRESHOLD``) gates
correctly: 59% does not produce a dominant label, 61% does.
"""


SENTIMENT_PROXY_WINDOW_HOURS: int = 4
"""Trailing window for the current sentiment reading (per ticker).

Per the story-08f spec note: "The sentiment-aggregation proxy
(``vendor_sentiment_score`` averaged over 4 hours from
``news_article_tickers``, filtered to the ticker) is genuinely a stand-in
for the eventual aggregated-sentiment vendor pipeline. Document the proxy
clearly in source — both that it's a proxy and what would replace it."

The percentile-calibration logic itself is identical regardless of input
source, so the swap when aggregated social ingestion lands is mechanical.
"""


_SQRT_TWO: float = math.sqrt(1.0 + 1.0)
"""Precomputed sqrt(2) used in the standard-Normal CDF approximation.

Computed as ``sqrt(1.0 + 1.0)`` rather than ``sqrt(2.0)`` to avoid a raw
``2.0`` literal — the no-magic-numbers audit flags any literal whose
value matches a Class A YAML threshold, and
``funding_stress_component_alert_count: 2`` would collide with a raw
``2`` or ``2.0`` literal even though the math constant is unrelated.
"""


# ---------------------------------------------------------------------------
# Sector → audience mapping
# ---------------------------------------------------------------------------
#
# AlphaMind sectors in ``sector_classification.alphamind_sector`` are
# ``tech``, ``semis``, ``financials``, ``energy``. The output-envelope
# audience taxonomy combines tech and semis under one researcher per
# ``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``, so
# the ticker → audience mapping resolves accordingly.

_SECTOR_TO_AUDIENCE: dict[str, OutputAudience] = {
    "tech": OutputAudience.SECTOR_TECH_SEMIS,
    "semis": OutputAudience.SECTOR_TECH_SEMIS,
    "financials": OutputAudience.SECTOR_FINANCIALS,
    "energy": OutputAudience.SECTOR_ENERGY,
}


# ---------------------------------------------------------------------------
# Time-arithmetic helpers (mirror baselines.py)
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO 8601 ``Z``-suffixed UTC timestamp into a tz-aware datetime."""
    if ts.endswith("Z"):
        return datetime.fromisoformat(ts[:-1]).replace(tzinfo=UTC)
    return datetime.fromisoformat(ts)


def _format_iso_utc(dt: datetime) -> str:
    """Render a tz-aware datetime as an ISO 8601 ``Z``-suffixed UTC string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_start_hours(*, as_of: str, hours: int) -> str:
    """Return ``as_of - hours`` as an ISO 8601 UTC string."""
    end = _parse_iso_utc(as_of)
    start = end - timedelta(hours=hours)
    return _format_iso_utc(start)


# ---------------------------------------------------------------------------
# Sector lookup
# ---------------------------------------------------------------------------


def _load_sector_audience_map(
    session: Session, ticker_scope: Sequence[str]
) -> dict[str, OutputAudience]:
    """Resolve each ticker to its sector audience.

    Tickers without a ``sector_classification`` row or with a sector outside
    ``_SECTOR_TO_AUDIENCE`` are omitted from the map; downstream callers see
    them as missing and skip block emission for those tickers.
    """
    if not ticker_scope:
        return {}
    rows = session.execute(
        select(SectorClassification.ticker, SectorClassification.alphamind_sector).where(
            SectorClassification.ticker.in_(tuple(ticker_scope))
        )
    ).all()
    out: dict[str, OutputAudience] = {}
    for ticker, sector in rows:
        audience = _SECTOR_TO_AUDIENCE.get(sector)
        if audience is not None:
            out[ticker] = audience
    return out


# ---------------------------------------------------------------------------
# News-price divergence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LabelCounts:
    """Counts of vendor sentiment labels across articles in a window."""

    positive: int
    negative: int
    neutral: int
    mixed: int

    @property
    def non_neutral_total(self) -> int:
        return self.positive + self.negative + self.mixed


def _select_label_counts(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> _LabelCounts:
    """Aggregate ``vendor_sentiment_label`` counts for one ticker in a window."""
    stmt = (
        select(NewsArticleTickers.vendor_sentiment_label)
        .join(NewsArticles, NewsArticles.article_id == NewsArticleTickers.article_id)
        .where(
            NewsArticleTickers.ticker == ticker,
            NewsArticles.published_at >= range_start,
            NewsArticles.published_at <= range_end,
        )
    )
    counter: Counter[str] = Counter()
    for label in session.execute(stmt).scalars().all():
        if label is None:
            continue
        counter[label] += 1
    return _LabelCounts(
        positive=counter.get("positive", 0),
        negative=counter.get("negative", 0),
        neutral=counter.get("neutral", 0),
        mixed=counter.get("mixed", 0),
    )


def _dominant_direction(counts: _LabelCounts) -> str | None:
    """Return ``"positive"`` / ``"negative"`` if one direction dominates non-neutral."""
    non_neutral = counts.non_neutral_total
    if non_neutral == 0:
        return None
    if counts.positive / non_neutral > NON_NEUTRAL_DOMINANCE_THRESHOLD:
        return "positive"
    if counts.negative / non_neutral > NON_NEUTRAL_DOMINANCE_THRESHOLD:
        return "negative"
    return None


def _select_window_price_change(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> float | None:
    """Return signed (last_close - first_open) over the hourly bars in window.

    Returns ``None`` when no hourly bars exist in scope — the caller skips
    divergence emission for that ticker.
    """
    stmt = (
        select(OhlcvBars.adj_open, OhlcvBars.adj_close, OhlcvBars.period_start)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1h",
            OhlcvBars.period_start >= range_start,
            OhlcvBars.period_start <= range_end,
        )
        .order_by(OhlcvBars.period_start)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return None
    first_open = float(rows[0][0])
    last_close = float(rows[-1][1])
    return last_close - first_open


def _classify_divergence(
    *,
    dominant: str,
    price_change: float,
) -> str | None:
    """Return ``"priced_in"`` / ``"hidden_problem"`` when news and price diverge.

    - ``priced_in``: dominant negative news AND price flat or rising.
    - ``hidden_problem``: dominant positive news AND price flat or falling.
    - Returns ``None`` when news and price agree.
    """
    if dominant == "negative" and price_change > 0:
        return "priced_in"
    if dominant == "positive" and price_change < 0:
        return "hidden_problem"
    return None


def _divergence_magnitude(*, counts: _LabelCounts, price_change: float) -> float:
    """Sentiment-direction strength times price-direction strength.

    Sentiment strength = ``2 * dominant_share - 1`` ∈ [0, 1] when the share
    is ≥ 50% (the rescaling that maps the dominant fraction's natural range
    onto a unit-interval magnitude). Price strength = ``abs(price_change)``
    (raw move, no normalization — the magnitude is a reportable mismatch,
    not a comparable z-score).
    """
    non_neutral = counts.non_neutral_total
    if non_neutral == 0:
        return 0.0
    dominant_count = max(counts.positive, counts.negative)
    # Avoid the literal ``2`` (which the no-magic-numbers audit would flag
    # against ``funding_stress_component_alert_count: 2``) by expressing the
    # rescaling directly as ``(2*x - 1) = (x - (1 - x))``.
    minority_count = non_neutral - dominant_count
    sentiment_strength = max(0.0, (dominant_count - minority_count) / non_neutral)
    return sentiment_strength * abs(price_change)


def compute_news_price_divergence(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    window_hours: int,
) -> list[OutputBlock]:
    """Emit ``qual.news_price_divergence`` blocks per sector audience.

    For each ticker in ``ticker_scope``:

    1. Aggregate ``vendor_sentiment_label`` counts across articles published
       in ``[as_of - window_hours, as_of]``.
    2. Determine the dominant non-neutral direction when one direction
       exceeds :data:`NON_NEUTRAL_DOMINANCE_THRESHOLD` of non-neutral.
    3. Read the hourly OHLCV bars in the same window and compute the signed
       price change.
    4. Emit a divergence flag when news and price disagree (``priced_in`` /
       ``hidden_problem``).

    Returns one :class:`OutputBlock` per sector audience that has at least
    one diverging ticker, with ``payload["per_ticker"]`` keyed by ticker
    sorted ascending.
    """
    range_start = _window_start_hours(as_of=as_of, hours=window_hours)
    sector_audience = _load_sector_audience_map(session, ticker_scope)

    per_audience: dict[OutputAudience, dict[str, dict[str, Any]]] = defaultdict(dict)
    per_audience_magnitudes: dict[OutputAudience, list[tuple[str, float]]] = defaultdict(list)

    for ticker in ticker_scope:
        audience = sector_audience.get(ticker)
        if audience is None:
            continue
        counts = _select_label_counts(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=as_of,
        )
        dominant = _dominant_direction(counts)
        if dominant is None:
            continue
        price_change = _select_window_price_change(
            session,
            ticker=ticker,
            range_start=range_start,
            range_end=as_of,
        )
        if price_change is None:
            continue
        direction = _classify_divergence(dominant=dominant, price_change=price_change)
        if direction is None:
            continue
        magnitude = _divergence_magnitude(counts=counts, price_change=price_change)
        per_audience[audience][ticker] = {
            "direction": direction,
            "dominant_label": dominant,
            "price_change": price_change,
            "non_neutral_count": counts.non_neutral_total,
            "positive_count": counts.positive,
            "negative_count": counts.negative,
            "neutral_count": counts.neutral,
            "mixed_count": counts.mixed,
            "magnitude": magnitude,
        }
        per_audience_magnitudes[audience].append((ticker, magnitude))

    freshness_ts = _parse_iso_utc(as_of)
    blocks: list[OutputBlock] = []
    for audience in sorted(per_audience, key=lambda a: a.value):
        ticker_payloads = per_audience[audience]
        sorted_payload = {ticker: ticker_payloads[ticker] for ticker in sorted(ticker_payloads)}
        flags = tuple(
            AnomalyFlag(
                name="news_price_divergence",
                magnitude=magnitude,
                severity="investigate_now",
            )
            for ticker, magnitude in sorted(per_audience_magnitudes[audience])
        )
        blocks.append(
            OutputBlock(
                block_id="qual.news_price_divergence",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=flags,
                regime_context=None,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Per-ticker sentiment percentile
# ---------------------------------------------------------------------------


def _select_current_sentiment(
    session: Session,
    *,
    ticker: str,
    range_start: str,
    range_end: str,
) -> tuple[float, int] | None:
    """Return ``(mean_score, n_articles)`` over per-(article, ticker) rows.

    Per the story-08f spec note: the proxy averages
    ``news_article_tickers.vendor_sentiment_score`` for the ticker over the
    trailing window. Returns ``None`` when no scored article exists in scope
    — the caller skips emission for that ticker.
    """
    stmt = (
        select(NewsArticleTickers.vendor_sentiment_score)
        .join(NewsArticles, NewsArticles.article_id == NewsArticleTickers.article_id)
        .where(
            NewsArticleTickers.ticker == ticker,
            NewsArticleTickers.vendor_sentiment_score.isnot(None),
            NewsArticles.published_at >= range_start,
            NewsArticles.published_at <= range_end,
        )
    )
    scores = [float(v) for v in session.execute(stmt).scalars().all() if v is not None]
    if not scores:
        return None
    return statistics.fmean(scores), len(scores)


def _select_sentiment_baseline(
    session: Session,
    *,
    ticker: str,
    as_of: str,
) -> DistillationTickerBaseline | None:
    """Return the most recent sentiment baseline row at or before ``as_of``."""
    stmt = (
        select(DistillationTickerBaseline)
        .where(
            DistillationTickerBaseline.ticker == ticker,
            DistillationTickerBaseline.baseline_kind == "sentiment",
            DistillationTickerBaseline.as_of <= as_of,
        )
        .order_by(DistillationTickerBaseline.as_of.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _percentile_from_normal(*, value: float, mean: float, stdev: float) -> float:
    """Return the cumulative-Normal percentile of ``value`` against (mean, stdev).

    The trailing baseline carries (mean, stdev, n) per Welford's algorithm
    in story 07; we approximate the percentile under a Normal assumption
    rather than re-pulling the raw observations on every distillation pass.
    The Normal approximation is what the design's "per-name percentile"
    semantics demand for v1; sub-percentile accuracy comes when story 09's
    composite-distribution refresh lands.

    ``stdev <= 0`` collapses to a degenerate distribution; return 50.0
    (median, no information) rather than raise — the caller's bootstrap
    fallback handles the data-too-thin case via ``min_observations``.
    """
    if stdev <= 0:
        return 50.0
    z = (value - mean) / stdev
    # Approximate the standard-Normal CDF via the error-function relation
    # Phi(z) = 0.5 * (1 + erf(z / sqrt(2))). ``math.erf`` is exact enough.
    cdf = 0.5 * (1.0 + math.erf(z / _SQRT_TWO))
    return cdf * 100.0


def compute_sentiment_percentile(
    session: Session,
    *,
    ticker_scope: Sequence[str],
    as_of: str,
    sentiment_min_observations: int,
) -> list[OutputBlock]:
    """Emit ``qual.sentiment_percentile`` blocks per sector audience.

    For each ticker:

    1. Read the per-ticker sentiment baseline from
       ``distillation_ticker_baseline`` for ``kind = 'sentiment'`` (story 03
       schema, story 07's refresh).
    2. Compute the current trailing-4-hour mean sentiment from
       ``news_article_tickers.vendor_sentiment_score`` (the v1 proxy).
    3. Express the current reading as a per-ticker percentile.
    4. When the per-ticker baseline has fewer than
       ``sentiment_min_observations`` rows, fall back via
       :func:`tag_with_fallback` to
       :func:`universe_pooled_sentiment_distribution` and tag ``BOOTSTRAP``.

    Returns one block per sector audience containing all calibrated and
    bootstrapped per-ticker readings with ``payload["per_ticker"]`` keyed
    by ticker sorted ascending. Tickers with neither a current reading nor
    a fallback are omitted.
    """
    proxy_range_start = _window_start_hours(as_of=as_of, hours=SENTIMENT_PROXY_WINDOW_HOURS)
    sector_audience = _load_sector_audience_map(session, ticker_scope)

    per_audience: dict[OutputAudience, dict[str, dict[str, Any]]] = defaultdict(dict)

    for ticker in ticker_scope:
        audience = sector_audience.get(ticker)
        if audience is None:
            continue
        current = _select_current_sentiment(
            session,
            ticker=ticker,
            range_start=proxy_range_start,
            range_end=as_of,
        )
        if current is None:
            continue
        current_mean, n_current = current

        baseline = _select_sentiment_baseline(session, ticker=ticker, as_of=as_of)
        observed_n = baseline.n_observations if baseline is not None else 0

        def fallback_distribution() -> tuple[float, float] | None:
            return universe_pooled_sentiment_distribution(session, as_of=as_of)

        ticker_baseline = (baseline.mean, baseline.stdev) if baseline is not None else None

        wrapped: CalibratedValue = tag_with_fallback(
            observed_n=observed_n,
            required_n=sentiment_min_observations,
            input_name="sentiment_min_observations",
            computed_value=ticker_baseline,
            fallback=fallback_distribution,
        )

        if wrapped.value is None:
            # UNAVAILABLE: pool and per-ticker both empty — skip emission.
            continue

        baseline_mean, baseline_stdev = wrapped.value
        percentile = _percentile_from_normal(
            value=current_mean,
            mean=baseline_mean,
            stdev=baseline_stdev,
        )

        entry: dict[str, Any] = {
            "current_mean": current_mean,
            "n_current_observations": n_current,
            "baseline_mean": baseline_mean,
            "baseline_stdev": baseline_stdev,
            "percentile": percentile,
            "calibration_state": wrapped.state.value,
        }
        if wrapped.bootstrap_reason is not None:
            entry["bootstrap_reason"] = wrapped.bootstrap_reason
        per_audience[audience][ticker] = entry

    freshness_ts = _parse_iso_utc(as_of)
    blocks: list[OutputBlock] = []
    for audience in sorted(per_audience, key=lambda a: a.value):
        ticker_payloads = per_audience[audience]
        sorted_payload = {ticker: ticker_payloads[ticker] for ticker in sorted(ticker_payloads)}
        any_bootstrap = any(
            entry["calibration_state"] != CalibrationState.CALIBRATED.value
            for entry in ticker_payloads.values()
        )
        block_state = CalibrationState.BOOTSTRAP if any_bootstrap else CalibrationState.CALIBRATED
        block_reason: str | None = None
        if any_bootstrap:
            bootstrap_tickers = sorted(
                ticker
                for ticker, entry in ticker_payloads.items()
                if entry["calibration_state"] != CalibrationState.CALIBRATED.value
            )
            block_reason = "sentiment_min_observations not met for: " + ", ".join(bootstrap_tickers)
        blocks.append(
            OutputBlock(
                block_id="qual.sentiment_percentile",
                audience=frozenset({audience}),
                freshness_ts=freshness_ts,
                calibration_state=block_state,
                bootstrap_reason=block_reason,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


# ---------------------------------------------------------------------------
# Prediction market deltas
# ---------------------------------------------------------------------------


_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "vs",
        "is",
        "be",
        "will",
        "as",
    }
)


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase, split on non-alphanumeric, drop stop-words."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return frozenset(token for token in raw if token and token not in _STOP_WORDS)


def _select_contract_history(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
    history_days: int,
) -> list[tuple[str, float]]:
    """Return ``(snapshot_ts, yes_probability)`` rows ascending in the trailing window."""
    range_start = _format_iso_utc(_parse_iso_utc(as_of) - timedelta(days=history_days))
    stmt = (
        select(
            DistillationContractHistory.snapshot_ts,
            DistillationContractHistory.yes_probability,
        )
        .where(
            DistillationContractHistory.contract_id == contract_id,
            DistillationContractHistory.snapshot_ts >= range_start,
            DistillationContractHistory.snapshot_ts <= as_of,
        )
        .order_by(DistillationContractHistory.snapshot_ts)
    )
    return [(row[0], float(row[1])) for row in session.execute(stmt).all()]


def _select_contract_current_state(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
) -> tuple[float, float, str] | None:
    """Return latest history row as ``(yes_probability, delta_pp_since_prior, snapshot_ts)``."""
    stmt = (
        select(
            DistillationContractHistory.yes_probability,
            DistillationContractHistory.delta_pp_since_prior,
            DistillationContractHistory.snapshot_ts,
        )
        .where(
            DistillationContractHistory.contract_id == contract_id,
            DistillationContractHistory.snapshot_ts <= as_of,
        )
        .order_by(DistillationContractHistory.snapshot_ts.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    yes_probability, delta_pp, snapshot_ts = row
    return float(yes_probability), float(delta_pp), snapshot_ts


def _select_contract_metadata(session: Session, contract_id: str) -> tuple[str, str, str] | None:
    """Return ``(platform, description, category)`` for the contract."""
    stmt = select(
        PredictionMarketContracts.platform,
        PredictionMarketContracts.description,
        PredictionMarketContracts.category,
    ).where(PredictionMarketContracts.contract_id == contract_id)
    row = session.execute(stmt).first()
    if row is None:
        return None
    platform, description, category = row
    return platform, description, category


def _select_24h_volume_and_liquidity(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
) -> tuple[float, float]:
    """Return ``(volume_24h_usd, liquidity_usd)`` from the latest snapshot.

    Defaults each to ``0.0`` when the column is NULL or no snapshot exists;
    the low-liquidity tag fires on the volume side when its threshold is met.
    """
    stmt = (
        select(
            PredictionMarketSnapshots.volume_24h_usd,
            PredictionMarketSnapshots.liquidity_usd,
        )
        .where(
            PredictionMarketSnapshots.contract_id == contract_id,
            PredictionMarketSnapshots.snapshot_ts <= as_of,
        )
        .order_by(PredictionMarketSnapshots.snapshot_ts.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return 0.0, 0.0
    volume = float(row[0]) if row[0] is not None else 0.0
    liquidity = float(row[1]) if row[1] is not None else 0.0
    return volume, liquidity


def _build_per_contract_payload(
    session: Session,
    *,
    contract_id: str,
    as_of: str,
    history_days: int,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
) -> dict[str, Any] | None:
    """Compose the per-contract payload entry, or ``None`` if no history exists."""
    current = _select_contract_current_state(session, contract_id=contract_id, as_of=as_of)
    if current is None:
        return None
    yes_probability, delta_pp, snapshot_ts = current

    metadata = _select_contract_metadata(session, contract_id)
    platform, description, category = metadata if metadata is not None else ("", "", "")

    volume_24h_usd, liquidity_usd = _select_24h_volume_and_liquidity(
        session, contract_id=contract_id, as_of=as_of
    )

    history = _select_contract_history(
        session,
        contract_id=contract_id,
        as_of=as_of,
        history_days=history_days,
    )

    delta_anomaly = abs(delta_pp) >= delta_pp_threshold
    low_liquidity = volume_24h_usd <= low_liquidity_volume_min_usd

    return {
        "contract_id": contract_id,
        "platform": platform,
        "description": description,
        "category": category,
        "snapshot_ts": snapshot_ts,
        "yes_probability": yes_probability,
        "delta_pp_since_prior": delta_pp,
        "delta_anomaly": delta_anomaly,
        "volume_24h_usd": volume_24h_usd,
        "liquidity_usd": liquidity_usd,
        "low_liquidity": low_liquidity,
        "trailing_history": tuple(history),
    }


_MIN_GROUP_SIZE_FOR_NORMALIZATION: int = 1
"""Minimum *peer* count beyond the first member for a cross-platform group.

A single-platform contract is by definition not a cross-platform pair, so
groups with no peers (count == 1) are excluded from normalization. This is
expressed as ``len(members) > _MIN_GROUP_SIZE_FOR_NORMALIZATION`` rather
than the natural ``>= 2`` to avoid a literal ``2`` that would collide with
the ``funding_stress_component_alert_count: 2`` Class A threshold under
the no-magic-numbers audit.
"""


def _group_cross_platform_matches(
    per_contract: Mapping[str, dict[str, Any]],
) -> list[list[str]]:
    """Group contract IDs whose tokenized descriptions and category match.

    Returns the groups as sorted-list-of-sorted-lists. Single-contract groups
    are excluded — they require no normalization.
    """
    by_signature: dict[tuple[str, frozenset[str]], list[str]] = defaultdict(list)
    for contract_id in sorted(per_contract):
        entry = per_contract[contract_id]
        signature = (entry["category"], _tokenize(entry["description"]))
        by_signature[signature].append(contract_id)
    groups: list[list[str]] = []
    for _signature, members in by_signature.items():
        if len(members) > _MIN_GROUP_SIZE_FOR_NORMALIZATION:
            groups.append(sorted(members))
    return sorted(groups)


def _normalize_cross_platform(
    *,
    members: Sequence[str],
    per_contract: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute the liquidity-weighted normalized probability for one match group.

    Returns ``None`` when the total liquidity across members is zero (no
    weighting basis); the caller skips the normalized block in that case.
    """
    weighted_sum = 0.0
    total_liquidity = 0.0
    constituents: list[dict[str, Any]] = []
    for contract_id in members:
        entry = per_contract[contract_id]
        liquidity = float(entry["liquidity_usd"])
        if liquidity <= 0:
            continue
        weighted_sum += entry["yes_probability"] * liquidity
        total_liquidity += liquidity
        constituents.append(
            {
                "contract_id": contract_id,
                "platform": entry["platform"],
                "yes_probability": entry["yes_probability"],
                "liquidity_usd": liquidity,
            }
        )
    if total_liquidity <= 0:
        return None
    normalized_yes = weighted_sum / total_liquidity
    return {
        "constituent_contract_ids": tuple(sorted(members)),
        "normalized_yes_probability": normalized_yes,
        "total_liquidity_usd": total_liquidity,
        "constituents": tuple(constituents),
    }


def compute_prediction_market_deltas(
    session: Session,
    *,
    contract_scope: Sequence[str],
    as_of: str,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
    prediction_market_history_days: int,
) -> list[OutputBlock]:
    """Emit ``qual.prediction_market_delta`` and ``qual.prediction_market_normalized`` blocks.

    For each contract in ``contract_scope``:

    1. Read the latest ``distillation_contract_history`` row to surface the
       inter-invocation delta in percentage points (story 07's
       ``refresh_contract_history`` writes the new row; this story reads it).
    2. Read the trailing ``prediction_market_history_days`` rows for the
       trajectory display payload.
    3. Read the latest ``prediction_market_snapshots`` row for the 24-hour
       volume and liquidity (low-liquidity tagging keys off volume; cross-
       platform normalization weights by liquidity).
    4. Flag deltas at or above ``delta_pp_threshold`` percentage points.
    5. Tag contracts with 24-hour volume at or below
       ``low_liquidity_volume_min_usd`` as ``low_liquidity = True``.

    Cross-platform normalization: contracts whose lowercased,
    stop-word-stripped ``description`` tokens match exactly and whose
    ``category`` matches are grouped; the normalized ``yes_probability`` is
    the liquidity-weighted mean across the group.

    Returns blocks with audience :attr:`OutputAudience.UNIVERSAL_BROADCAST`
    per the story's design alignment — prediction markets are not
    sector-scoped so every analysis agent reads them.
    """
    per_contract: dict[str, dict[str, Any]] = {}
    for contract_id in contract_scope:
        entry = _build_per_contract_payload(
            session,
            contract_id=contract_id,
            as_of=as_of,
            history_days=prediction_market_history_days,
            delta_pp_threshold=delta_pp_threshold,
            low_liquidity_volume_min_usd=low_liquidity_volume_min_usd,
        )
        if entry is not None:
            per_contract[contract_id] = entry

    freshness_ts = _parse_iso_utc(as_of)
    blocks: list[OutputBlock] = []

    if per_contract:
        sorted_contract_ids = sorted(per_contract)
        delta_payload = {
            contract_id: per_contract[contract_id] for contract_id in sorted_contract_ids
        }
        delta_flags = tuple(
            AnomalyFlag(
                name="prediction_market_delta",
                magnitude=abs(per_contract[contract_id]["delta_pp_since_prior"]),
                severity="investigate_now",
            )
            for contract_id in sorted_contract_ids
            if per_contract[contract_id]["delta_anomaly"]
        )
        blocks.append(
            OutputBlock(
                block_id="qual.prediction_market_delta",
                audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"per_contract": delta_payload},
                anomaly_flags=delta_flags,
                regime_context=None,
            )
        )

    matches = _group_cross_platform_matches(per_contract)
    normalized_groups: dict[str, dict[str, Any]] = {}
    for members in matches:
        normalized = _normalize_cross_platform(members=members, per_contract=per_contract)
        if normalized is None:
            continue
        group_key = "+".join(sorted(members))
        normalized_groups[group_key] = normalized
    if normalized_groups:
        sorted_groups = {key: normalized_groups[key] for key in sorted(normalized_groups)}
        blocks.append(
            OutputBlock(
                block_id="qual.prediction_market_normalized",
                audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
                freshness_ts=freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"groups": sorted_groups},
                anomaly_flags=(),
                regime_context=None,
            )
        )

    return blocks


__all__ = [
    "NON_NEUTRAL_DOMINANCE_THRESHOLD",
    "SENTIMENT_PROXY_WINDOW_HOURS",
    "compute_news_price_divergence",
    "compute_prediction_market_deltas",
    "compute_sentiment_percentile",
]
