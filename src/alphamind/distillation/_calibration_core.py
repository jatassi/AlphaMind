"""Pure-compute helpers for the calibration framework (ALP-467 extraction).

The cross-sectional fallback functions (``sector_pooled_*``,
``universe_pooled_*``) live in :mod:`alphamind.distillation.calibration`
and reach the database; the pure tag-vs-fallback boundary helper plus the
:class:`CalibratedValue` dataclass live here so the q1/q3 compute cores can
import them without picking up a sqlalchemy edge.

This module is intentionally leaf-shaped — no first-party imports beyond
:mod:`alphamind._kernel.calibration` (the StrEnum vocabulary). The legacy
``distillation/calibration.py`` re-exports these symbols unchanged so
existing call sites continue to work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from alphamind._kernel.calibration import CALIBRATION_STATE_VALUES, CalibrationState

__all__ = [
    "CALIBRATION_STATE_VALUES",
    "CalibratedValue",
    "CalibrationState",
    "decide_calibration_state",
    "tag_with_fallback",
]


@dataclass(frozen=True)
class CalibratedValue:
    """A computed value paired with its calibration tag.

    Mirrors the legacy :class:`alphamind.distillation.calibration.CalibratedValue`
    shape exactly — re-exported from there for backward compatibility.
    """

    value: Any
    state: CalibrationState
    bootstrap_reason: str | None


def decide_calibration_state(*, observed_n: int, required_n: int) -> CalibrationState:
    """Return CALIBRATED when ``observed_n >= required_n``, else BOOTSTRAP."""
    if observed_n >= required_n:
        return CalibrationState.CALIBRATED
    return CalibrationState.BOOTSTRAP


def tag_with_fallback(
    *,
    observed_n: int,
    required_n: int,
    input_name: str,
    computed_value: Any,
    fallback: Callable[[], Any],
) -> CalibratedValue:
    """Decide the calibration state and wrap the result.

    Three branches:

    - ``observed_n >= required_n`` — ``computed_value`` carried with
      :attr:`CalibrationState.CALIBRATED`; the fallback is not invoked.
    - ``observed_n < required_n`` and ``fallback()`` returns a value —
      fallback value carried with :attr:`CalibrationState.BOOTSTRAP` and a
      compact ``"<input_name>: <observed_n> < <required_n>"`` reason.
    - ``observed_n < required_n`` and ``fallback()`` returns ``None`` —
      ``value=None`` with :attr:`CalibrationState.UNAVAILABLE` and a reason
      noting the pool was empty.
    """
    state = decide_calibration_state(observed_n=observed_n, required_n=required_n)
    if state is CalibrationState.CALIBRATED:
        return CalibratedValue(
            value=computed_value,
            state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
    reason = f"{input_name}: {observed_n} < {required_n}"
    fallback_value = fallback()
    if fallback_value is None:
        return CalibratedValue(
            value=None,
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=f"{reason} (cross-sectional pool empty)",
        )
    return CalibratedValue(
        value=fallback_value,
        state=CalibrationState.BOOTSTRAP,
        bootstrap_reason=reason,
    )
