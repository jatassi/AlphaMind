"""Metric descriptor + result types for the analytics spine (ALP-882 story 05).

The metric model the feedback-loop design encodes (``docs/design/feedback-loop.md``
§ Process metrics vs. outcome metrics, § Conditioning surface): each metric has a
process/outcome (P/O) type, a meaningful window, and a set of conditioning slices
it supports (regime / sector / conviction / strategist status / anti-pattern /
time-of-day / prompt version / model version).

Functional core / imperative shell (P1): a ``Metric``'s ``compute`` is a **pure
function** over a :class:`~alphamind.feedback_loop.dataset.WindowDataset`; all DB
I/O lives in the loader shell (``dataset.load_window``). The ``feedback_loop.metrics``
package therefore never imports ``sqlalchemy`` or the session layer — enforced by the
``feedback-loop-metric-cores-no-sqlalchemy`` ``.importlinter`` contract.

``MetricId`` is an **append-only persistence contract** — stable snake_case IDs that
``validations.watched_metric_ids`` and the weekly digest reference by value. It is the
*same* ``NewType`` minted for the validation-discipline layer
(:mod:`alphamind.feedback_loop.validation.records`); re-exported here so there is one
contract, not two incompatible aliases.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

# Re-export the single MetricId contract minted alongside the validation records,
# so a metric id flows losslessly into validations.watched_metric_ids.
from alphamind.feedback_loop.validation.records import MetricId

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import WindowDataset

# ---------------------------------------------------------------------------
# Definitional constants (parent ALP-131 pre-resolved (J): windows + bands are
# in code, not config — only sample-size thresholds are operator-tunable).
# ---------------------------------------------------------------------------

#: Outcome-tier metrics are displayed as a Bayesian posterior band rather than a
#: point estimate (``docs/design/feedback-loop.md`` § Cadence, § Section 3 —
#: Trajectory). The band is the central 80% credible interval.
POSTERIOR_BAND_WIDTH = 0.80


PoType = Literal["process", "outcome"]
"""Process metrics (high-frequency, low-noise) vs. outcome metrics (low-frequency,
high-noise). The load-bearing distinction throughout the metric inventory."""


class Window(StrEnum):
    """Meaningful aggregation window for a metric (``feedback-loop.md`` § Cadence).

    The three observability tiers: daily (per-invocation structural signal),
    weekly (process metrics with small-but-meaningful samples), monthly /
    quarterly (outcome metrics that need statistical power).
    """

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class ConditioningDimension(StrEnum):
    """The slices an outcome metric can be conditioned on (§ Conditioning surface).

    Each is a query over existing invocation-provenance fields. ``REGIME`` is the
    single most important confounder.
    """

    REGIME = "regime"
    SECTOR = "sector"
    CONVICTION = "conviction"
    STRATEGIST_STATUS = "strategist_status"
    ANTI_PATTERN = "anti_pattern"
    TIME_OF_DAY = "time_of_day"
    PROMPT_VERSION = "prompt_version"
    MODEL_VERSION = "model_version"


@dataclass(frozen=True, slots=True)
class Conditioning:
    """A single conditioning slice — a dimension paired with the value held fixed.

    The unconditioned (whole-window) case is :data:`UNCONDITIONED` — ``dimension``
    and ``value`` both ``None``. A conditioned slice names exactly one dimension
    and the value to filter to (e.g. ``REGIME`` = ``"elevated"``).
    """

    dimension: ConditioningDimension | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        if (self.dimension is None) != (self.value is None):
            msg = "Conditioning dimension and value must be set together or both None"
            raise ValueError(msg)


#: The whole-window slice — no conditioning applied.
UNCONDITIONED = Conditioning()


@dataclass(frozen=True, slots=True)
class PosteriorBand:
    """An 80% Bayesian credible interval for an outcome-tier point estimate.

    ``lower``/``upper`` bound the central :data:`POSTERIOR_BAND_WIDTH` mass. A
    wide band on a small sample is the design's deliberate signal that no honest
    reading triggers a change (§ Cadence).
    """

    lower: float
    upper: float

    def __post_init__(self) -> None:
        if self.lower > self.upper:
            msg = f"PosteriorBand lower ({self.lower}) must not exceed upper ({self.upper})"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MetricResult:
    """The computed reading for one metric over one conditioning slice.

    * ``value`` — the point estimate (a scalar, or ``None`` when the sample is
      empty).
    * ``posterior_band`` — the 80% credible interval for outcome metrics; ``None``
      for process metrics and for empty samples.
    * ``sample_size`` — the number of underlying observations the reading rests on.
    * ``insufficient_sample`` — ``True`` when ``sample_size`` is below the
      operator-tunable threshold, so the reading carries a "needs N more
      observations" annotation rather than being read as actionable signal.
    """

    metric_id: MetricId
    value: float | None
    posterior_band: PosteriorBand | None
    sample_size: int
    insufficient_sample: bool


# The pure metric-core signature: WindowDataset + slice -> result. No I/O.
ComputeFn = Callable[["WindowDataset", Conditioning], MetricResult]


@dataclass(frozen=True, slots=True)
class Metric:
    """An immutable metric descriptor — identity, classification, and pure core.

    ``compute`` is a pure function of the (already-loaded) ``WindowDataset`` and a
    ``Conditioning`` slice; it performs no I/O. The registry
    (:mod:`alphamind.feedback_loop.metrics`) discovers ``Metric`` instances by id.
    """

    metric_id: MetricId
    po_type: PoType
    default_window: Window
    supported_conditioning: tuple[ConditioningDimension, ...]
    compute: ComputeFn = field(compare=False)


__all__ = [
    "POSTERIOR_BAND_WIDTH",
    "UNCONDITIONED",
    "ComputeFn",
    "Conditioning",
    "ConditioningDimension",
    "Metric",
    "MetricId",
    "MetricResult",
    "PoType",
    "PosteriorBand",
    "Window",
]
