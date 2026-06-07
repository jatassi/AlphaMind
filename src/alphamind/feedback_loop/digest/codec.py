"""JSON codec for the weekly digest (ALP-891 story 08a).

Serializes a frozen-dataclass :class:`~alphamind.feedback_loop.digest.generator.WeeklyDigest`
tree to JSON text and rehydrates it back to an *equal* ``WeeklyDigest``. The snapshot
writer (``snapshot.py``) stamps the serialized form, with :data:`DIGEST_SCHEMA_VERSION`,
into a ``weekly_digest_snapshots`` row; the read CLI (``cli.py``) shares the serializer
to emit a digest as JSON.

Two halves:

* :func:`serialize_digest` — a recursive walk (:func:`_to_jsonable`) over the frozen
  dataclass tree producing JSON-native values: dataclasses → dicts (slots-friendly via
  :func:`dataclasses.fields`), tuples / lists → lists, datetimes → ISO strings, the rest
  passed through. ``property`` attributes are never serialized — only declared fields.

* :func:`deserialize_digest` — a **hand-written, type-directed** rebuild. The serializer
  is lossy in two ways the structural frozen-dataclass ``__eq__`` cares about: every
  tuple field is emitted as a JSON list, and the typed leaves (``MetricId`` NewType,
  ``PosteriorBand``, ``MetricResult``) become plain dicts / strings. The deserializer
  rebuilds the exact tree — tuples *not* lists, rehydrated leaf types, and the
  ``result: None`` sentinel for absent cells — so ``deserialize_digest(serialize_digest(d))
  == d`` holds.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any, Final

from alphamind.feedback_loop.digest.generator import (
    HeadlineOutcomes,
    MetricCell,
    ProcessPulse,
    PulseMetric,
    ReplayQueue,
    Sparkline,
    Trajectory,
    TrajectoryPoint,
    ValidationStatusRow,
    WeeklyDigest,
)
from alphamind.feedback_loop.digest.shifts import ShiftFinding
from alphamind.feedback_loop.metrics.types import MetricId, MetricResult, PosteriorBand

#: Payload-shape version stamped on each snapshot row. Bump when the serialized
#: ``WeeklyDigest`` shape changes incompatibly so consumers can reject / migrate
#: stale payloads (parent ALP-131 pre-resolved (G) ``digest_schema_version``).
DIGEST_SCHEMA_VERSION: Final[int] = 1

__all__ = [
    "DIGEST_SCHEMA_VERSION",
    "deserialize_digest",
    "serialize_digest",
]


# ---------------------------------------------------------------------------
# Serializer engine (the cli.py ``_to_jsonable`` walk, lifted here)
# ---------------------------------------------------------------------------


def _to_jsonable(obj: Any) -> Any:
    """Recursively convert a frozen-dataclass digest tree into JSON-native values.

    Dataclasses become dicts (slots-friendly via :func:`dataclasses.fields`); tuples
    and lists become lists; datetimes become ISO strings; everything else is passed
    through. ``property`` attributes are not serialised — only declared fields.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def serialize_digest(digest: WeeklyDigest) -> str:
    """Serialize *digest* to a stable JSON string (the snapshot ``digest_json``)."""
    return json.dumps(_to_jsonable(digest), indent=2)


# ---------------------------------------------------------------------------
# Deserializer — hand-written and type directed
# ---------------------------------------------------------------------------


def _posterior_band(raw: dict[str, Any] | None) -> PosteriorBand | None:
    if raw is None:
        return None
    return PosteriorBand(lower=raw["lower"], upper=raw["upper"])


def _metric_result(raw: dict[str, Any] | None) -> MetricResult | None:
    """Rebuild a :class:`MetricResult`, or ``None`` for the absent-cell sentinel."""
    if raw is None:
        return None
    return MetricResult(
        metric_id=MetricId(raw["metric_id"]),
        value=raw["value"],
        posterior_band=_posterior_band(raw["posterior_band"]),
        sample_size=raw["sample_size"],
        insufficient_sample=raw["insufficient_sample"],
    )


def _metric_cell(raw: dict[str, Any]) -> MetricCell:
    return MetricCell(
        metric_id=MetricId(raw["metric_id"]),
        result=_metric_result(raw["result"]),
    )


