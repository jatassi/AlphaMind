"""Decision-layer process metrics (ALP-883 story 06a).

Pure ``Metric`` cores over the ``WindowDataset`` ``pm_decision_log`` bundle — the
high-frequency, low-noise *process* metrics the weekly digest's process pulse, the
validation skill's watched-metrics, and the retrospective read. Computed from the PM
decision envelopes persisted as :class:`PMDecisionDetail` activity-log entries, plus the
analyst / strategist proposal bodies carried in ``originating_proposal_json``.

Functional core / imperative shell (P1): every ``compute`` here is a pure function of an
already-loaded ``WindowDataset`` and a ``Conditioning`` slice — no I/O, no session, no
sqlalchemy import (enforced by the ``feedback-loop-metric-cores-no-sqlalchemy``
``.importlinter`` contract; ``WindowDataset`` is imported under ``TYPE_CHECKING`` only so
the loader's transitive DB imports never reach this module statically).

Each metric is a scalar rate/count addressable by a stable snake_case ``MetricId`` — a
distribution (verdict, conviction histogram, status, action, transition matrix) is
expressed as one ``MetricId`` per bin so a single bin flows losslessly into
``validations.watched_metric_ids`` and the digest's per-bin notable-shift detectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.portfolio_state.events.types import EventType, PMVerdict

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import WindowDataset
    from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail


# ---------------------------------------------------------------------------
# PM-decision extraction over the pm_decision_log bundle
# ---------------------------------------------------------------------------


def _pm_decisions(dataset: WindowDataset) -> tuple[PMDecisionDetail, ...]:
    """The ``PMDecisionDetail`` payloads in the window's ``pm_decision_log``.

    The bundle is a sliding window of activity-log entries; only ``PM_DECISION``
    entries carry a ``PMDecisionDetail`` (the bundle's loader already filters to that
    event type, but the guard keeps the core total over any entry shape).
    """
    return tuple(
        entry.detail
        for entry in dataset.pm_decision_log
        if entry.event_type == EventType.PM_DECISION
    )


# ---------------------------------------------------------------------------
# Rate metric
# ---------------------------------------------------------------------------


def _rate_result(metric_id: MetricId, numerator: int, denominator: int) -> MetricResult:
    """A process-tier fraction reading: ``numerator / denominator``.

    Empty denominator yields ``value=None`` (the design's "no honest reading" signal)
    rather than a divide-by-zero. ``insufficient_sample`` is left ``False`` here — the
    operator-tunable sample-size gate is applied by the digest, not the metric core.
    """
    value = None if denominator == 0 else numerator / denominator
    return MetricResult(
        metric_id=metric_id,
        value=value,
        posterior_band=None,
        sample_size=denominator,
        insufficient_sample=False,
    )


# ---------------------------------------------------------------------------
# PM verdict metrics
# ---------------------------------------------------------------------------

_PM_APPROVAL_RATE = MetricId("pm_approval_rate")


def _compute_pm_approval_rate(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    decisions = _pm_decisions(dataset)
    approved = sum(
        d.verdict in (PMVerdict.APPROVE, PMVerdict.APPROVE_WITH_MODIFICATION) for d in decisions
    )
    return _rate_result(_PM_APPROVAL_RATE, approved, len(decisions))


METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=_PM_APPROVAL_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_pm_approval_rate,
    ),
)
