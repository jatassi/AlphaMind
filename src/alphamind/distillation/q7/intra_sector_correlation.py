"""Intra-sector pairwise correlation matrices and divergence detection.

Implements ``compute_intra_sector_correlation``, ``_detect_pair_divergence``,
and ``_persist_correlation_divergence_events`` from the Q7 cross-asset layer.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
    _format_iso_utc,
    _log_returns_from_closes,
    _select_close_series,
    _window_bounds,
)
from alphamind.persistence.models import DistillationEventHistory


def _detect_pair_divergence(
    *,
    short_matrix: dict[str, dict[str, float]],
    long_matrix: dict[str, dict[str, float]],
    divergence_sigma: float,
) -> list[AnomalyFlag]:
    """Emit one :class:`AnomalyFlag` per sector pair whose correlation diverged.

    The baseline distribution is the population of off-diagonal long-window
    correlations within the sector; a pair fires when its short-window
    correlation deviates from the long-window value by at least
    ``divergence_sigma`` multiples of that distribution's standard deviation.
    """
    tickers = sorted(short_matrix)
    long_values: list[float] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            long_values.append(long_matrix[row][col])
    # Pairs require at least one off-diagonal entry (i.e., a non-singleton
    # ticker set); pstdev needs at least one observation to be defined.
    if not long_values:
        return []
    sigma = statistics.pstdev(long_values)
    if sigma == 0.0:
        return []
    flags: list[AnomalyFlag] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_corr = short_matrix[row][col]
            long_corr = long_matrix[row][col]
            deviation = abs(short_corr - long_corr)
            magnitude = deviation / sigma
            if magnitude >= divergence_sigma:
                flags.append(
                    AnomalyFlag(
                        name=f"intra_sector_correlation_divergence:{row}:{col}",
                        magnitude=magnitude,
                        severity="investigate_if_persists",
                    )
                )
    return flags


def _persist_correlation_divergence_events(
    session: Session,
    *,
    flags: Sequence[AnomalyFlag],
    as_of: datetime,
) -> None:
    """Persist one ``correlation_divergence`` event row per pair-divergence flag.

    The event row's ``ticker`` carries the lead leg of the pair and
    ``direction`` carries the lag leg, so the (lead, kind, ts) primary key
    remains unique. ``magnitude_atr_multiple`` carries the divergence
    z-score (re-using the column for visibility — the column is generic
    enough that the q7 reader treats it as the divergence magnitude).

    Insertion is idempotent: rows already present at the same
    ``(ticker, event_kind, event_ts)`` triple are not duplicated.
    """
    as_of_iso = _format_iso_utc(as_of)
    for flag in flags:
        # Flag name shape: ``intra_sector_correlation_divergence:<lead>:<lag>``.
        try:
            _prefix, lead, lag = flag.name.split(":")
        except ValueError:
            continue
        existing = session.execute(
            select(DistillationEventHistory).where(
                DistillationEventHistory.ticker == lead,
                DistillationEventHistory.event_kind == "correlation_divergence",
                DistillationEventHistory.event_ts == as_of_iso,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            DistillationEventHistory(
                ticker=lead,
                event_kind="correlation_divergence",
                event_ts=as_of_iso,
                direction=lag,
                magnitude_atr_multiple=float(flag.magnitude),
                outcome="pending",
                outcome_observed_at=None,
                ingested_at=as_of_iso,
            )
        )


def compute_intra_sector_correlation(
    session: Session,
    *,
    sector: str,
    sector_tickers: Sequence[str],
    as_of: datetime,
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
) -> list[OutputBlock]:
    """Compute the per-sector intra-sector correlation block.

    For ``sector`` with ``sector_tickers`` returns one
    :class:`OutputBlock` whose payload carries:

    - ``short_window.correlation_matrix`` — pairwise short-window matrix.
    - ``long_window.correlation_matrix`` — pairwise long-window matrix.
    - ``short_window.window_days`` / ``long_window.window_days`` —
      window sizes for the audit trail.

    Pair-level divergence detection is attached as
    :class:`AnomalyFlag` instances on the block: a pair fires when the
    short-window correlation deviates from the long-window correlation by
    at least ``divergence_sigma`` multiples of the standard deviation of
    the long-window correlations. Each fired flag also writes a
    ``correlation_divergence`` event row to ``distillation_event_history``
    for resolution lookup on subsequent invocations.
    """
    long_start, range_end = _window_bounds(as_of=as_of, window_days=long_window_days)

    short_returns: dict[str, Sequence[float]] = {}
    long_returns: dict[str, Sequence[float]] = {}
    short_n_min = math.inf
    long_n_min = math.inf
    for ticker in sector_tickers:
        # One query per ticker: read the full long-window close series and
        # slice the short window from its tail. Avoids N redundant queries
        # for an otherwise overlapping range.
        long_closes = _select_close_series(
            session,
            ticker=ticker,
            range_start=long_start,
            range_end=range_end,
        )
        long_returns[ticker] = _log_returns_from_closes(long_closes)
        short_returns[ticker] = list(long_returns[ticker])[-short_window_days:]
        short_n_min = min(short_n_min, len(short_returns[ticker]))
        long_n_min = min(long_n_min, len(long_returns[ticker]))

    short_matrix = _correlation_matrix(short_returns)
    long_matrix = _correlation_matrix(long_returns)

    short_n = 0 if short_n_min is math.inf else int(short_n_min)
    long_n = 0 if long_n_min is math.inf else int(long_n_min)
    state, reason = _calibration_for_window(
        n_observations=min(short_n, long_n),
        required=short_window_days,
        input_name="correlation_short_days",
    )

    flags = _detect_pair_divergence(
        short_matrix=short_matrix,
        long_matrix=long_matrix,
        divergence_sigma=divergence_sigma,
    )

    _persist_correlation_divergence_events(session, flags=flags, as_of=as_of)
    session.flush()

    payload = {
        "short_window": {
            "correlation_matrix": short_matrix,
            "window_days": short_window_days,
            "n_observations": short_n,
        },
        "long_window": {
            "correlation_matrix": long_matrix,
            "window_days": long_window_days,
            "n_observations": long_n,
        },
        "sector": sector,
    }
    block = OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intra_sector_correlation.{sector}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=tuple(flags),
        regime_context=None,
    )
    return [block]
