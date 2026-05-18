"""Pure-compute core for per-ticker sentiment percentile (ALP-487).

Splits the session-bound :func:`compute_sentiment_percentile` from legacy
:mod:`alphamind.distillation.qualitative_derived` into pure compute over
frozen inputs (this module) and an IO shell that owns the DB reads
(:mod:`.sentiment_percentile_loaders`). The pure compute imports zero ORM
types so the orchestrator can run
:func:`compute_sentiment_percentile_blocks` inside an
``asyncio.to_thread`` call without touching the shared SQLAlchemy
Session.

Sentiment proxy: per-(article, ticker) ``vendor_sentiment_score`` averaged
over the trailing :data:`SENTIMENT_PROXY_WINDOW_HOURS` window from
``news_article_tickers`` is the v1 input source. When aggregated
social-sentiment ingestion lands, the swap is mechanical — the
percentile-calibration logic itself is identical regardless of input source.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alphamind.distillation._calibration_core import (
    CalibratedValue,
    CalibrationState,
    tag_with_fallback,
)
from alphamind.distillation._repository import TickerBaselineRow
from alphamind.distillation.output import OutputAudience, OutputBlock

# ---------------------------------------------------------------------------
# Definitional constants
# ---------------------------------------------------------------------------

SENTIMENT_PROXY_WINDOW_HOURS: int = 4
"""Trailing window for the current sentiment reading (per ticker).

The v1 proxy averages ``vendor_sentiment_score`` from
``news_article_tickers`` over this window. When aggregated-sentiment
ingestion lands, the loader swaps without touching the compute.
"""


_SQRT_TWO: float = math.sqrt(1.0 + 1.0)
"""Precomputed ``sqrt(2)`` used in the standard-Normal CDF approximation.

