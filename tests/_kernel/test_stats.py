"""Tests for ``alphamind._kernel.stats`` — shared statistical primitives.

``percentile_from_normal`` is the single Normal-CDF percentile formula
ALP-589 extracted out of two near-identical copies (the analysis-layer
qualitative loader and the distillation sentiment-percentile compute). The
invariants that matter:

1. The percentile is the standard-Normal CDF of the z-score, clamped to the
   unit interval ``[0.0, 1.0]``.
2. A non-positive ``stdev`` raises rather than returning a placeholder — the
   degenerate-distribution policy belongs to each caller, not the shared math.
3. ``__module__`` resolves to ``alphamind._kernel.stats`` — proving the
   function is defined here, not re-exported from a previous home.
"""

from __future__ import annotations

import math

import pytest

from alphamind._kernel.stats import percentile_from_normal


def test_value_at_mean_yields_median() -> None:
    assert percentile_from_normal(value=0.3, mean=0.3, stdev=0.1) == pytest.approx(0.5)


def test_two_stdev_above_mean_yields_upper_tail() -> None:
    # Phi(2) ≈ 0.97725.
    assert percentile_from_normal(value=0.5, mean=0.3, stdev=0.1) == pytest.approx(
        0.97725, abs=1e-5
    )


def test_two_stdev_below_mean_yields_lower_tail() -> None:
    # Phi(-2) ≈ 0.02275.
    assert percentile_from_normal(value=0.1, mean=0.3, stdev=0.1) == pytest.approx(
        0.02275, abs=1e-5
    )


def test_result_is_clamped_to_unit_interval() -> None:
    far_high = percentile_from_normal(value=100.0, mean=0.0, stdev=0.01)
    far_low = percentile_from_normal(value=-100.0, mean=0.0, stdev=0.01)
    assert 0.0 <= far_low <= far_high <= 1.0
    assert far_high == pytest.approx(1.0)
    assert far_low == pytest.approx(0.0)


def test_matches_erf_definition() -> None:
    # Spot-check the formula against an independent erf evaluation.
    z = (0.42 - 0.10) / 0.25
    expected = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    assert percentile_from_normal(value=0.42, mean=0.10, stdev=0.25) == pytest.approx(expected)


@pytest.mark.parametrize("bad_stdev", [0.0, -0.1, -5.0])
def test_non_positive_stdev_raises(bad_stdev: float) -> None:
    with pytest.raises(ValueError, match="stdev"):
        percentile_from_normal(value=0.5, mean=0.3, stdev=bad_stdev)


def test_module_is_kernel_stats() -> None:
    assert percentile_from_normal.__module__ == "alphamind._kernel.stats"
