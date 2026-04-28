"""Gap analysis — story 02-distillation/08a.

Implements the four pieces of gap analysis from
``docs/design/01-data-layer/external/quantitative.md`` § 1d:

1. :func:`analyze_gap` — overnight gap magnitude and classification.
2. :func:`detect_session_gap` — bar-level detection that wraps
   :func:`analyze_gap` with a threshold gate; returns a :class:`DetectedGap`
   or ``None``.
3. :func:`record_pending_gap_event` — append a fresh row to
   ``distillation_event_history`` with ``outcome = "pending"`` per story 07's
   sentinel convention; idempotent on rerun and never overwrites a resolved
   outcome.
4. :func:`resolve_gap_fill_probability` — per-ticker rate when the per-ticker
   event history meets ``min_events``; sector-pooled fallback otherwise;
   :attr:`CalibrationState.UNAVAILABLE` when neither is computable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamind.distillation.baselines import PENDING_OUTCOME
from alphamind.distillation.calibration import (
    CalibratedValue,
    sector_pooled_gap_fill_rate,
    tag_with_fallback,
)
from alphamind.distillation.normalization import atr_normalize
from alphamind.persistence.models import DistillationEventHistory

# ---------------------------------------------------------------------------
# Categorical labels for gap classification
# ---------------------------------------------------------------------------

GAP_KIND_FULL: str = "full"
"""Gap classification: open lies entirely outside the prior bar's range."""

GAP_KIND_PARTIAL: str = "partial"
"""Gap classification: open lies inside the prior bar's range."""

GAP_TREND_WITH: str = "with_trend"
"""Gap direction matches the prior trend."""

GAP_TREND_COUNTER: str = "counter_trend"
"""Gap direction opposes the prior trend."""


GapDirection = Literal["up", "down"]
TrendDirection = Literal["up", "down", "flat"]


# ---------------------------------------------------------------------------
# Pure analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapAnalysisResult:
    """Gap classification plus magnitude in absolute and ATR-relative terms."""

    gap_absolute: float
    gap_atr_ratio: float
    direction: GapDirection
    kind: str
    trend_classification: str


def analyze_gap(
    *,
    today_open: float,
    prior_close: float,
    prior_high: float,
    prior_low: float,
    atr_14d: float,
    trend_direction: TrendDirection,
) -> GapAnalysisResult:
    """Classify a gap given the open vs the prior bar.

    The classification follows
    ``docs/design/01-data-layer/external/quantitative.md`` § 1d:

    - **Full** when the open is above the prior high (gap up) or below the
      prior low (gap down) — the open lies entirely outside the prior bar's
      range.
    - **Partial** otherwise — there is some gap from the prior close but
      the open still sits inside the prior bar's range.
    - **With-trend** / **counter-trend** is decided by whether the gap
      direction matches ``trend_direction``.

    ``atr_14d`` must be positive — a non-positive ATR is a data error per
    :func:`alphamind.distillation.normalization.atr_normalize`.
    """
    gap_absolute = today_open - prior_close
    gap_atr_ratio = atr_normalize(gap_absolute, atr_14d)
    direction: GapDirection = "up" if gap_absolute > 0.0 else "down"

    if direction == "up":
        kind = GAP_KIND_FULL if today_open > prior_high else GAP_KIND_PARTIAL
        trend_match = trend_direction == "up"
    else:
        kind = GAP_KIND_FULL if today_open < prior_low else GAP_KIND_PARTIAL
        trend_match = trend_direction == "down"
    trend_classification = GAP_TREND_WITH if trend_match else GAP_TREND_COUNTER

    return GapAnalysisResult(
        gap_absolute=gap_absolute,
        gap_atr_ratio=gap_atr_ratio,
        direction=direction,
        kind=kind,
        trend_classification=trend_classification,
    )


# ---------------------------------------------------------------------------
# Detection — gate on the threshold and surface a DetectedGap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectedGap:
    """A gap that cleared the detection threshold and should produce an event row.

    The carrier is intentionally minimal — :func:`record_pending_gap_event`
    only needs the ticker, event-ts, direction, and magnitude. Richer
    classification comes from :func:`analyze_gap`'s :class:`GapAnalysisResult`,
    which the caller assembles into the output payload.
    """

    ticker: str
    event_ts: str
    direction: GapDirection
    magnitude_atr_multiple: float


