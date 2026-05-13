"""Q1 price/volume anomaly detections - story 02-distillation/08a.

Two detections key off the indicator computations in this story:

- ``volume_anomaly``: today's volume exceeds ``volume_anomaly_sigma`` standard
  deviations above the 20-day trailing mean (from
  ``distillation_ticker_baseline`` for ``kind = 'volume'``).
- ``price_move_anomaly``: today's daily-bar move exceeds
  ``price_move_atr_multiple`` times ATR.

Both detections downgrade severity to ``investigate_if_persists`` when the
calibration state is ``bootstrap`` per
``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
section "Anomaly detections".
"""

from __future__ import annotations

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation.output import AnomalyFlag, AnomalySeverity


def _severity_for_state(state: CalibrationState) -> AnomalySeverity:
    """Map calibration state → default anomaly severity.

    A ``bootstrap``-tagged anomaly is genuinely weaker evidence than the
    same anomaly against per-ticker history; surfacing this through severity
    (rather than swallowing the flag) lets downstream consumers act on it
    while weighting conviction appropriately.
    """
    if state is CalibrationState.BOOTSTRAP:
        return "investigate_if_persists"
    return "investigate_now"


def detect_volume_anomaly(
    *,
    today_volume: float,
    baseline_mean: float,
    baseline_stdev: float,
    sigma_threshold: float,
    calibration_state: CalibrationState,
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
        severity=_severity_for_state(calibration_state),
    )


def detect_price_move_anomaly(
    *,
    price_move: float,
    atr: float,
    atr_multiple_threshold: float,
    calibration_state: CalibrationState,
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
        severity=_severity_for_state(calibration_state),
    )


__all__ = [
    "detect_price_move_anomaly",
    "detect_volume_anomaly",
]
