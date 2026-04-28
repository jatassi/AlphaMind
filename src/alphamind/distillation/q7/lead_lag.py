"""Lead-lag relationships with overdue and inversion flags.

Implements ``LeadLagPair``, ``compute_lead_lag``, ``_detect_overdue_and_inversion``,
``_select_recent_returns_window``, ``_select_pair_lag_estimate``,
``_LAG_TRACKING_RATIO``, and ``LEAD_LAG_LOOKBACK_DEFAULT_DAYS``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _format_iso_utc,
    _max_lagged_correlation,
    _select_close_series,
    _window_bounds,
    _zscore,
)
from alphamind.persistence.models import DistillationPairLag

# Ratio at which a lag asset is considered to have "tracked" the lead. A
# tracking lag's max-magnitude move within the pair window is at least this
# fraction of the lead's most recent return (in absolute units). 0.5 is a
# common "half-tracked" rule of thumb; not a tunable Class A threshold.
_LAG_TRACKING_RATIO: float = 0.5


# Default trailing window over which the per-pair z-score distribution for
# lead-lag overdue / inversion detection is computed. Story 08d does not
# specify a calibrated value; a quarter (~120 calendar days) is conventional
# for short-term lead-lag monitoring. Caller may override via
# ``lookback_window_days``.
LEAD_LAG_LOOKBACK_DEFAULT_DAYS: int = 120


@dataclass(frozen=True)
class LeadLagPair:
    """Configuration for one lead-lag pair the layer monitors.

    The five named pairs from
    ``docs/design/02-distillation-layer/threshold-calibration.md`` § Lead-lag
    each map to a :class:`LeadLagPair`. ``pair_key`` is the named identifier
    (``"semis_to_tech"`` etc.), ``lead_ticker`` and ``lag_ticker`` are the
    OHLCV-bar tickers driving each leg, and ``max_days`` is the per-pair
    ``_max_days`` bound from the configuration.
    """

    pair_key: str
    lead_ticker: str
    lag_ticker: str
    max_days: int


def _select_recent_returns_window(
    session: Session,
    *,
    ticker: str,
    as_of: datetime,
    window_days: int,
) -> list[float]:
    """Return ``window_days`` ascending day-over-day percentage returns up to ``as_of``."""
    range_start, range_end = _window_bounds(as_of=as_of, window_days=window_days)
    closes = _select_close_series(
        session, ticker=ticker, range_start=range_start, range_end=range_end
    )
    return [(later - earlier) / earlier for earlier, later in pairwise(closes) if earlier > 0.0]


def _detect_overdue_and_inversion(
    *,
    pair: LeadLagPair,
    lead_returns: list[float],
    lag_returns: list[float],
    overdue_lead_sigma: float,
) -> list[AnomalyFlag]:
    """Emit overdue and inversion flags for one lead-lag pair.

    Per ``external.md`` § quant 7f and story 08d:

    - **Overdue lag**: the most recent lead return has z-score (against its
      trailing distribution) at or above ``overdue_lead_sigma`` AND the
      lag has not tracked within the pair's ``max_days`` window.
    - **Inversion (regime-shift)**: the named "lag" asset moves first —
      the normal leader/follower has flipped. Detected by comparing
      forward-direction (lead→lag) and reverse-direction (lag→lead)
      lagged correlation across the recent window; the structural
      comparison uses every observation rather than a single argmax bar
      so transient noise does not flip the verdict. Magnitude gates on
      both legs require both sides to clear ``overdue_lead_sigma`` so
      the inversion is meaningful, not noise.
    """
    flags: list[AnomalyFlag] = []
    if not lead_returns or not lag_returns:
        return flags

    # The lead's "today" move is the most recent return.
    lead_latest = lead_returns[-1]
    lead_zscore = abs(_zscore(lead_latest, lead_returns[:-1]))

    # Tracking window: lag's most recent ``max_days`` returns.
    lag_window = lag_returns[-pair.max_days :]
    lag_max_magnitude = max((abs(r) for r in lag_window), default=0.0)
    # A lag has "tracked" when its magnitude in the window is comparable to
    # the lead's. The simple comparison: lag's max abs return is at least
    # half the lead's. Encoded as a named ratio rather than a magic literal.
    tracked = lag_max_magnitude >= _LAG_TRACKING_RATIO * abs(lead_latest)

    if lead_zscore >= overdue_lead_sigma and not tracked:
        flags.append(
            AnomalyFlag(
                name=f"overdue_lag_flag:{pair.pair_key}",
                magnitude=lead_zscore,
                severity="investigate_now",
            )
        )

    # Inversion: forward (lead→lag) vs. reverse (lag→lead) lagged
    # correlation across the recent window. When the reverse direction
    # strictly dominates, the named "lag" leads the named "lead". The
    # magnitude gate on both legs keeps single-bar coincidences from
    # firing the regime-shift signal.
    n = min(len(lead_returns), len(lag_returns), pair.max_days + 1)
    if n >= 2:
        lead_recent = lead_returns[-n:]
        lag_recent = lag_returns[-n:]
        forward_corr = _max_lagged_correlation(
            leading_returns=lead_recent,
            following_returns=lag_recent,
            max_lag_days=pair.max_days,
        )
        reverse_corr = _max_lagged_correlation(
            leading_returns=lag_recent,
            following_returns=lead_recent,
            max_lag_days=pair.max_days,
        )
        lag_max = max(lag_recent, key=lambda r: abs(r))
        lag_zscore = abs(_zscore(lag_max, lag_returns[:-1]))
        if (
            reverse_corr > forward_corr
            and lag_zscore >= overdue_lead_sigma
            and lead_zscore >= overdue_lead_sigma
        ):
            flags.append(
                AnomalyFlag(
                    name=f"lead_lag_inversion_flag:{pair.pair_key}",
                    magnitude=lag_zscore,
                    severity="investigate_now",
                )
            )

    return flags


def _select_pair_lag_estimate(
    session: Session,
    *,
    lead: str,
    lag: str,
    as_of: datetime,
) -> tuple[float, int, str] | None:
    """Return the most recent persisted ``(lag_days, n_events, state)`` triple."""
    as_of_str = _format_iso_utc(as_of)
    stmt = (
        select(
            DistillationPairLag.lead_lag_days_estimate,
            DistillationPairLag.n_pair_events,
            DistillationPairLag.calibration_state,
        )
        .where(
            DistillationPairLag.lead_ticker == lead,
            DistillationPairLag.lag_ticker == lag,
            DistillationPairLag.as_of <= as_of_str,
        )
        .order_by(DistillationPairLag.as_of.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    estimate, n_events, state = row
    return float(estimate), int(n_events), str(state)


def compute_lead_lag(
    session: Session,
    *,
    pairs: Sequence[LeadLagPair],
    as_of: datetime,
    overdue_lead_sigma: float,
    lookback_window_days: int = LEAD_LAG_LOOKBACK_DEFAULT_DAYS,
) -> list[OutputBlock]:
    """Compute per-pair lead-lag overdue and inversion flags.

    For each :class:`LeadLagPair` in ``pairs``:

    1. Read the persisted ``distillation_pair_lag`` row to surface the
       calibrated lead-lag estimate.
    2. Read the recent return windows for the lead and lag tickers.
    3. Emit the ``overdue_lag_flag`` and ``lead_lag_inversion_flag`` flags
       when the documented conditions are met.

    ``lookback_window_days`` is the trailing window over which the per-pair
    z-score distribution is computed; pairs with a longer ``max_days`` need
    proportionally more lookback to keep the distribution stable.

    Returns one :class:`OutputBlock` per pair, addressed to
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.
    """
    blocks: list[OutputBlock] = []
    for pair in pairs:
        # Read returns over the long-window cutoff so the trailing
        # distribution carries enough mass for a stable z-score.
        recent_lead = _select_recent_returns_window(
            session,
            ticker=pair.lead_ticker,
            as_of=as_of,
            window_days=max(lookback_window_days, pair.max_days + 1),
        )
        recent_lag = _select_recent_returns_window(
            session,
            ticker=pair.lag_ticker,
            as_of=as_of,
            window_days=max(lookback_window_days, pair.max_days + 1),
        )
        flags = _detect_overdue_and_inversion(
            pair=pair,
            lead_returns=recent_lead,
            lag_returns=recent_lag,
            overdue_lead_sigma=overdue_lead_sigma,
        )

        persisted = _select_pair_lag_estimate(
            session, lead=pair.lead_ticker, lag=pair.lag_ticker, as_of=as_of
        )
        if persisted is None:
            estimate_days = float(pair.max_days)
            n_pair_events = 0
            state = CalibrationState.BOOTSTRAP
            reason: str | None = f"pair_lag_history_missing:{pair.pair_key}: 0 < 1"
        else:
            estimate_days, n_pair_events, state_str = persisted
            state = CalibrationState(state_str)
            reason = (
                None
                if state is CalibrationState.CALIBRATED
                else f"pair_lag_calibration:{pair.pair_key}: state={state_str}"
            )

        payload = {
            "pair_lag": {
                pair.pair_key: {
                    "lead_ticker": pair.lead_ticker,
                    "lag_ticker": pair.lag_ticker,
                    "lead_lag_days_estimate": estimate_days,
                    "n_pair_events": n_pair_events,
                    "max_days": pair.max_days,
                }
            },
        }

        blocks.append(
            OutputBlock(
                block_id=f"{_BLOCK_NAMESPACE}.lead_lag.{pair.pair_key}",
                audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
                freshness_ts=as_of,
                calibration_state=state,
                bootstrap_reason=reason,
                payload=payload,
                anomaly_flags=tuple(flags),
                regime_context=None,
            )
        )
    return blocks