def detect_session_gap(
    *,
    ticker: str,
    today_open: float,
    prior_close: float,
    atr_14d: float,
    event_ts: str,
    min_atr_multiple: float,
) -> DetectedGap | None:
    """Return a :class:`DetectedGap` if the gap clears ``min_atr_multiple``.

    A gap whose magnitude is below the threshold returns ``None`` — the
    caller skips both the payload and the event-history write. Full vs
    partial classification lives in :func:`analyze_gap`; detection only
    gates magnitude.
    """
    gap_absolute = today_open - prior_close
    if atr_14d <= 0:
        return None
    magnitude = abs(gap_absolute) / atr_14d
    if magnitude < min_atr_multiple:
        return None
    direction: GapDirection = "up" if gap_absolute > 0.0 else "down"
    return DetectedGap(
        ticker=ticker,
        event_ts=event_ts,
        direction=direction,
        magnitude_atr_multiple=magnitude,
    )


# ---------------------------------------------------------------------------
# Pending event recording
# ---------------------------------------------------------------------------


def record_pending_gap_event(
    session: Session,
    detected: DetectedGap,
    *,
    ingested_at: str,
) -> None:
    """Append a pending ``distillation_event_history`` row, idempotent on rerun.

    Behavior matches the spec in
    ``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
    § Gap analysis: "On detecting a new gap at the start of the session,
    append a fresh row to ``distillation_event_history`` with
    ``outcome = NULL`` (story 07's refresh entry point owns the write; this
    story passes the detected event through)."

    Note the schema-level ``outcome`` column is NOT NULL; the
    "pending" sentinel string is the in-table representation of an
    unresolved event per story 07's convention. A row already present at
    ``(ticker, "gap", event_ts)`` is not modified — preserves any prior
    resolved outcome on rerun.
    """
    existing = session.execute(
        select(DistillationEventHistory).where(
            DistillationEventHistory.ticker == detected.ticker,
            DistillationEventHistory.event_kind == "gap",
            DistillationEventHistory.event_ts == detected.event_ts,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        DistillationEventHistory(
            ticker=detected.ticker,
            event_kind="gap",
            event_ts=detected.event_ts,
            direction=detected.direction,
            magnitude_atr_multiple=detected.magnitude_atr_multiple,
            outcome=PENDING_OUTCOME,
            outcome_observed_at=None,
            ingested_at=ingested_at,
        )
    )


# ---------------------------------------------------------------------------
# Gap-fill probability lookup
# ---------------------------------------------------------------------------


def _per_ticker_resolved_gap_fill_rate(
    session: Session,
    *,
    ticker: str,
    as_of: str,
) -> tuple[float | None, int]:
    """Return ``(rate, resolved_count)`` for ``ticker`` at or before ``as_of``.

    Counts only resolved events (``outcome != "pending"``). Returns
    ``(None, 0)`` when no resolved events exist.
    """
    base = (
        select(func.count())
        .select_from(DistillationEventHistory)
        .where(
            DistillationEventHistory.ticker == ticker,
            DistillationEventHistory.event_kind == "gap",
            DistillationEventHistory.outcome != PENDING_OUTCOME,
            DistillationEventHistory.event_ts <= as_of,
        )
    )
    resolved = int(session.execute(base).scalar_one())
    if resolved == 0:
        return None, 0
    filled_stmt = base.where(DistillationEventHistory.outcome == "filled")
    filled = int(session.execute(filled_stmt).scalar_one())
    return float(filled) / float(resolved), resolved


def resolve_gap_fill_probability(
    session: Session,
    *,
    ticker: str,
    sector: str,
    as_of: str,
    min_events: int,
) -> CalibratedValue:
    """Look up the gap-fill probability with the standard fallback chain.

    Three branches:

    - Per-ticker resolved-event count ≥ ``min_events`` → per-ticker rate,
      :attr:`CalibrationState.CALIBRATED`.
    - Below threshold but the sector pool has events → sector-pooled rate,
      :attr:`CalibrationState.BOOTSTRAP`.
    - Sector pool also empty → :attr:`CalibrationState.UNAVAILABLE`.

    The fallback path reuses
    :func:`alphamind.distillation.calibration.sector_pooled_gap_fill_rate`
    so the pooled-rate query lives in one place.
    """
    per_ticker_rate, resolved = _per_ticker_resolved_gap_fill_rate(
        session, ticker=ticker, as_of=as_of
    )
    return tag_with_fallback(
        observed_n=resolved,
        required_n=min_events,
        input_name="gap_fill_min_events",
        computed_value=per_ticker_rate,
        fallback=lambda: sector_pooled_gap_fill_rate(session, sector=sector, as_of=as_of),
    )


__all__ = [
    "GAP_KIND_FULL",
    "GAP_KIND_PARTIAL",
    "GAP_TREND_COUNTER",
    "GAP_TREND_WITH",
    "DetectedGap",
    "GapAnalysisResult",
    "GapDirection",
    "TrendDirection",
    "analyze_gap",
    "detect_session_gap",
    "record_pending_gap_event",
    "resolve_gap_fill_probability",
]
