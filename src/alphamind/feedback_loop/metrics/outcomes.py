"""Outcome + calibration metric cores (ALP-885 / story 06c).

The outcome-tier metrics calibrate the system's predictors (analyst conviction,
strategist status) against the *realized* outcomes recorded on resolved theses,
and summarise the P/L outcome surface. Each is a **pure** function over a
:class:`~alphamind.feedback_loop.dataset.WindowDataset` and a
:class:`~alphamind.feedback_loop.metrics.types.Conditioning` slice — no I/O, no
session (enforced by the ``feedback-loop-metric-cores-no-sqlalchemy`` contract).

The conditioning surface is realised by the slice argument: the *same* outcome
core, called once per conditioning value, yields the per-regime / per-conviction /
per-status reading. Conviction calibration is the win-rate core sliced on
conviction; status calibration is the adverse-rate core sliced on strategist
status.

Outcome metrics carry an 80% Bayesian posterior band
(:data:`~alphamind.feedback_loop.metrics.types.POSTERIOR_BAND_WIDTH`) where a band
is statistically meaningful — rates (Beta posterior) and the mean P/L (normal
approximation). Ratio summaries (profit factor, P/L per token) and drawdown are
point estimates with no band. Every reading carries its sample size and the
insufficient-sample flag, set when the resolved-thesis count is below the
operator-tunable ``FeedbackLoopConfig`` threshold for the metric's tier (carried
on the dataset's ``outcomes`` bundle).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING

from scipy.stats import beta, norm  # type: ignore[import-untyped]

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
from alphamind.portfolio_state.records.theses import ThesisResolutionCategory

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import ThesisOutcome, WindowDataset
    from alphamind.feedback_loop.metrics.types import Conditioning

# ---------------------------------------------------------------------------
# Stable metric ids — append-only persistence contract (referenced by the
# weekly digest and validations.watched_metric_ids by value).
# ---------------------------------------------------------------------------

METRIC_WIN_RATE = MetricId("outcome_win_rate")
METRIC_PROFIT_FACTOR = MetricId("outcome_profit_factor")
METRIC_DRAWDOWN = MetricId("outcome_drawdown")
METRIC_RESOLUTION_DISTRIBUTION = MetricId("outcome_resolution_distribution_validated")
METRIC_PL_PER_RESOLVED_THESIS = MetricId("outcome_pl_per_resolved_thesis")
METRIC_PL_PER_TOKEN = MetricId("outcome_pl_per_token")
METRIC_CONVICTION_CALIBRATION = MetricId("outcome_conviction_calibration_win_rate")
METRIC_STATUS_CALIBRATION = MetricId("outcome_status_calibration_adverse_rate")
METRIC_WEEKLY_PL = MetricId("outcome_weekly_pl")

# The conditioning dimensions every outcome metric supports (§ Conditioning surface).
_ALL_CONDITIONING: tuple[ConditioningDimension, ...] = (
    ConditioningDimension.REGIME,
    ConditioningDimension.SECTOR,
    ConditioningDimension.CONVICTION,
    ConditioningDimension.STRATEGIST_STATUS,
    ConditioningDimension.ANTI_PATTERN,
    ConditioningDimension.TIME_OF_DAY,
    ConditioningDimension.PROMPT_VERSION,
    ConditioningDimension.MODEL_VERSION,
)

# Each outcome-tier metric has a meaningful window; the window selects which
# ``FeedbackLoopConfig`` sample-size floor gates its insufficient-sample flag.
# Calibration is quarterly (needs statistical power); the P/L outcome surface is
# monthly (§ Cadence, § Cross-cutting metrics).


# ---------------------------------------------------------------------------
# Conditioning filter
# ---------------------------------------------------------------------------


def _matches(outcome: ThesisOutcome, conditioning: Conditioning) -> bool:
    """Whether *outcome* belongs in the slice named by *conditioning*.

    The unconditioned slice admits every outcome. A conditioned slice admits an
    outcome whose attribute for the named dimension equals the held value;
    ``ANTI_PATTERN`` matches on membership (a position may carry several tags).
    An outcome whose attribute is unknown (``None`` / empty) never matches a
    conditioned slice — an honest exclusion until the 04e provenance join lands.
    """
    if conditioning.dimension is None:
        return True
    attrs = outcome.conditioning
    if conditioning.dimension is ConditioningDimension.ANTI_PATTERN:
        return conditioning.value in attrs.anti_patterns
    selected = {
        ConditioningDimension.REGIME: attrs.regime,
        ConditioningDimension.SECTOR: attrs.sector,
        ConditioningDimension.CONVICTION: attrs.conviction,
        ConditioningDimension.STRATEGIST_STATUS: attrs.strategist_status,
        ConditioningDimension.TIME_OF_DAY: attrs.time_of_day,
        ConditioningDimension.PROMPT_VERSION: attrs.prompt_version,
        ConditioningDimension.MODEL_VERSION: attrs.model_version,
    }[conditioning.dimension]
    return selected is not None and selected == conditioning.value


def _slice(dataset: WindowDataset, conditioning: Conditioning) -> tuple[ThesisOutcome, ...]:
    return tuple(o for o in dataset.outcomes.theses if _matches(o, conditioning))


# ---------------------------------------------------------------------------
# Posterior-band helpers
# ---------------------------------------------------------------------------

_TAIL_MASS = (1.0 - POSTERIOR_BAND_WIDTH) / 2.0


def _beta_band(successes: int, total: int) -> PosteriorBand:
    """Central 80% credible interval of a Beta(k+1, n-k+1) rate posterior.

    A uniform Beta(1, 1) prior updated by *successes* of *total* observations.
    The interval narrows as *total* grows — the design's deliberate signal that a
    small sample carries a wide band that no honest reading acts on.
    """
    posterior = beta(successes + 1, total - successes + 1)
    return PosteriorBand(
        lower=float(posterior.ppf(_TAIL_MASS)),
        upper=float(posterior.ppf(1.0 - _TAIL_MASS)),
    )


def _mean_band(values: tuple[float, ...]) -> PosteriorBand:
    """Central 80% interval for a mean via the normal approximation.

    With a single observation (or zero spread) the interval collapses to the
    point estimate. Uses the sample standard error of the mean.
    """
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return PosteriorBand(lower=mean, upper=mean)
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    std_error = (variance / n) ** 0.5
    half_width = float(norm.ppf(1.0 - _TAIL_MASS)) * std_error
    return PosteriorBand(lower=mean - half_width, upper=mean + half_width)


def _insufficient(sample_size: int, dataset: WindowDataset, window: Window) -> bool:
    """Whether *sample_size* is below the tier's resolved-thesis threshold."""
    bundle = dataset.outcomes
    threshold = (
        bundle.min_resolved_theses_quarterly
        if window is Window.QUARTERLY
        else bundle.min_resolved_theses_monthly
    )
    return sample_size < threshold