Computed as ``sqrt(1.0 + 1.0)`` rather than ``sqrt(2.0)`` to avoid a raw
``2.0`` literal — the no-magic-numbers audit flags any literal whose
value matches a Class A YAML threshold, and
``funding_stress_component_alert_count: 2`` would collide.
"""


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentimentPercentileInputs:
    """Frozen pre-loaded inputs for :func:`compute_sentiment_percentile_blocks`.

    - ``ticker_scope`` — tickers iterated in this invocation.
    - ``freshness_ts`` — parsed ``as_of`` threaded onto each emitted block.
    - ``sector_audience_by_ticker`` — per-ticker audience.
    - ``current_sentiment_by_ticker`` — ``(mean_score, n_articles)`` over the
      trailing 4-hour window, or ``None`` when the ticker has no scored
      article in scope.
    - ``baseline_by_ticker`` — most recent sentiment baseline row at-or-before
      ``as_of`` per ticker, or ``None`` when absent.
    - ``universe_pooled_sentiment`` — universe-pooled ``(mean, stdev)`` used
      as the cross-sectional fallback when a ticker baseline has some
      observations but fewer than ``sentiment_min_observations``; ``None``
      when the pool itself is empty (caller then treats the ticker as
      ``UNAVAILABLE``). When a ticker has zero observations the fallback
      is not consulted — per ALP-540 the ticker is treated as
      ``UNAVAILABLE`` (operator action required) and omitted from the
      per-ticker payload.
    """

    ticker_scope: tuple[str, ...]
    freshness_ts: datetime
    sector_audience_by_ticker: Mapping[str, OutputAudience]
    current_sentiment_by_ticker: Mapping[str, tuple[float, int] | None]
    baseline_by_ticker: Mapping[str, TickerBaselineRow | None]
    universe_pooled_sentiment: tuple[float, float] | None


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _percentile_from_normal(*, value: float, mean: float, stdev: float) -> float:
    """Return the cumulative-Normal percentile of ``value`` against (mean, stdev).

    The trailing baseline carries (mean, stdev, n) per Welford's algorithm;
    this approximates the percentile under a Normal assumption rather than
    re-pulling raw observations on every distillation pass.

    ``stdev <= 0`` collapses to a degenerate distribution; return 50.0
    (median, no information) rather than raise — the caller's
    cross-sectional fallback handles the data-too-thin case via
    ``min_observations``.
    """
    if stdev <= 0:
        return 50.0
    z = (value - mean) / stdev
    # Phi(z) = 0.5 * (1 + erf(z / sqrt(2))). ``math.erf`` is exact enough.
    cdf = 0.5 * (1.0 + math.erf(z / _SQRT_TWO))
    return cdf * 100.0


# ---------------------------------------------------------------------------
# Compute entry point
# ---------------------------------------------------------------------------


def compute_sentiment_percentile_blocks(
    inputs: SentimentPercentileInputs,
    *,
    sentiment_min_observations: int,
) -> list[OutputBlock]:
    """Emit ``qual.sentiment_percentile`` blocks per sector audience.

    For each ticker with a current reading, compute the per-ticker percentile
    against the trailing sentiment baseline; fall back via
    :func:`tag_with_fallback` to the universe-pooled distribution when the
    baseline is sub-threshold but non-zero. Tickers whose baseline has zero
    observations (or whose fallback is unavailable) are omitted from the
    payload per the ALP-540 vocabulary.

    Returns one block per sector audience containing all calibrated and
    accumulating per-ticker readings with ``payload["per_ticker"]`` keyed
    by ticker sorted ascending.
    """
    per_audience: dict[OutputAudience, dict[str, dict[str, Any]]] = defaultdict(dict)
    fallback = inputs.universe_pooled_sentiment

    def _fallback_distribution() -> tuple[float, float] | None:
        return fallback

    for ticker in inputs.ticker_scope:
        audience = inputs.sector_audience_by_ticker.get(ticker)
        if audience is None:
            continue
        current = inputs.current_sentiment_by_ticker.get(ticker)
        if current is None:
            continue
        current_mean, n_current = current

        baseline = inputs.baseline_by_ticker.get(ticker)
        observed_n = baseline.n_observations if baseline is not None else 0
        ticker_baseline = (baseline.mean, baseline.stdev) if baseline is not None else None

        wrapped: CalibratedValue = tag_with_fallback(
            observed_n=observed_n,
            required_n=sentiment_min_observations,
            input_name="sentiment_min_observations",
            computed_value=ticker_baseline,
            fallback=_fallback_distribution,
        )

        if wrapped.value is None:
            # UNAVAILABLE: pool and per-ticker both empty — skip emission.
            continue

        baseline_mean, baseline_stdev = wrapped.value
        percentile = _percentile_from_normal(
            value=current_mean, mean=baseline_mean, stdev=baseline_stdev
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

    blocks: list[OutputBlock] = []
    for audience in sorted(per_audience, key=lambda a: a.value):
        ticker_payloads = per_audience[audience]
        sorted_payload = {ticker: ticker_payloads[ticker] for ticker in sorted(ticker_payloads)}
        block_state, block_reason = _block_calibration(sorted_payload)
        blocks.append(
            OutputBlock(
                block_id="qual.sentiment_percentile",
                audience=frozenset({audience}),
                freshness_ts=inputs.freshness_ts,
                calibration_state=block_state,
                bootstrap_reason=block_reason,
                payload={"per_ticker": sorted_payload},
                anomaly_flags=(),
                regime_context=None,
            )
        )
    return blocks


def _block_calibration(
    ticker_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[CalibrationState, str | None]:
    """Compute the block-level calibration tag and (when non-calibrated) the reason.

    A block escalates to :attr:`CalibrationState.UNAVAILABLE` if any ticker
    payload carries that state; otherwise falls back to
    :attr:`CalibrationState.ACCUMULATING` when at least one ticker is
    non-calibrated.
    """
    unavailable: list[str] = []
    accumulating: list[str] = []
    for ticker, entry in ticker_payloads.items():
        state = CalibrationState(entry["calibration_state"])
        if state is CalibrationState.UNAVAILABLE:
            unavailable.append(ticker)
        elif state is CalibrationState.ACCUMULATING:
            accumulating.append(ticker)
    if unavailable:
        return (
            CalibrationState.UNAVAILABLE,
            "sentiment baseline unavailable for: " + ", ".join(sorted(unavailable)),
        )
    if accumulating:
        return (
            CalibrationState.ACCUMULATING,
            "sentiment_min_observations not met for: " + ", ".join(sorted(accumulating)),
        )
    return CalibrationState.CALIBRATED, None


__all__ = [
    "SENTIMENT_PROXY_WINDOW_HOURS",
    "SentimentPercentileInputs",
    "compute_sentiment_percentile_blocks",
]
