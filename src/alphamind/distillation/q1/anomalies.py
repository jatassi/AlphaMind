"""Q1 price/volume anomaly detections - story 02-distillation/08a.

Two detections key off the indicator computations in this story:

- ``volume_anomaly``: today's volume exceeds ``volume_anomaly_sigma`` standard
  deviations above the 20-day trailing mean (from
  ``distillation_ticker_baseline`` for ``kind = 'volume'``).
- ``price_move_anomaly``: today's daily-bar move exceeds
  ``price_move_atr_multiple`` times ATR.

Both detections emit ``investigate_now`` at the producer; the
calibration-state cap in :mod:`alphamind.distillation._severity_cap` is
the single publishing-layer policy that downgrades severity based on
the surrounding block's state. The producer does not map calibration
state to severity — that policy lives once at the publishing layer so
the operator's exempt-flag-names configuration applies uniformly across
every emitting module.
"""

from __future__ import annotations

from alphamind.distillation.output import AnomalyFlag


def detect_volume_anomaly(
    *,
    today_volume: float,
    baseline_mean: float,
    baseline_stdev: float,
    sigma_threshold: float,
) -> AnomalyFlag | None:
    """Detect a volume anomaly relative to a per-ticker rolling baseline.

    Fires when ``today_volume - baseline_mean ≥ sigma_threshold * baseline_stdev``.
    The boundary is inclusive — at exactly the threshold, the flag fires.

    Returns ``None`` when:

    - The baseline has zero stdev (degenerate, no anomaly is computable).
    - The deviation is below the threshold.

    The reported magnitude is the standard-deviation multiple, directly
    interpretable against the configured threshold.
    """
    if baseline_stdev <= 0.0:
        return None
    deviation_sigma = (today_volume - baseline_mean) / baseline_stdev
    if deviation_sigma < sigma_threshold:
        return None
    return AnomalyFlag(
        name="volume_anomaly",
        magnitude=deviation_sigma,
        severity="investigate_now",
    )


def detect_price_move_anomaly(
    *,
    price_move: float,
    atr: float,
    atr_multiple_threshold: float,
) -> AnomalyFlag | None:
    """Detect a price-move anomaly relative to ATR.

    Fires when ``|price_move| / atr ≥ atr_multiple_threshold``. The boundary
    is inclusive — at exactly the threshold, the flag fires.

    Returns ``None`` when ``atr <= 0`` (degenerate, no anomaly is
    computable) or when the move is below the threshold. Magnitude is the
    ATR multiple.
    """
    if atr <= 0.0:
        return None
    multiple = abs(price_move) / atr
    if multiple < atr_multiple_threshold:
        return None
    return AnomalyFlag(
        name="price_move_anomaly",
        magnitude=multiple,
        severity="investigate_now",
    )


__all__ = [
    "detect_price_move_anomaly",
    "detect_volume_anomaly",
]
