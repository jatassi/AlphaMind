"""Validators reused across config-model packages.

Lives outside any individual model module so multiple configs can share
the same parsing + diagnostic posture without cross-importing each other.
The first re-user (ALP-715 review F6) is the
``borrow_accrual_tick_local_time`` knob on ``ContinuousMonitorConfig``,
which mirrors the HH:MM 24-hour-clock format that
``SessionWindow.open``/``.close`` (in :mod:`alphamind.config.models.venue`)
already validate. Keeping one canonical regex + diagnostic prevents two
HH:MM formats from drifting and gives operators a single error message
to recognize.
"""

from __future__ import annotations

import re

# HH:MM in 24-hour clock; rejects 25:00, 9:30 (single-digit hour), 16:0
# (single-digit minute), 16:60 (out-of-range minute), and the like.
# Finer rules (open <= close, business-hours sanity, etc.) belong to
# runtime callers if they need them.
HH_MM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def validate_hh_mm(value: str) -> str:
    """Return *value* unchanged if it parses as HH:MM 24-hour-clock, else raise.

    Raises :class:`ValueError` with the regex pattern in the message so the
    Pydantic ``ValidationError`` envelope is operator-actionable.
    """
    if not HH_MM_RE.match(value):
        msg = f"Time {value!r} must match HH:MM in 24-hour clock ({HH_MM_RE.pattern})"
        raise ValueError(msg)
    return value
