"""Unit-conversion constants for the distillation layer.

Centralizes the basis-points / percent / ratio conventions referenced by every
per-category computation so callers reach for a named constant rather than
inlining ``100.0`` or ``0.01``. Pure module: no I/O, no state, no side effects.

The unit conventions follow ``docs/design/02-distillation-layer/external.md``
§ 1 Normalization and formatting: "Dollar values, percentages, and ratios in
consistent notation."
"""

from __future__ import annotations

BPS_PER_PCT: float = 100.0
"""Basis points per one percent.

Used when converting a percentage (e.g. ``0.05`` for "5%") to basis points
(``500.0``) or vice versa. Named so the conversion is searchable and the
discipline visible at the call site.
"""


PCT_TO_RATIO: float = 0.01
"""Multiplier converting a percentage to its ratio form.

Used when a percentage value (e.g. ``3.2`` for "3.2%") needs to be multiplied
into a price or rate as a fractional ratio (``0.032``). Named so the magic
number ``0.01`` does not appear in callers.
"""
