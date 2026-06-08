"""PM-accuracy + modification-effectiveness metric cores (ALP-887 / story 06e).

The counterfactual-replay-sourced PM outcome metrics: when the PM rejected or
modified a proposal, the trade was never taken, so there is no forward outcome to
score against. The ALP-129 counterfactual replay engine closes the gap by
simulating the rejected / un-modified form against historical price data; these
cores read those replays (joined to the originating PM envelope by the loader into
:class:`~alphamind.feedback_loop.dataset.ReplayObservation`) to measure PM
evaluation quality.

Each is a **pure** function over a
:class:`~alphamind.feedback_loop.dataset.WindowDataset` and a
:class:`~alphamind.feedback_loop.metrics.types.Conditioning` slice — no I/O, no
session (functional core / imperative shell, P1; the loader does the DB join).

The aggregation rule the design fixes (``feedback-loop.md`` § Counterfactual replay,
§ Section 3 — Trajectory): only ``evaluated`` replays with ``medium`` / ``high``
confidence enter an aggregated metric value. Low-confidence and unevaluable replays
are still carried on the bundle (so the count of *attempted* replays is observable)
but excluded from the rate, per the engine's confidence-determination rule. The
loader pre-filters to a single ``replay_engine_version`` so a metric never mixes
versions (ALP-129 pre-resolved (B)).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from scipy.stats import beta  # type: ignore[import-untyped]

from alphamind.feedback_loop.metrics.types import (
    POSTERIOR_BAND_WIDTH,
    ConditioningDimension,
    Metric,
    MetricId,
    MetricResult,
    PosteriorBand,
    Window,
    rate_result,
)

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import ReplayObservation, WindowDataset
    from alphamind.feedback_loop.metrics.types import Conditioning

# ---------------------------------------------------------------------------
# Replay vocabulary the cores branch on, as plain ``StrEnum`` values.
#
# The enum classes live in ``state.tables.counterfactual_replays`` — an ORM module
# that imports sqlalchemy — so a pure metric core (``feedback-loop-metric-cores-no-
# sqlalchemy`` contract) cannot import them. ``ReplayKind`` / ``ReplayStatus`` /
# ``Confidence`` are ``StrEnum``s, so the enum-typed fields on ``ReplayObservation``
# compare equal to these mirrored value tokens; the loader shell carries the typed
# enums, the cores read their values.
# ---------------------------------------------------------------------------

_KIND_REJECTION = "rejection"
_KIND_MODIFICATION_ORIGINAL_FORM = "modification_original_form"
_STATUS_EVALUATED = "evaluated"
_CONFIDENCE_HIGH = "high"
_CONFIDENCE_MEDIUM = "medium"


# ---------------------------------------------------------------------------
# Stable metric ids — append-only persistence contract (referenced by the
# weekly digest and validations.watched_metric_ids by value).
# ---------------------------------------------------------------------------

METRIC_PM_REJECTION_ACCURACY = MetricId("pm_rejection_accuracy")
METRIC_MODIFICATION_EFFECTIVENESS = MetricId("pm_modification_effectiveness")
METRIC_SIZING_MODIFICATION_EFFECTIVENESS = MetricId("pm_sizing_modification_effectiveness")
METRIC_ANTI_PATTERN_DETECTOR_ACCURACY = MetricId("pm_anti_pattern_detector_accuracy")


# ---------------------------------------------------------------------------
# Aggregation eligibility (the confidence + evaluability rule)
# ---------------------------------------------------------------------------


def _eligible(observation: ReplayObservation) -> bool:
    """Whether *observation* enters an aggregated metric value.

    Only ``evaluated`` replays with ``medium`` / ``high`` confidence count;
    ``unevaluable`` and low-confidence replays are loaded but excluded
    (``feedback-loop.md`` § Counterfactual replay).
    """
    return observation.replay_status == _STATUS_EVALUATED and observation.confidence in (
        _CONFIDENCE_HIGH,
        _CONFIDENCE_MEDIUM,
    )


# ---------------------------------------------------------------------------
# Posterior band (the four metrics are rates → Beta posterior)
# ---------------------------------------------------------------------------

_TAIL_MASS = (1.0 - POSTERIOR_BAND_WIDTH) / 2.0


def _beta_band(successes: int, total: int) -> PosteriorBand:
    """Central 80% credible interval of a Beta(k+1, n-k+1) rate posterior."""
    posterior = beta(successes + 1, total - successes + 1)
    return PosteriorBand(
        lower=float(posterior.ppf(_TAIL_MASS)),
        upper=float(posterior.ppf(1.0 - _TAIL_MASS)),
    )


# ---------------------------------------------------------------------------
# Shared rate core
# ---------------------------------------------------------------------------


def _rate_over(
    metric_id: MetricId,
    observations: tuple[ReplayObservation, ...],
    success: Callable[[ReplayObservation], bool],
) -> MetricResult:
    """Build the Beta-banded rate ``count(success) / len`` over *observations*.

    An empty slice yields the shared "no honest reading" empty result.
    """
    total = len(observations)
    if total == 0:
        return rate_result(metric_id, 0, 0)
    successes = sum(1 for o in observations if success(o))
    return MetricResult(
        metric_id=metric_id,
        value=successes / total,
        posterior_band=_beta_band(successes, total),
        sample_size=total,
        insufficient_sample=False,
    )


# ---------------------------------------------------------------------------
# PM rejection accuracy
# ---------------------------------------------------------------------------


def _compute_pm_rejection_accuracy(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    """Fraction of PM rejections the counterfactual confirms were correct.

    Over evaluated, medium/high-confidence ``rejection`` replays: a rejection is
    *correct* when the un-rejected trade would not have been profitable
    (``counterfactual_pnl <= 0``). A high rate means PM rejects the right proposals.
    """
    observations = tuple(
        o for o in dataset.replays.replays if o.replay_kind == _KIND_REJECTION and _eligible(o)
    )
    return _rate_over(
        METRIC_PM_REJECTION_ACCURACY,
        observations,
        lambda o: o.counterfactual_pnl is not None and o.counterfactual_pnl <= 0.0,
    )


# ---------------------------------------------------------------------------
# Modification effectiveness (+ the sizing subset)
# ---------------------------------------------------------------------------


def _scorable_modification(observation: ReplayObservation) -> bool:
    """Whether a modification replay can be scored.

    A ``modification_original_form`` replay is scorable once the *actual*
    modified-form trade the PM approved has resolved (``actual_modified_pnl`` set);
    until then the comparison has no realized modified-form leg and the replay is
    loaded but excluded.
    """
    return (
        observation.replay_kind == _KIND_MODIFICATION_ORIGINAL_FORM
        and _eligible(observation)
        and observation.actual_modified_pnl is not None
    )


def _modification_helped(observation: ReplayObservation) -> bool:
    """Whether the modified-form trade outperformed the counterfactual original form."""
    counterfactual = observation.counterfactual_pnl
    counterfactual = 0.0 if counterfactual is None else counterfactual
    assert observation.actual_modified_pnl is not None  # guaranteed by _scorable_modification
    return observation.actual_modified_pnl > counterfactual


def _compute_modification_effectiveness(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    """Fraction of PM modifications whose modified form beat the original form.

    Over scorable ``modification_original_form`` replays, the modified-form trade's
    realized P/L vs the counterfactual original-form P/L the engine simulated. A
    high rate means PM modifications improve outcomes.
    """
    observations = tuple(o for o in dataset.replays.replays if _scorable_modification(o))
    return _rate_over(METRIC_MODIFICATION_EFFECTIVENESS, observations, _modification_helped)


def _compute_sizing_modification_effectiveness(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    """Modification effectiveness restricted to sizing-down (``risk_reduction``) modifications.

    When the PM sizes a proposal down, does the modified form outperform the
    as-proposed original form? The same comparison as
    :func:`_compute_modification_effectiveness`, filtered to the sizing subset.
    """
    observations = tuple(
        o for o in dataset.replays.replays if _scorable_modification(o) and o.is_sizing_modification
    )
    return _rate_over(METRIC_SIZING_MODIFICATION_EFFECTIVENESS, observations, _modification_helped)


# ---------------------------------------------------------------------------
# Anti-pattern detector accuracy
# ---------------------------------------------------------------------------


def _compute_anti_pattern_detector_accuracy(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    """Per anti-pattern: fraction of tagged rejections the counterfactual confirms.

    When the PM rejected a proposal citing anti-pattern X, the detector was *right*
    if the un-rejected trade would not have been profitable (``counterfactual_pnl
    <= 0``). The metric is meaningful only per pattern, so it is read through the
    ``ANTI_PATTERN`` conditioning dimension — the whole-window (``UNCONDITIONED``)
    slice has no pattern to score and returns the empty result.

    Note: the anti-pattern tags ride on the observation but are empty in production
    until the anti-pattern persistence join lands (the ``pm_decision`` activity-log
    detail does not yet carry ``anti_patterns_identified`` — see ALP-887 report /
    ALP-906). This core is exercised by fixtures and degrades to insufficient-sample
    until that join populates the tags.
    """
    if (
        conditioning.dimension is not ConditioningDimension.ANTI_PATTERN
        or conditioning.value is None
    ):
        return rate_result(METRIC_ANTI_PATTERN_DETECTOR_ACCURACY, 0, 0)
    pattern = conditioning.value
    observations = tuple(
        o
        for o in dataset.replays.replays
        if o.replay_kind == _KIND_REJECTION and _eligible(o) and pattern in o.anti_patterns
    )
    return _rate_over(
        METRIC_ANTI_PATTERN_DETECTOR_ACCURACY,
        observations,
        lambda o: o.counterfactual_pnl is not None and o.counterfactual_pnl <= 0.0,
    )


# ---------------------------------------------------------------------------
# Registry tuple — discovered by feedback_loop.metrics._discover
# ---------------------------------------------------------------------------

METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=METRIC_PM_REJECTION_ACCURACY,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        supported_conditioning=(),
        compute=_compute_pm_rejection_accuracy,
    ),
    Metric(
        metric_id=METRIC_MODIFICATION_EFFECTIVENESS,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        supported_conditioning=(),
        compute=_compute_modification_effectiveness,
    ),
    Metric(
        metric_id=METRIC_SIZING_MODIFICATION_EFFECTIVENESS,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        supported_conditioning=(),
        compute=_compute_sizing_modification_effectiveness,
    ),
    Metric(
        metric_id=METRIC_ANTI_PATTERN_DETECTOR_ACCURACY,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        # Read per pattern: the metric is meaningful only sliced by ANTI_PATTERN
        # (each tag scored against its own tagged rejections), so it advertises the
        # single dimension it conditions on rather than the full conditioning surface.
        supported_conditioning=(ConditioningDimension.ANTI_PATTERN,),
        compute=_compute_anti_pattern_detector_accuracy,
    ),
)


__all__ = [
    "METRICS",
    "METRIC_ANTI_PATTERN_DETECTOR_ACCURACY",
    "METRIC_MODIFICATION_EFFECTIVENESS",
    "METRIC_PM_REJECTION_ACCURACY",
    "METRIC_SIZING_MODIFICATION_EFFECTIVENESS",
]
