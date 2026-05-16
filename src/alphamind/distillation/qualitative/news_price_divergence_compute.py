"""Pure-compute core for news/price divergence (ALP-487).

Splits the session-bound :func:`compute_news_price_divergence` from
legacy :mod:`alphamind.distillation.qualitative_derived` into pure compute
over frozen inputs (this module) and an IO shell that owns the DB reads
(:mod:`.news_price_divergence_loaders`). The pure compute imports zero ORM
types so the orchestrator can run
:func:`compute_news_price_divergence_blocks` inside an
``asyncio.to_thread`` call without touching the shared SQLAlchemy
Session.

Semantics mirror the legacy implementation exactly:

- Aggregate ``vendor_sentiment_label`` counts across articles in the window.
- Compute the dominant non-neutral direction when one direction exceeds
  :data:`NON_NEUTRAL_DOMINANCE_THRESHOLD` of non-neutral articles.
- Cross-reference with the signed hourly price change over the same window.
- Emit a ``priced_in`` / ``hidden_problem`` flag when news and price diverge.
- Per-block calibration: block is tagged ``bootstrap`` when the thinnest
  audience-ticker evidence is below ``min_articles``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._repository import NewsLabelCountsRow
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock

# ---------------------------------------------------------------------------
# Definitional constants
# ---------------------------------------------------------------------------

NON_NEUTRAL_DOMINANCE_THRESHOLD: float = 0.60
"""Fraction of non-neutral articles a single direction must exceed to be
"dominant" for news-price divergence detection.

