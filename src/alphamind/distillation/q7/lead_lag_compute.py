"""Pure compute for Q7 lead-lag relationships (ALP-486).

Splits the lead-lag compute path along the compute/load boundary. This
module is ORM-free; the session-bound read surface lives in
:mod:`.lead_lag_loaders`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _max_lagged_correlation,
    _zscore,
)

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


def _detect_overdue_and_inversion(
    *,
    pair: LeadLagPair,
    lead_returns: Sequence[float],
    lag_returns: Sequence[float],
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
        lead_recent = list(lead_returns[-n:])
        lag_recent = list(lag_returns[-n:])
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


@dataclass(frozen=True, slots=True)
class LeadLagPairInputs:
    """Pre-loaded per-pair inputs for the lead-lag pure compute.

    ``persisted`` carries the most recent ``distillation_pair_lag`` row's
    ``(lead_lag_days_estimate, n_pair_events, calibration_state)`` triple,
    or ``None`` when no row exists at or before ``as_of`` (the BOOTSTRAP
    branch).
    """

    pair: LeadLagPair
    lead_returns: tuple[float, ...]
    lag_returns: tuple[float, ...]
    persisted: tuple[float, int, str] | None


def compute_lead_lag_pure(
    *,
    pair_inputs: Sequence[LeadLagPairInputs],
    overdue_lead_sigma: float,
    freshness_ts: datetime | None = None,
) -> list[OutputBlock]:
    """Pure compute of the per-pair lead-lag blocks.

    For each :class:`LeadLagPairInputs`:

    1. Detect overdue and inversion flags via :func:`_detect_overdue_and_inversion`.
    2. Project the persisted ``distillation_pair_lag`` snapshot into the
       block payload; absent rows route through the BOOTSTRAP branch.
    3. Emit one :class:`OutputBlock` addressed to
       :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`.

    ``freshness_ts`` defaults to ``None`` for tests that don't care; the
    loader passes ``as_of``.
    """
    blocks: list[OutputBlock] = []
    for inputs in pair_inputs:
        pair = inputs.pair
        flags = _detect_overdue_and_inversion(
            pair=pair,
            lead_returns=inputs.lead_returns,
            lag_returns=inputs.lag_returns,
            overdue_lead_sigma=overdue_lead_sigma,
        )

        if inputs.persisted is None:
            estimate_days = float(pair.max_days)
            n_pair_events = 0
            state = CalibrationState.BOOTSTRAP
            reason: str | None = f"pair_lag_history_missing:{pair.pair_key}: 0 < 1"
        else:
            estimate_days_raw, n_pair_events_raw, state_str = inputs.persisted
            estimate_days = float(estimate_days_raw)
            n_pair_events = int(n_pair_events_raw)
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
                freshness_ts=(
                    freshness_ts if freshness_ts is not None else datetime.min.replace(tzinfo=UTC)
                ),
                calibration_state=state,
                bootstrap_reason=reason,
                payload=payload,
                anomaly_flags=tuple(flags),
                regime_context=None,
            )
        )
    return blocks


__all__ = [
    "LEAD_LAG_LOOKBACK_DEFAULT_DAYS",
    "LeadLagPair",
    "LeadLagPairInputs",
    "compute_lead_lag_pure",
]
