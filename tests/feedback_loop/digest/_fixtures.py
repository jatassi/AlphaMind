"""Shared builders for the weekly-digest tests (ALP-888).

Hand-built ``WindowDataset`` weeks + a temporary self-registering metric module so
the digest's registry-lookup path is exercised both when a metric is present and
when it is absent (graceful degradation). No DB — the generator and shifts are pure
over these inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from alphamind.feedback_loop.dataset import WindowDataset
from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    SupersededReason,
    ValidationId,
    ValidationRecord,
)
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMVerdict,
)

_SEQ = [0]


def week_bounds(monday: datetime) -> tuple[datetime, datetime]:
    return monday, monday + timedelta(days=7)


def pm_entry(verdict: PMVerdict, timestamp: datetime) -> ActivityLogEntry:
    """A minimal PM_DECISION activity-log entry carrying *verdict*."""
    _SEQ[0] += 1
    detail = PMDecisionDetail(
        envelope_id=f"ENV-{_SEQ[0]}",
        source_provenance_json={
            "source_provenance": "pm_analyst",
            "source_recommendation_id": f"REC-{_SEQ[0]}",
            "recommendation_type": "new_entry",
            "position_id": None,
        },
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=(),
        verdict=verdict,
        originating_proposal_json={},
    )
    return ActivityLogEntry(
        entry_id=f"ent-{_SEQ[0]}",
        invocation_id="inv-1",
        timestamp=timestamp,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )


def validation(
    *,
    validation_id: str,
    evaluation_due_at: datetime,
    watched_metric_ids: tuple[MetricId, ...] = (),
    superseded_at: datetime | None = None,
    superseded_reason: SupersededReason | None = None,
    edited_artifact: str = "prompts/analysis/analyst.md",
) -> ValidationRecord:
    return ValidationRecord(
        validation_id=ValidationId(validation_id),
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        registered_by_session_id=None,
        edited_artifact=edited_artifact,
        pre_edit_version="v1",
        post_edit_version="v2",
        registered_regime="neutral",
        registered_model_id="claude-opus-4-8",
        watched_metric_ids=watched_metric_ids,
        window_length_days=14,
        expected_direction=ExpectedDirection.IMPROVED,
        expected_magnitude="small",
        success_criterion="x",
        failure_criterion="y",
        evaluation_due_at=evaluation_due_at,
        superseded_at=superseded_at,
        superseded_reason=superseded_reason,
    )


def dataset(
    *,
    monday: datetime,
    pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    validations: tuple[ValidationRecord, ...] = (),
    superseded_validations: tuple[ValidationRecord, ...] = (),
) -> WindowDataset:
    start, end = week_bounds(monday)
    return WindowDataset(
        start=start,
        end=end,
        agent_calls=(),
        pm_decision_log=pm_decision_log,
        validations=validations,
        superseded_validations=superseded_validations,
    )


def week_sequence(count: int, *, base: datetime | None = None) -> list[tuple[str, WindowDataset]]:
    """*count* trailing weeks, oldest-first, each an empty dataset.

    Default base is a fixed Monday so the labels are deterministic across runs.
    """
    base_monday = base or datetime(2026, 3, 30, tzinfo=UTC)  # a Monday
    mondays = [base_monday - timedelta(weeks=offset) for offset in range(count - 1, -1, -1)]
    return [(m.date().isoformat(), dataset(monday=m)) for m in mondays]


def constant_metric(metric_id: str, value: float, sample_size: int = 10) -> Metric:
    """A registered-shaped :class:`Metric` whose compute returns a fixed value.

    Used to plant a temporary module in the registry so a digest section / detector
    that references *metric_id* gets a live reading.
    """

    def compute(_dataset: WindowDataset, _conditioning: Conditioning) -> MetricResult:
        return MetricResult(
            metric_id=MetricId(metric_id),
            value=value,
            posterior_band=None,
            sample_size=sample_size,
            insufficient_sample=False,
        )

    return Metric(
        metric_id=MetricId(metric_id),
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=compute,
    )


def per_week_metric(metric_id: str, values_by_label: dict[str, float]) -> Metric:
    """A :class:`Metric` returning a per-week value keyed by the dataset's week label.

    Since metric compute receives only the dataset (not the label), the value is keyed
    by the dataset's ``start`` ISO date — the label convention the fixtures use.
    """

    def compute(ds: WindowDataset, _conditioning: Conditioning) -> MetricResult:
        label = ds.start.date().isoformat()
        value = values_by_label.get(label)
        return MetricResult(
            metric_id=MetricId(metric_id),
            value=value,
            posterior_band=None,
            sample_size=0 if value is None else 10,
            insufficient_sample=value is None,
        )

    return Metric(
        metric_id=MetricId(metric_id),
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=compute,
    )


def install_metrics(monkeypatch: Any, metrics: tuple[Metric, ...]) -> None:
    """Augment the registry with *metrics* for the duration of a test.

    Builds the real registry, overlays *metrics* on top (so the planted ids resolve
    while every real id still does), and pins the result into the registry's memoised
    ``_registry`` cache via ``monkeypatch.setattr`` — pytest restores the original
    cache value on teardown, so nothing leaks into another test.
    """
    import alphamind.feedback_loop.metrics as registry

    combined = dict(registry._discover())  # test seam: real registry + overlay
    for metric in metrics:
        combined[metric.metric_id] = metric
    monkeypatch.setattr(registry, "_registry", combined)
