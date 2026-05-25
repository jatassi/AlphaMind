"""Emergency-invocation review report (story 15 / ALP-99).

Reads ``invocations`` filtered to ``trigger_type='emergency'`` over a
window and joins each row to its ``activity_log`` entries to classify
the downstream consequence (drove remediating action / produced no-op /
operator-overridden) and surface the per-trigger-reason false-positive
rate. The report is operator tooling for structured trigger #3 from
``threshold-calibration.md`` § Update process — emergency invocations
firing without warrant.

Exit code is 0 regardless of REVIEW verdicts; non-zero exit is reserved
for script-internal errors.

Usage:
  uv run python scripts/report_emergency_invocations.py \\
      [--db-path PATH] [--window-days N] [--output text|json]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import DistillationRegimeState
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow

DEFAULT_WINDOW_DAYS = 90
"""Default window per story 15 — one quarter, the operator-driven cadence."""

OPERATOR_OVERRIDE_WINDOW = timedelta(minutes=5)
"""Operator-console events further than this from ``start_at`` are not the
operator's response to the emergency itself."""

REVIEW_FALSE_POSITIVE_RATE = 0.30
"""False-positive rate above this triggers a REVIEW verdict for the
trigger reason. Story-15 calls out 30% as 'more than 1 in 3 emergencies
was unnecessary' — at that point operator review is justified. Soft
target, not a pipeline gate."""

REVIEW_MIN_TOTAL = 4
"""Minimum emergency count before the REVIEW verdict can fire — small-N
suppression keeps a 1-of-1 no-op from triggering misleading REVIEW
signals."""

# "Defensive" event types — their presence in the per-invocation changelog
# indicates the emergency drove a remediating portfolio mutation. Position
# closes, position reductions, order cancellations, and bracket modifications
# all reduce exposure or pause flow; opens / adds do not count.
_DEFENSIVE_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.POSITION_CLOSED,
        EventType.POSITION_REDUCED,
        EventType.ORDER_CANCELLED,
        EventType.BRACKET_DISSOLVED,
        EventType.BRACKET_MODIFIED,
    }
)

