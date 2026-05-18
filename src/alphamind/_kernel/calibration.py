"""Calibration state vocabulary shared across distillation and persistence.

A leaf module: no first-party imports. Splitting the vocabulary off from the
``distillation/calibration.py`` framework breaks the runtime cycle between
``persistence.models`` (which CHECK-constrains ``calibration_state`` columns
to these literals) and the distillation computations that import the enum to
emit them.

The string values are part of the persistence contract — every distillation
table's ``calibration_state`` column has a CHECK constraint accepting exactly
``calibrated``, ``accumulating``, ``unavailable``.

Three operator-meaningful states (ALP-540):

- ``calibrated`` — sufficient observations; signal is reliable.
- ``accumulating`` — collector healthy, observations strictly between zero
  and the required minimum; time alone resolves it.
- ``unavailable`` — zero observations (collector failure / vendor error /
  series missing) or no fallback computable; operator action required.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CALIBRATION_STATE_VALUES",
    "CalibrationState",
]


CALIBRATION_STATE_VALUES: tuple[str, str, str] = (
    "calibrated",
    "accumulating",
    "unavailable",
)
"""The schema CHECK-constraint accepts exactly these three strings.

Centralized here so ``alphamind.persistence.models`` imports the tuple instead
of restating the literals — this module is the single source of truth for the
state vocabulary.
"""


class CalibrationState(StrEnum):
    """Per-output calibration tag.

    Each member's value matches the ``calibration_state`` CHECK constraint
    on every distillation table.
    """

    CALIBRATED = "calibrated"
    ACCUMULATING = "accumulating"
    UNAVAILABLE = "unavailable"
