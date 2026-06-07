"""Shared metric-result builder (ALP-912 / 06g item B).

``rate_result`` is the one fraction-reading builder the decision, citation-chain, and
outcome metric families share. On an empty denominator it yields the design's
"no honest reading" shape — ``value=None``, ``sample_size=0``,
``insufficient_sample=True`` — consistently across all three families.
"""

from __future__ import annotations

from alphamind.feedback_loop.metrics.types import MetricId, PosteriorBand, rate_result


def test_posterior_band_contains_is_inclusive_of_bounds() -> None:
    band = PosteriorBand(lower=0.2, upper=0.8)
    assert band.contains(0.2) is True
    assert band.contains(0.8) is True
    assert band.contains(0.5) is True


def test_posterior_band_contains_excludes_values_outside_bounds() -> None:
    band = PosteriorBand(lower=0.2, upper=0.8)
    assert band.contains(0.19) is False
    assert band.contains(0.81) is False


def test_rate_result_populated_fraction() -> None:
    result = rate_result(MetricId("m"), 3, 4)
    assert result.metric_id == MetricId("m")
    assert result.value == 0.75
    assert result.posterior_band is None
    assert result.sample_size == 4
    assert result.insufficient_sample is False


def test_rate_result_empty_denominator_is_insufficient_no_value() -> None:
    result = rate_result(MetricId("m"), 0, 0)
    assert result.value is None
    assert result.posterior_band is None
    assert result.sample_size == 0
    assert result.insufficient_sample is True