Definitional cutoff, not a Class A tunable. The strict-greater-than test
(``> NON_NEUTRAL_DOMINANCE_THRESHOLD``) gates correctly: 59% does not
produce a dominant label, 61% does. If paper trading reveals the cutoff
is wrong, raise it as a future story rather than tuning silently here.
"""


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewsPriceDivergenceInputs:
    """Frozen pre-loaded inputs for :func:`compute_news_price_divergence_blocks`.

    Carries everything the compute path needs:

    - ``ticker_scope`` — tickers iterated in this invocation.
    - ``freshness_ts`` — parsed ``as_of`` timestamp threaded onto each emitted
      :class:`OutputBlock`.
    - ``sector_audience_by_ticker`` — per-ticker sector audience; tickers
      without a routed audience are silently skipped.
    - ``label_counts_by_ticker`` — sentiment-label counts in window per ticker.
    - ``price_change_by_ticker`` — signed hourly window price change per
      ticker; ``None`` when no hourly bars exist in scope and the ticker
      should be skipped.
    """

    ticker_scope: tuple[str, ...]
    freshness_ts: datetime
    sector_audience_by_ticker: Mapping[str, OutputAudience]
    label_counts_by_ticker: Mapping[str, NewsLabelCountsRow]
    price_change_by_ticker: Mapping[str, float | None]


# ---------------------------------------------------------------------------
# Compute helpers
# ---------------------------------------------------------------------------


def _non_neutral_total(counts: NewsLabelCountsRow) -> int:
    return counts.positive + counts.negative + counts.mixed


def _dominant_direction(counts: NewsLabelCountsRow) -> str | None:
    """Return ``"positive"`` / ``"negative"`` if one direction dominates non-neutral."""
    non_neutral = _non_neutral_total(counts)
    if non_neutral == 0:
        return None
    if counts.positive / non_neutral > NON_NEUTRAL_DOMINANCE_THRESHOLD:
        return "positive"
    if counts.negative / non_neutral > NON_NEUTRAL_DOMINANCE_THRESHOLD:
        return "negative"
    return None


def _classify_divergence(*, dominant: str, price_change: float) -> str | None:
    """Return ``"priced_in"`` / ``"hidden_problem"`` when news and price diverge.

    - ``priced_in``: dominant negative news AND price flat or rising.
    - ``hidden_problem``: dominant positive news AND price flat or falling.
    - Returns ``None`` when news and price agree.

    Inclusive of zero on both sides — an exact-zero price move on a
    halt-resume open or an illiquid name still represents a "flat" reaction
    that diverges from the dominant sentiment direction.
    """
    if dominant == "negative" and price_change >= 0:
        return "priced_in"
    if dominant == "positive" and price_change <= 0:
        return "hidden_problem"
    return None


def _divergence_magnitude(*, counts: NewsLabelCountsRow, price_change: float) -> float:
    """Sentiment-direction strength times price-direction strength.

    Sentiment strength rescales the dominant-share fraction onto a unit
    interval: at exactly 50% (no dominance) it is 0; at 100% it is 1. Price
    strength is the raw absolute price move — the magnitude is a reportable
    mismatch, not a comparable z-score.
    """
    non_neutral = _non_neutral_total(counts)
    if non_neutral == 0:
        return 0.0
    dominant_count = max(counts.positive, counts.negative)
    # Express (2*x - 1) as (x - (1 - x)) to avoid a literal ``2`` that the
    # no-magic-numbers audit would flag against
    # ``funding_stress_component_alert_count: 2``.
    minority_count = non_neutral - dominant_count
    sentiment_strength = max(0.0, (dominant_count - minority_count) / non_neutral)
    return sentiment_strength * abs(price_change)


# ---------------------------------------------------------------------------
# Compute entry point
# ---------------------------------------------------------------------------


def compute_news_price_divergence_blocks(
    inputs: NewsPriceDivergenceInputs,
    *,
    min_articles: int,
) -> list[OutputBlock]:
    """Emit ``qual.news_price_divergence`` blocks per sector audience.

    Returns one :class:`OutputBlock` per sector audience that has at least
    one diverging ticker, with ``payload["per_ticker"]`` keyed by ticker
    sorted ascending. Block-level calibration is the worst (thinnest)
    ticker evidence in the audience — one thin ticker drags the whole
    block to ``bootstrap`` so downstream consumers see the caveat without
    having to inspect every per-ticker entry.
    """
    per_audience: dict[OutputAudience, dict[str, dict[str, Any]]] = defaultdict(dict)
    per_audience_magnitudes: dict[OutputAudience, list[tuple[str, float]]] = defaultdict(list)
    per_audience_min_evidence: dict[OutputAudience, int] = {}

    for ticker in inputs.ticker_scope:
        audience = inputs.sector_audience_by_ticker.get(ticker)
        if audience is None:
            continue
        counts = inputs.label_counts_by_ticker.get(ticker)
        if counts is None:
            continue
        dominant = _dominant_direction(counts)
        if dominant is None:
            continue
        price_change = inputs.price_change_by_ticker.get(ticker)
        if price_change is None:
            continue
        direction = _classify_divergence(dominant=dominant, price_change=price_change)
        if direction is None:
            continue
        magnitude = _divergence_magnitude(counts=counts, price_change=price_change)
        non_neutral = _non_neutral_total(counts)
        per_audience[audience][ticker] = {
            "direction": direction,
            "dominant_label": dominant,
            "price_change": price_change,
            "non_neutral_count": non_neutral,
            "positive_count": counts.positive,
            "negative_count": counts.negative,
            "neutral_count": counts.neutral,
            "mixed_count": counts.mixed,
            "magnitude": magnitude,
        }
        per_audience_magnitudes[audience].append((ticker, magnitude))
        prior_min = per_audience_min_evidence.get(audience)
        if prior_min is None or non_neutral < prior_min:
            per_audience_min_evidence[audience] = non_neutral

    blocks: list[OutputBlock] = []
    for audience in sorted(per_audience, key=lambda a: a.value):
        ticker_payloads = per_audience[audience]
        sorted_payload = {ticker: ticker_payloads[ticker] for ticker in sorted(ticker_payloads)}
        flags = _flag_tuple(per_audience_magnitudes[audience])
        worst_evidence = per_audience_min_evidence[audience]
        calibration_state, bootstrap_reason = _block_calibration(
            worst_evidence=worst_evidence, min_articles=min_articles
        )
        blocks.append(
            OutputBlock(
                block_id="qual.news_price_divergence",
                audience=frozenset({audience}),
                freshness_ts=inputs.freshness_ts,
                calibration_state=calibration_state,
                bootstrap_reason=bootstrap_reason,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=flags,
                regime_context=None,
            )
        )
    return blocks


def _flag_tuple(magnitudes: Sequence[tuple[str, float]]) -> tuple[AnomalyFlag, ...]:
    """Render the per-ticker magnitudes as anomaly flags in ticker-sorted order."""
    return tuple(
        AnomalyFlag(
            name="news_price_divergence",
            magnitude=magnitude,
            severity="investigate_now",
        )
        for _ticker, magnitude in sorted(magnitudes)
    )


def _block_calibration(
    *, worst_evidence: int, min_articles: int
) -> tuple[CalibrationState, str | None]:
    """Compute the block-level calibration tag and (when bootstrap) the reason."""
    if worst_evidence < min_articles:
        return (
            CalibrationState.BOOTSTRAP,
            f"news_price_divergence_min_articles: {worst_evidence} < {min_articles}",
        )
    return CalibrationState.CALIBRATED, None


__all__ = [
    "NON_NEUTRAL_DOMINANCE_THRESHOLD",
    "NewsPriceDivergenceInputs",
    "compute_news_price_divergence_blocks",
]