_REMEDIATING_PM_VERDICTS: frozenset[PMVerdict] = frozenset(
    {
        PMVerdict.APPROVE,
        PMVerdict.APPROVE_WITH_MODIFICATION,
        PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION,
    }
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class Classification(StrEnum):
    """Mutually-exclusive downstream-consequence buckets per story 15."""

    DROVE_REMEDIATING_ACTION = "drove_remediating_action"
    PRODUCED_NO_OP = "produced_no_op"
    OPERATOR_OVERRIDDEN = "operator_overridden"


class SaturationLevel(StrEnum):
    """Per-trigger-reason verdict the operator interprets."""

    HEALTHY = "HEALTHY"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class TriggerReasonBucket:
    """Aggregate stats for one ``trigger_reason`` value."""

    trigger_reason: str
    total: int
    drove_remediating_action: int
    produced_no_op: int
    operator_overridden: int
    false_positive_rate: float
    saturation: SaturationLevel


@dataclass(frozen=True)
class PerInvocationDetail:
    """One row in the per-invocation block."""

    invocation_id: str
    start_at: datetime
    trigger_reason: str
    classification: Classification
    pm_decision_count: int
    action_command_count: int
    regime_narrative: str


@dataclass(frozen=True)
class Recommendation:
    """One recommendation surfaced by a REVIEW verdict."""

    trigger_reason: str
    false_positive_rate: float
    message: str


@dataclass(frozen=True)
class EmergencyInvocationReport:
    """Top-level report shape consumed by both renderers."""

    window_start: datetime
    window_end: datetime
    window_days: int
    scheduled_count: int
    emergency_count: int
    by_trigger_reason: tuple[TriggerReasonBucket, ...]
    per_invocation: tuple[PerInvocationDetail, ...]
    recommendations: tuple[Recommendation, ...]


# ---------------------------------------------------------------------------
# Compute (RED — minimal stub)
# ---------------------------------------------------------------------------


def _parse_iso(text: str) -> datetime:
    """Parse the ``Z``-suffixed ISO 8601 string the writers emit."""
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class _InvocationFacts:
    """Subset of ``invocations`` columns the report consumes."""

    invocation_id: str
    start_at: datetime
    trigger_type: str
    trigger_reason: str


def _load_invocations_in_window(
    session: Session, *, now: datetime, window_days: int
) -> tuple[_InvocationFacts, ...]:
    """Return all in-window invocations ordered by ``start_at`` ascending."""
    cutoff = now - timedelta(days=window_days)
    stmt = select(
        InvocationRow.invocation_id,
        InvocationRow.start_at,
        InvocationRow.trigger_type,
        InvocationRow.trigger_reason,
    ).order_by(InvocationRow.start_at.asc())
    facts: list[_InvocationFacts] = []
    for invocation_id, start_at, trigger_type, trigger_reason in session.execute(stmt).all():
        parsed = _parse_iso(start_at)
        if parsed < cutoff or parsed > now:
            continue
        facts.append(
            _InvocationFacts(
                invocation_id=invocation_id,
                start_at=parsed,
                trigger_type=trigger_type,
                trigger_reason=trigger_reason,
            )
        )
    return tuple(facts)


def _load_entries_by_invocation(
    session: Session, invocation_ids: Sequence[str]
) -> dict[str, tuple[ActivityLogEntry, ...]]:
    """Bulk-fetch activity-log entries for the given invocation IDs.

    Issues a single indexed query against ``ix_activity_log_invocation_id``
    rather than one query per invocation, so wide windows stay cheap.
    """
    if not invocation_ids:
        return {}
    stmt = (
        select(ActivityLogRow)
        .where(ActivityLogRow.invocation_id.in_(invocation_ids))
        .order_by(ActivityLogRow.entry_at.asc(), ActivityLogRow.entry_id.asc())
    )
    grouped: dict[str, list[ActivityLogEntry]] = {inv: [] for inv in invocation_ids}
    for row in session.execute(stmt).scalars():
        grouped[row.invocation_id].append(activity_log_entry_from_row(row))
    return {inv: tuple(entries) for inv, entries in grouped.items()}


def _classify_invocation(
    *,
    invocation_start_at: datetime,
    entries: Sequence[ActivityLogEntry],
) -> tuple[Classification, int, int]:
    """Apply story-15 precedence and return ``(classification, pm_count, action_count)``.

    Precedence: ``OPERATOR_OVERRIDDEN`` > ``DROVE_REMEDIATING_ACTION`` >
    ``PRODUCED_NO_OP``. ``pm_count`` is the number of ``PM_DECISION``
    entries; ``action_count`` is the number of approved decisions whose
    resulting commands materialized as defensive activity-log events in
    the same invocation.
    """
    pm_decisions = [e for e in entries if e.event_type == EventType.PM_DECISION]
    pm_count = len(pm_decisions)

    operator_event_present = any(
        e.source == EventSource.OPERATOR_CONSOLE
        and e.timestamp - invocation_start_at <= OPERATOR_OVERRIDE_WINDOW
        and e.timestamp >= invocation_start_at
        for e in entries
    )
    defensive_event_present = any(e.event_type in _DEFENSIVE_EVENT_TYPES for e in entries)
    remediating_decisions = [
        e
        for e in pm_decisions
        if isinstance(e.detail, PMDecisionDetail)
        and e.detail.verdict in _REMEDIATING_PM_VERDICTS
        and e.detail.resulting_command_ids
    ]
    action_count = len(remediating_decisions) if defensive_event_present else 0

    if operator_event_present:
        return Classification.OPERATOR_OVERRIDDEN, pm_count, action_count
    if remediating_decisions and defensive_event_present:
        return Classification.DROVE_REMEDIATING_ACTION, pm_count, action_count
    return Classification.PRODUCED_NO_OP, pm_count, action_count


def compute_emergency_invocation_report(
    *,
    session: Session,
    now: datetime,
    window_days: int,
) -> EmergencyInvocationReport:
    """Build the report from ``invocations`` and ``activity_log`` rows."""
    window_start = now - timedelta(days=window_days)
    invocations = _load_invocations_in_window(session, now=now, window_days=window_days)
    scheduled_count = sum(1 for inv in invocations if inv.trigger_type == "scheduled")
    emergencies = tuple(inv for inv in invocations if inv.trigger_type == "emergency")

    entries_by_inv = _load_entries_by_invocation(
        session, [inv.invocation_id for inv in emergencies]
    )
    regime_by_invocation = _resolve_regime_narratives(session, emergencies)

    per_invocation_list: list[PerInvocationDetail] = []
    for inv in emergencies:
        entries = entries_by_inv.get(inv.invocation_id, ())
        classification, pm_count, action_count = _classify_invocation(
            invocation_start_at=inv.start_at, entries=entries
        )
        per_invocation_list.append(
            PerInvocationDetail(
                invocation_id=inv.invocation_id,
                start_at=inv.start_at,
                trigger_reason=inv.trigger_reason,
                classification=classification,
                pm_decision_count=pm_count,
                action_command_count=action_count,
                regime_narrative=regime_by_invocation.get(
                    inv.invocation_id, "regime narrative unavailable"
                ),
            )
        )
    per_invocation = tuple(per_invocation_list)

    by_reason: dict[str, list[PerInvocationDetail]] = {}
    for detail in per_invocation:
        by_reason.setdefault(detail.trigger_reason, []).append(detail)

    by_trigger_reason = tuple(
        _bucket_for(reason, details) for reason, details in sorted(by_reason.items())
    )
    recommendations = tuple(
        _recommendation_for(bucket)
        for bucket in by_trigger_reason
        if bucket.saturation == SaturationLevel.REVIEW
    )

    return EmergencyInvocationReport(
        window_start=window_start,
        window_end=now,
        window_days=window_days,
        scheduled_count=scheduled_count,
        emergency_count=len(emergencies),
        by_trigger_reason=by_trigger_reason,
        per_invocation=per_invocation,
        recommendations=recommendations,
    )


_REGIME_SKIP_REASON = "regime_skip_emergency"


def _resolve_regime_narratives(
    session: Session, emergencies: Sequence[_InvocationFacts]
) -> dict[str, str]:
    """Build the per-invocation regime-narrative string for ``regime_skip``.

    Issues one query for the matching ``distillation_regime_state`` row
    per regime-skip invocation, matched by exact ``as_of == start_at``
    (the writer stamps both with the same ISO timestamp).
    """
    regime_skip_invocations = [
        inv for inv in emergencies if inv.trigger_reason == _REGIME_SKIP_REASON
    ]
    if not regime_skip_invocations:
        return {}
    iso_to_invocation: dict[str, str] = {
        _iso(inv.start_at): inv.invocation_id for inv in regime_skip_invocations
    }
    stmt = select(
        DistillationRegimeState.as_of,
        DistillationRegimeState.regime_label,
        DistillationRegimeState.prior_label,
    ).where(DistillationRegimeState.as_of.in_(iso_to_invocation.keys()))
    narratives: dict[str, str] = {}
    for as_of, regime_label, prior_label in session.execute(stmt).all():
        invocation_id = iso_to_invocation.get(as_of)
        if invocation_id is None:
            continue
        prior = prior_label or "(unknown)"
        narratives[invocation_id] = f"{prior} -> {regime_label}"
    return narratives


def _iso(dt: datetime) -> str:
    """Render a tz-aware UTC datetime as ``Z``-suffixed ISO 8601."""
    return dt.isoformat().replace("+00:00", "Z")


def _recommendation_for(bucket: TriggerReasonBucket) -> Recommendation:
    """Build the templated recommendation for one REVIEW-verdict bucket."""
    pct = bucket.false_positive_rate * 100
    base = (
        f"{bucket.trigger_reason} false-positive rate of {pct:.0f}% "
        f"exceeds the {REVIEW_FALSE_POSITIVE_RATE * 100:.0f}% review threshold."
    )
    if bucket.trigger_reason == _REGIME_SKIP_REASON:
        body = (
            f"{base} Consider disabling regime_skip_emergency_trigger "
            "(currently true) or refining the regime-skip definition. "
            "Cross-reference with verify_regime_transition.py for "
            "transition-state-machine integrity in the same window."
        )
    else:
        body = (
            f"{base} Consider re-tuning the trigger conditions in "
            "breach-behavior.md § Emergency invocation trigger."
        )
    return Recommendation(
        trigger_reason=bucket.trigger_reason,
        false_positive_rate=bucket.false_positive_rate,
        message=body,
    )


def _bucket_for(trigger_reason: str, details: Sequence[PerInvocationDetail]) -> TriggerReasonBucket:
    """Aggregate one trigger-reason cluster into a ``TriggerReasonBucket``."""
    drove = sum(1 for d in details if d.classification == Classification.DROVE_REMEDIATING_ACTION)
    no_op = sum(1 for d in details if d.classification == Classification.PRODUCED_NO_OP)
    overridden = sum(1 for d in details if d.classification == Classification.OPERATOR_OVERRIDDEN)
    total = len(details)
    fp_rate = (no_op + overridden) / total if total else 0.0
    saturation = (
        SaturationLevel.REVIEW
        if total >= REVIEW_MIN_TOTAL and fp_rate > REVIEW_FALSE_POSITIVE_RATE
        else SaturationLevel.HEALTHY
    )
    return TriggerReasonBucket(
        trigger_reason=trigger_reason,
        total=total,
        drove_remediating_action=drove,
        produced_no_op=no_op,
        operator_overridden=overridden,
        false_positive_rate=fp_rate,
        saturation=saturation,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


_CLASSIFICATION_LABEL = {
    Classification.DROVE_REMEDIATING_ACTION: "drove remediating action",
    Classification.PRODUCED_NO_OP: "produced no-op",
    Classification.OPERATOR_OVERRIDDEN: "operator-overridden / cancelled",
}


def format_report_text(report: EmergencyInvocationReport) -> str:
    """Render the report as plain text following the story-15 layout."""
    lines: list[str] = []
    lines.append("Emergency invocation review")
    lines.append(
        f"  Window: {report.window_start.date()} to "
        f"{report.window_end.date()} ({report.window_days} days)"
    )
    if report.emergency_count == 0:
        lines.append("  no emergency invocations in window")
        return "\n".join(lines) + "\n"

    total = report.scheduled_count + report.emergency_count
    pct = (report.emergency_count / total * 100) if total else 0.0
    lines.append(f"  Scheduled invocations:    {report.scheduled_count}")
    lines.append(f"  Emergency invocations:    {report.emergency_count}")
    lines.append(f"  Emergency rate:           {pct:.1f}% of total")

    lines.append("")
    lines.append("=== BY TRIGGER REASON ===")
    for bucket in report.by_trigger_reason:
        lines.append(f"  {bucket.trigger_reason}:                {bucket.total}")
        lines.append(f"    drove remediating action:           {bucket.drove_remediating_action}")
        lines.append(f"    produced no-op (no commands):       {bucket.produced_no_op}")
        lines.append(f"    operator-overridden / cancelled:    {bucket.operator_overridden}")
        lines.append(
            f"    false-positive rate:                "
            f"{bucket.false_positive_rate * 100:.0f}% ({bucket.saturation.value})"
        )
        lines.append("")

    lines.append("=== PER-INVOCATION DETAIL ===")
    for detail in report.per_invocation:
        lines.append(
            f"  {detail.start_at.strftime('%Y-%m-%d %H:%M UTC')} | "
            f"{detail.trigger_reason} | {detail.regime_narrative}"
        )
        lines.append(
            f"    pm_decisions:    {detail.pm_decision_count} "
            f"({detail.action_command_count} actions)"
        )
        lines.append(f"    classification:  {_CLASSIFICATION_LABEL[detail.classification]}")
        lines.append("")

    if report.recommendations:
        lines.append("=== RECOMMENDATION ===")
        for rec in report.recommendations:
            lines.append(f"  {rec.message}")

    return "\n".join(lines) + "\n"


def format_report_json(report: EmergencyInvocationReport) -> str:
    """Render the report as a JSON document mirroring the text structure."""
    import json

    payload = {
        "summary": {
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "window_days": report.window_days,
            "scheduled_count": report.scheduled_count,
            "emergency_count": report.emergency_count,
        },
        "by_trigger_reason": [
            {
                "trigger_reason": b.trigger_reason,
                "total": b.total,
                "drove_remediating_action": b.drove_remediating_action,
                "produced_no_op": b.produced_no_op,
                "operator_overridden": b.operator_overridden,
                "false_positive_rate": b.false_positive_rate,
                "saturation": b.saturation.value,
            }
            for b in report.by_trigger_reason
        ],
        "per_invocation_detail": [
            {
                "invocation_id": d.invocation_id,
                "start_at": d.start_at.isoformat(),
                "trigger_reason": d.trigger_reason,
                "classification": d.classification.value,
                "pm_decision_count": d.pm_decision_count,
                "action_command_count": d.action_command_count,
                "regime_narrative": d.regime_narrative,
            }
            for d in report.per_invocation
        ],
        "recommendations": [
            {
                "trigger_reason": r.trigger_reason,
                "false_positive_rate": r.false_positive_rate,
                "message": r.message,
            }
            for r in report.recommendations
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=str, default=None)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--output", type=str, choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    engine = make_engine(args.db_path)
    factory = make_session_factory(engine)
    now = datetime.now(tz=UTC)
    with factory() as session:
        report = compute_emergency_invocation_report(
            session=session, now=now, window_days=args.window_days
        )
    if args.output == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))
    return 0


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "Classification",
    "EmergencyInvocationReport",
    "PerInvocationDetail",
    "Recommendation",
    "SaturationLevel",
    "TriggerReasonBucket",
    "compute_emergency_invocation_report",
    "format_report_json",
    "format_report_text",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
