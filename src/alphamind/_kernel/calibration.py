"""Calibration state vocabulary shared across distillation and persistence.

A leaf module: no first-party imports. Splitting the vocabulary off from the
``distillation/calibration.py`` framework breaks the runtime cycle between
``persistence.models`` (which CHECK-constrains ``calibration_state`` columns
to these literals) and the distillation computations that import the enum to
emit them.

The string values are part of the persistence contract — every distillation
table's ``calibration_state`` column has a CHECK constraint accepting exactly
``calibrated``, ``bootstrap``, ``unavailable``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CALIBRATION_STATE_VALUES",
    "CalibrationState",
]


CALIBRATION_STATE_VALUES: tuple[str, str, str] = (
    "calibrated",
    "bootstrap",
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
    BOOTSTRAP = "bootstrap"
    UNAVAILABLE = "unavailable"
