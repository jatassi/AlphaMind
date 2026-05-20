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
    "combine_calibration_states",
    "decide_calibration_state",
    "tag_with_fallback",
]


@dataclass(frozen=True)
class CalibratedValue:
    """A computed value paired with its calibration tag.

    ``bootstrap_reason`` is the operator-readable string explaining why the
    state is non-calibrated. The field name predates the ALP-540 three-state
    rename and is kept stable here to avoid churning the ~30 dataclasses that
    forward it through the pipeline; the name is internal-Python-only and
    never appears in operator-facing artifacts.
    """

    value: Any
    state: CalibrationState
    bootstrap_reason: str | None


def decide_calibration_state(*, observed_n: int, required_n: int) -> CalibrationState:
    """Map ``(observed_n, required_n)`` to the three-state calibration vocabulary.

    - ``observed_n >= required_n`` → :attr:`CalibrationState.CALIBRATED`.
    - ``0 < observed_n < required_n`` → :attr:`CalibrationState.ACCUMULATING`
      (collector healthy, just need more time).
    - ``observed_n == 0`` → :attr:`CalibrationState.UNAVAILABLE` (zero data
      points indicates collector failure or vendor outage — operator action
      required, not "give it time").
    """
    if observed_n >= required_n:
        return CalibrationState.CALIBRATED
    if observed_n == 0:
        return CalibrationState.UNAVAILABLE
    return CalibrationState.ACCUMULATING


def tag_with_fallback(
    *,
    observed_n: int,
    required_n: int,
    input_name: str,
    computed_value: Any,
    fallback: Callable[[], Any],
) -> CalibratedValue:
    """Decide the calibration state and wrap the result.

    Four branches:

    - ``observed_n >= required_n`` — ``computed_value`` carried with
      :attr:`CalibrationState.CALIBRATED`; the fallback is not invoked.
    - ``observed_n == 0`` — ``value=None`` with
      :attr:`CalibrationState.UNAVAILABLE`. The fallback is not invoked
      because the operator-facing signal is "the series is missing,"
      not "we substituted a pooled prior."
    - ``0 < observed_n < required_n`` and ``fallback()`` returns a value —
      fallback value carried with :attr:`CalibrationState.ACCUMULATING`
      and a compact ``"<input_name>: <observed_n> < <required_n>"`` reason.
    - ``0 < observed_n < required_n`` and ``fallback()`` returns ``None`` —
      ``value=None`` with :attr:`CalibrationState.UNAVAILABLE` and a reason
      noting the cross-sectional pool was empty.
    """
    if observed_n >= required_n:
        return CalibratedValue(
            value=computed_value,
            state=CalibrationState.CALIBRATED,
            bootstrap_reason=None,
        )
    reason = f"{input_name}: {observed_n} < {required_n}"
    if observed_n == 0:
        return CalibratedValue(
            value=None,
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=f"{reason} (0 observations)",
        )
    fallback_value = fallback()
    if fallback_value is None:
        return CalibratedValue(
            value=None,
            state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=f"{reason} (cross-sectional pool empty)",
        )
    return CalibratedValue(
        value=fallback_value,
        state=CalibrationState.ACCUMULATING,
        bootstrap_reason=reason,
    )


_STATE_SEVERITY: dict[CalibrationState, int] = {
    CalibrationState.CALIBRATED: 0,
    CalibrationState.ACCUMULATING: 1,
    CalibrationState.UNAVAILABLE: 2,
}
"""Worst-wins severity rank for :class:`CalibrationState`.

The three-state ladder (CALIBRATED → ACCUMULATING → UNAVAILABLE) is fixed
by the ALP-540 framework; the explicit dict pins the ordering here so
:func:`combine_calibration_states` doesn't depend on the iteration order
of any other constant.
"""


def combine_calibration_states(
    *states: tuple[CalibrationState, str | None],
) -> tuple[CalibrationState, str | None]:
    """Worst-wins fold across multiple ``(state, reason)`` pairs.

    Severity order is ``UNAVAILABLE > ACCUMULATING > CALIBRATED``. The
    reason returned is a ``"; "``-joined string of every non-empty
    reason from the most-severe pairs — when two inputs are both
    UNAVAILABLE the operator sees both gaps named on the same block.

    Returns ``(CALIBRATED, None)`` when every input is calibrated.
    """
    worst_state = max(states, key=lambda pair: _STATE_SEVERITY[pair[0]])[0]
    if worst_state is CalibrationState.CALIBRATED:
        return CalibrationState.CALIBRATED, None
    reasons = [reason for state, reason in states if state is worst_state and reason]
    combined_reason = "; ".join(reasons) if reasons else None
    return worst_state, combined_reason
