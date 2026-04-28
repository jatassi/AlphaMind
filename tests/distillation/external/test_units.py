"""Tests for the unit-conversion constants — story 02-distillation/06.

Verify the two named constants in :mod:`alphamind.distillation.units` carry
their documented values; the constants exist precisely so callers in later
stories never inline ``100.0`` or ``0.01``.
"""

from __future__ import annotations

from alphamind.distillation.units import BPS_PER_PCT, PCT_TO_RATIO


def test_bps_per_pct_is_100() -> None:
    """One percent is 100 basis points by definition."""
    assert BPS_PER_PCT == 100.0


def test_pct_to_ratio_is_1_over_100() -> None:
    """Percent-to-ratio multiplier converts 1% to 0.01."""
    assert PCT_TO_RATIO == 0.01


def test_bps_per_pct_and_pct_to_ratio_are_inverses() -> None:
    """The two constants compose: one bps in ratio terms is ``1 / BPS_PER_PCT * PCT_TO_RATIO``.

    The relationship is the reason both constants live side-by-side; pinning
    it here documents the invariant a future reviewer can trust.
    """
    assert (1.0 / BPS_PER_PCT) * BPS_PER_PCT == 1.0
    assert PCT_TO_RATIO * BPS_PER_PCT == 1.0