def _opt_metric_cell(raw: dict[str, Any] | None) -> MetricCell | None:
    return None if raw is None else _metric_cell(raw)


def _headline(raw: dict[str, Any]) -> HeadlineOutcomes:
    return HeadlineOutcomes(
        pl_last_7d=_metric_cell(raw["pl_last_7d"]),
        win_rate_last_7d=_metric_cell(raw["win_rate_last_7d"]),
        current_drawdown=_metric_cell(raw["current_drawdown"]),
        trades_closed_last_7d=_metric_cell(raw["trades_closed_last_7d"]),
    )


def _pulse_metric(raw: dict[str, Any]) -> PulseMetric:
    return PulseMetric(
        current=_metric_cell(raw["current"]),
        prior=_metric_cell(raw["prior"]),
        delta=raw["delta"],
    )


def _pulse(raw: dict[str, Any]) -> ProcessPulse:
    return ProcessPulse(
        pm_rejection_rate=_pulse_metric(raw["pm_rejection_rate"]),
        pm_modification_rate=_pulse_metric(raw["pm_modification_rate"]),
        analyst_inaction_rate=_pulse_metric(raw["analyst_inaction_rate"]),
        strategist_hold_rate=_pulse_metric(raw["strategist_hold_rate"]),
        guardrail_rejection_count=_pulse_metric(raw["guardrail_rejection_count"]),
        anti_pattern_frequencies=tuple(
            _metric_cell(cell) for cell in raw["anti_pattern_frequencies"]
        ),
    )


def _sparkline(raw: dict[str, Any]) -> Sparkline:
    return Sparkline(
        metric_id=MetricId(raw["metric_id"]),
        points=tuple(
            TrajectoryPoint(week=point["week"], cell=_metric_cell(point["cell"]))
            for point in raw["points"]
        ),
    )


def _trajectory(raw: dict[str, Any]) -> Trajectory:
    return Trajectory(
        weekly_pl=_sparkline(raw["weekly_pl"]),
        win_rate=_sparkline(raw["win_rate"]),
        cost_per_resolved_thesis=_sparkline(raw["cost_per_resolved_thesis"]),
        conviction_calibration_spread=_sparkline(raw["conviction_calibration_spread"]),
        status_calibration_spread=_sparkline(raw["status_calibration_spread"]),
        pm_rejection_accuracy=_sparkline(raw["pm_rejection_accuracy"]),
    )


def _validation_status(raw: list[dict[str, Any]]) -> tuple[ValidationStatusRow, ...]:
    return tuple(
        ValidationStatusRow(
            validation_id=row["validation_id"],
            edited_artifact=row["edited_artifact"],
            expected_direction=row["expected_direction"],
            watched_metric_reading=_opt_metric_cell(row["watched_metric_reading"]),
            evaluation_due_at=row["evaluation_due_at"],
        )
        for row in raw
    )


def _notable_shifts(raw: list[dict[str, Any]]) -> tuple[ShiftFinding, ...]:
    return tuple(
        ShiftFinding(kind=row["kind"], subject=row["subject"], detail=row["detail"]) for row in raw
    )


def _replay_queue(raw: dict[str, Any]) -> ReplayQueue:
    return ReplayQueue(
        total_attempted=raw["total_attempted"],
        by_status=dict(raw["by_status"]),
        by_unevaluable_reason=dict(raw["by_unevaluable_reason"]),
        by_confidence=dict(raw["by_confidence"]),
    )


def deserialize_digest(digest_json: str) -> WeeklyDigest:
    """Rehydrate the JSON produced by :func:`serialize_digest` into a ``WeeklyDigest``.

    Type-directed so the rebuilt tree is *equal* to the original: tuples (not lists),
    rehydrated ``MetricId`` / ``PosteriorBand`` / ``MetricResult`` leaves, and the
    ``result: None`` absent-cell sentinel.
    """
    raw: dict[str, Any] = json.loads(digest_json)
    return WeeklyDigest(
        week=raw["week"],
        headline=_headline(raw["headline"]),
        pulse=_pulse(raw["pulse"]),
        trajectory=_trajectory(raw["trajectory"]),
        validation_status=_validation_status(raw["validation_status"]),
        notable_shifts=_notable_shifts(raw["notable_shifts"]),
        replay_queue=_replay_queue(raw["replay_queue"]),
    )