# ---------------------------------------------------------------------------
# Rate cores (win rate + the two calibration metrics share this shape)
# ---------------------------------------------------------------------------


def _is_win(outcome: ThesisOutcome) -> bool:
    return outcome.resolution_pnl_usd > 0.0


def _is_adverse(outcome: ThesisOutcome) -> bool:
    return outcome.resolution_pnl_usd < 0.0


def _rate_metric(
    metric_id: MetricId,
    window: Window,
    *,
    predicate: Callable[[ThesisOutcome], bool],
    dataset: WindowDataset,
    conditioning: Conditioning,
) -> MetricResult:
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return rate_result(metric_id, 0, 0)
    successes = sum(1 for o in outcomes if predicate(o))
    return MetricResult(
        metric_id=metric_id,
        value=successes / total,
        posterior_band=_beta_band(successes, total),
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, window),
    )


def _compute_win_rate(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    return _rate_metric(
        METRIC_WIN_RATE,
        Window.MONTHLY,
        predicate=_is_win,
        dataset=dataset,
        conditioning=conditioning,
    )


def _compute_conviction_calibration(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    return _rate_metric(
        METRIC_CONVICTION_CALIBRATION,
        Window.QUARTERLY,
        predicate=_is_win,
        dataset=dataset,
        conditioning=conditioning,
    )


def _compute_status_calibration(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    return _rate_metric(
        METRIC_STATUS_CALIBRATION,
        Window.QUARTERLY,
        predicate=_is_adverse,
        dataset=dataset,
        conditioning=conditioning,
    )


def _compute_resolution_distribution(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    """Fraction of resolved theses in the VALIDATED category.

    The single most consequential category of the four-way resolution
    distribution; the full distribution is the family of conditioned readings the
    digest renders together. A rate, so it carries a Beta band.
    """
    return _rate_metric(
        METRIC_RESOLUTION_DISTRIBUTION,
        Window.MONTHLY,
        predicate=lambda o: o.resolution_category is ThesisResolutionCategory.VALIDATED,
        dataset=dataset,
        conditioning=conditioning,
    )


# ---------------------------------------------------------------------------
# Outcome-surface cores
# ---------------------------------------------------------------------------


def _profit_factor_value(wins: float, losses: float) -> float | None:
    """Profit factor over a non-empty slice, with the undefined denominators distinct.

    ``+inf`` for an all-winning slice (``wins > 0``, no losses); ``None`` for the
    genuinely-undefined all-break-even ``0/0`` case; the finite ratio otherwise.
    """
    if losses == 0.0:
        return math.inf if wins > 0.0 else None
    return wins / losses


def _compute_profit_factor(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    """Sum of wins / sum of absolute losses over the resolved-thesis slice.

    Three undefined-denominator cases are kept distinct:

    * **all-wins** (``wins > 0``, no losses) — the mathematically-correct profit factor
      is ``+inf``; reported as such so an all-winning slice is not conflated with no data.
    * **all break-even** (a non-empty slice with neither wins nor losses) — the genuinely
      undefined ``0/0`` case stays ``value=None``.
    * **empty slice** (``total == 0``) — routed through the shared empty builder above.
    """
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return rate_result(METRIC_PROFIT_FACTOR, 0, 0)
    wins = sum(o.resolution_pnl_usd for o in outcomes if o.resolution_pnl_usd > 0.0)
    losses = -sum(o.resolution_pnl_usd for o in outcomes if o.resolution_pnl_usd < 0.0)
    value = _profit_factor_value(wins, losses)
    return MetricResult(
        metric_id=METRIC_PROFIT_FACTOR,
        value=value,
        posterior_band=None,
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, Window.MONTHLY),
    )


def _compute_drawdown(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    """Maximum peak-to-trough drop of the cumulative resolved-thesis P/L curve.

    Resolution order is the cumulation order: the read helper returns theses
    ordered by ``resolution_timestamp`` ascending, preserved through the loader.
    """
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return rate_result(METRIC_DRAWDOWN, 0, 0)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for outcome in outcomes:
        cumulative += outcome.resolution_pnl_usd
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return MetricResult(
        metric_id=METRIC_DRAWDOWN,
        value=max_drawdown,
        posterior_band=None,
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, Window.MONTHLY),
    )


def _compute_pl_per_resolved_thesis(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    """Average realized P/L per resolved thesis. A mean, so it carries a band."""
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return rate_result(METRIC_PL_PER_RESOLVED_THESIS, 0, 0)
    pnls = tuple(o.resolution_pnl_usd for o in outcomes)
    return MetricResult(
        metric_id=METRIC_PL_PER_RESOLVED_THESIS,
        value=sum(pnls) / total,
        posterior_band=_mean_band(pnls),
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, Window.MONTHLY),
    )


def _compute_pl_per_token(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    """Realized P/L per LLM token spent over the window — cost efficiency.

    The token denominator is the window's total agent-call token spend; it is
    *not* conditioned, because the thesis→invocation join needed to attribute
    tokens to a single resolved thesis is not yet wired (04e). The numerator is
    the conditioned resolved-thesis P/L. ``None`` when no tokens were spent.
    """
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return rate_result(METRIC_PL_PER_TOKEN, 0, 0)
    tokens = sum(call.input_tokens + call.output_tokens for call in dataset.agent_calls)
    pnl = sum(o.resolution_pnl_usd for o in outcomes)
    value = None if tokens == 0 else pnl / tokens
    return MetricResult(
        metric_id=METRIC_PL_PER_TOKEN,
        value=value,
        posterior_band=None,
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, Window.MONTHLY),
    )


def _compute_weekly_pl(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    """Total realized P/L across theses resolved in the weekly window.

    A point sum (not a rate or mean), so ``posterior_band`` is always ``None``.
    ``value=None`` on an empty slice — the "no honest reading" signal matching the
    weekly-digest graceful-degradation placeholder (digest Section 3 — Trajectory,
    ``_M_WEEKLY_PL``).
    """
    outcomes = _slice(dataset, conditioning)
    total = len(outcomes)
    if total == 0:
        return MetricResult(
            metric_id=METRIC_WEEKLY_PL,
            value=None,
            posterior_band=None,
            sample_size=0,
            insufficient_sample=True,
        )
    value = sum(o.resolution_pnl_usd for o in outcomes)
    return MetricResult(
        metric_id=METRIC_WEEKLY_PL,
        value=value,
        posterior_band=None,
        sample_size=total,
        insufficient_sample=_insufficient(total, dataset, Window.WEEKLY),
    )


# ---------------------------------------------------------------------------
# Registry tuple — discovered by feedback_loop.metrics._discover
# ---------------------------------------------------------------------------

METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=METRIC_WIN_RATE,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_win_rate,
    ),
    Metric(
        metric_id=METRIC_PROFIT_FACTOR,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_profit_factor,
    ),
    Metric(
        metric_id=METRIC_DRAWDOWN,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_drawdown,
    ),
    Metric(
        metric_id=METRIC_RESOLUTION_DISTRIBUTION,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_resolution_distribution,
    ),
    Metric(
        metric_id=METRIC_PL_PER_RESOLVED_THESIS,
        po_type="outcome",
        default_window=Window.MONTHLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_pl_per_resolved_thesis,
    ),
    Metric(
        metric_id=METRIC_PL_PER_TOKEN,
        po_type="outcome",
        default_window=Window.MONTHLY,
        # Conditioning intentionally disabled: the token denominator is the window
        # total (unconditioned) until the thesis→invocation token-attribution join
        # lands (04e / 06f), so a conditioned slice would divide a conditioned
        # numerator by an unconditioned denominator (an unsound ratio). Re-enable
        # with _ALL_CONDITIONING once per-thesis token attribution is wired.
        supported_conditioning=(),
        compute=_compute_pl_per_token,
    ),
    Metric(
        metric_id=METRIC_CONVICTION_CALIBRATION,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_conviction_calibration,
    ),
    Metric(
        metric_id=METRIC_STATUS_CALIBRATION,
        po_type="outcome",
        default_window=Window.QUARTERLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_status_calibration,
    ),
    Metric(
        metric_id=METRIC_WEEKLY_PL,
        po_type="outcome",
        default_window=Window.WEEKLY,
        supported_conditioning=_ALL_CONDITIONING,
        compute=_compute_weekly_pl,
    ),
)


__all__ = [
    "METRICS",
    "METRIC_CONVICTION_CALIBRATION",
    "METRIC_DRAWDOWN",
    "METRIC_PL_PER_RESOLVED_THESIS",
    "METRIC_PL_PER_TOKEN",
    "METRIC_PROFIT_FACTOR",
    "METRIC_RESOLUTION_DISTRIBUTION",
    "METRIC_STATUS_CALIBRATION",
    "METRIC_WEEKLY_PL",
    "METRIC_WIN_RATE",
]
