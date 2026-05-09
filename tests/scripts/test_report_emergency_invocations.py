"""Tests for ``scripts/report_emergency_invocations.py`` (story 15 / ALP-99).

The report joins ``invocations.trigger_type='emergency'`` to ``activity_log``
to classify each emergency invocation by downstream consequence and surface
the per-trigger-reason false-positive rate the operator uses to decide
whether to disable ``regime_skip_emergency_trigger`` or re-tune any other
emergency-trigger logic.

Coverage:
- Empty-window run produces a "no emergency invocations in window" report.
- Trigger reasons bucket the emergency invocations.
- Classification rules (drove remediating action / produced no-op /
  operator-overridden) match the story-15 spec, including the precedence
  ``operator-overridden`` > ``drove remediating action`` > ``produced no-op``.
- False-positive rate computation per trigger reason.
- Saturation indicators (HEALTHY vs. REVIEW) honour the small-N suppression.
- JSON output mirrors the text structure with the four documented top-level
  keys.
- Recommendation entries cross-reference ``verify_regime_transition.py`` for
  ``regime_skip_emergency`` REVIEW verdicts.
- Per-invocation regime narrative falls back gracefully when the
  ``distillation_regime_state`` row is missing.
- ``main()`` returns 0 even when REVIEW verdicts surface — non-zero exit is
  reserved for script-internal errors.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alphamind.execution.state_persistence.invocation_context.activity_log import (
    activity_log_entry_to_row,
)
from alphamind.execution.state_persistence.invocation_context.records import (
    invocation_record_to_row,
)
from alphamind.execution.state_persistence.tables.process_lifetimes import (
    ProcessLifetimeRow,
)
from alphamind.persistence.models import Base, DistillationRegimeState
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    OrderCancelledDetail,
    PMDecisionDetail,
    PMVerdict,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.scripts.report_emergency_invocations import (
    Classification,
    EmergencyInvocationReport,
    SaturationLevel,
    compute_emergency_invocation_report,
    format_report_json,
    format_report_text,
    main,
)

# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_PROC_ID = "proc-1"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with state-persistence tables registered."""
    # Registers ActivityLogRow / InvocationRow / ProcessLifetimeRow on Base.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        # Every invocation row needs a parent process_lifetimes row.
        sess.add(_make_process_lifetime_row())
        sess.commit()
        yield sess


def _make_process_lifetime_row() -> ProcessLifetimeRow:
    return ProcessLifetimeRow(
        process_lifetime_id=_PROC_ID,
        process_role="pipeline",
        process_start_at="2026-04-01T00:00:00Z",
        process_pid=12345,
        hostname="host",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux",
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _add_invocation(
    session: Session,
    *,
    invocation_id: str,
    start_at: datetime,
    trigger_type: str = "emergency",
    trigger_reason: str = "regime_skip_emergency",
    trigger_source: str = "continuous_monitor",
) -> None:
    """Insert one ``invocations`` row using the typed-record adapter."""
    from alphamind.execution.state_persistence.invocation_context.records import (
        InvocationRecord,
    )

    record = InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROC_ID,
        start_at=_iso(start_at),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type=trigger_type,  # type: ignore[arg-type]
        trigger_source=trigger_source,
        trigger_reason=trigger_reason,
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/{invocation_id}/config.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=f"/tmp/{invocation_id}/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    session.add(invocation_record_to_row(record))


def _add_pm_decision(
    session: Session,
    *,
    invocation_id: str,
    timestamp: datetime,
    entry_id: str,
    verdict: PMVerdict = PMVerdict.APPROVE,
    resulting_command_ids: tuple[str, ...] = ("cmd-1",),
    envelope_id: str = "env-1",
) -> None:
    detail = PMDecisionDetail(
        envelope_id=envelope_id,
        source_provenance_json={"agent": "strategist"},
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=resulting_command_ids,
        verdict=verdict,
    )
    entry = ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=detail,
    )
    session.add(activity_log_entry_to_row(entry))


def _add_position_closed(
    session: Session,
    *,
    invocation_id: str,
    timestamp: datetime,
    entry_id: str,
    position_id: str = "pos-1",
) -> None:
    detail = PositionClosedDetail(
        exit_method=PositionExitMethod.PM_DECISION,
        exit_price=100.0,
        realized_pnl_usd=-50.0,
        thesis_resolution_category="risk_management",
    )
    entry = ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.FILL_PROCESSOR,
        detail=detail,
    )
    session.add(activity_log_entry_to_row(entry))


def _add_order_cancelled(
    session: Session,
    *,
    invocation_id: str,
    timestamp: datetime,
    entry_id: str,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
    cancel_reason: str = "operator_pause",
) -> None:
    detail = OrderCancelledDetail(
        cancel_reason=cancel_reason,
        filled_quantity_at_cancellation=0,
    )
    entry = ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.ORDER_CANCELLED,
        event_group=EventGroup.ORDER_LIFECYCLE,
        position_id=None,
        order_id="ord-1",
        thesis_id=None,
        source=source,
        detail=detail,
    )
    session.add(activity_log_entry_to_row(entry))


def _add_regime_state(
    session: Session,
    *,
    as_of: datetime,
    regime_label: str = "vol_expansion",
    prior_label: str = "low_vol_compression",
    transition_state: str = "early-strong",
) -> None:
    session.add(
        DistillationRegimeState(
            as_of=_iso(as_of),
            regime_label=regime_label,
            vix_level=18.0,
            term_structure_basis=0.5,
            vvix_percentile=50.0,
            realized_vol=12.0,
            indicator_agreement_count=4,
            invocations_held=1,
            transition_state=transition_state,
            prior_label=prior_label,
            ingested_at=_iso(as_of),
        )
    )


# ---------------------------------------------------------------------------
# Tests — empty window, basic bucketing
# ---------------------------------------------------------------------------


class TestEmptyWindow:
    def test_returns_zero_emergency_count_and_no_recommendations(self, session: Session) -> None:
        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)

        assert isinstance(report, EmergencyInvocationReport)
        assert report.emergency_count == 0
        assert report.scheduled_count == 0
        assert report.by_trigger_reason == ()
        assert report.per_invocation == ()
        assert report.recommendations == ()

    def test_text_renders_no_emergencies_line(self, session: Session) -> None:
        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        text = format_report_text(report)

        assert "Emergency invocation review" in text
        assert "no emergency invocations in window" in text

    def test_main_exits_zero_on_empty_window(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``main()`` invokes the report against the configured DB; for the
        # test we override ``compute_emergency_invocation_report`` to use
        # the pytest session via dependency-injection at the module level.
        from alphamind.scripts import report_emergency_invocations as mod

        def _fake_engine(_path: str | None) -> Any:
            return session.bind

        monkeypatch.setattr(mod, "make_engine", _fake_engine)
        # ``make_session_factory`` returns a sessionmaker; bind to the same
        # in-memory engine so the script reads from our test fixture.
        rc = main(["--window-days", "90"])
        assert rc == 0


# ---------------------------------------------------------------------------
# Trigger reason bucketing
# ---------------------------------------------------------------------------


class TestTriggerReasonBucketing:
    def test_groups_emergency_invocations_by_trigger_reason(self, session: Session) -> None:
        # Three regime-skip emergencies, two guardrail-tier emergencies,
        # one scheduled invocation.
        for i in range(3):
            _add_invocation(
                session,
                invocation_id=f"inv-skip-{i}",
                start_at=_NOW - timedelta(days=i + 1),
                trigger_reason="regime_skip_emergency",
            )
        for i in range(2):
            _add_invocation(
                session,
                invocation_id=f"inv-tier-{i}",
                start_at=_NOW - timedelta(days=i + 4),
                trigger_reason="guardrail_progressive_tier_breach",
            )
        _add_invocation(
            session,
            invocation_id="inv-sched-0",
            start_at=_NOW - timedelta(days=2),
            trigger_type="scheduled",
            trigger_reason="0 9 * * 1-5",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)

        assert report.scheduled_count == 1
        assert report.emergency_count == 5
        reasons = {b.trigger_reason: b for b in report.by_trigger_reason}
        assert reasons["regime_skip_emergency"].total == 3
        assert reasons["guardrail_progressive_tier_breach"].total == 2

    def test_excludes_invocations_outside_the_window(self, session: Session) -> None:
        _add_invocation(
            session,
            invocation_id="inv-recent",
            start_at=_NOW - timedelta(days=10),
            trigger_reason="regime_skip_emergency",
        )
        _add_invocation(
            session,
            invocation_id="inv-stale",
            start_at=_NOW - timedelta(days=200),
            trigger_reason="regime_skip_emergency",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)

        assert report.emergency_count == 1
        assert report.per_invocation[0].invocation_id == "inv-recent"


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------


class TestClassification:
    def test_approve_with_action_command_drove_remediating_action(self, session: Session) -> None:
        """An approved PM decision plus a defensive POSITION_CLOSED entry counts."""
        start_at = _NOW - timedelta(days=5)
        _add_invocation(
            session,
            invocation_id="inv-1",
            start_at=start_at,
        )
        session.flush()
        _add_pm_decision(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(seconds=10),
            entry_id="pm-1",
            verdict=PMVerdict.APPROVE,
        )
        _add_position_closed(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(seconds=20),
            entry_id="close-1",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.DROVE_REMEDIATING_ACTION

    def test_only_rejections_classifies_as_produced_no_op(self, session: Session) -> None:
        start_at = _NOW - timedelta(days=5)
        _add_invocation(session, invocation_id="inv-1", start_at=start_at)
        session.flush()
        for i in range(3):
            _add_pm_decision(
                session,
                invocation_id="inv-1",
                timestamp=start_at + timedelta(seconds=i),
                entry_id=f"pm-{i}",
                verdict=PMVerdict.REJECT,
                resulting_command_ids=(),
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.PRODUCED_NO_OP

    def test_zero_pm_decisions_classifies_as_produced_no_op(self, session: Session) -> None:
        """Aborted-before-PM emergencies count as no-op per the story spec."""
        start_at = _NOW - timedelta(days=5)
        _add_invocation(session, invocation_id="inv-1", start_at=start_at)
        session.flush()
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.PRODUCED_NO_OP

    def test_operator_console_pause_within_window_classifies_as_overridden(
        self, session: Session
    ) -> None:
        start_at = _NOW - timedelta(days=5)
        _add_invocation(session, invocation_id="inv-1", start_at=start_at)
        session.flush()
        # Operator-console event 3 minutes after invocation → override.
        _add_order_cancelled(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(minutes=3),
            entry_id="cancel-1",
            source=EventSource.OPERATOR_CONSOLE,
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.OPERATOR_OVERRIDDEN

    def test_operator_console_event_outside_5min_window_does_not_classify_as_overridden(
        self, session: Session
    ) -> None:
        """An operator-console event 10m after invocation is not a response to it."""
        start_at = _NOW - timedelta(days=5)
        _add_invocation(session, invocation_id="inv-1", start_at=start_at)
        session.flush()
        _add_order_cancelled(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(minutes=10),
            entry_id="cancel-1",
            source=EventSource.OPERATOR_CONSOLE,
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.PRODUCED_NO_OP

    def test_operator_overridden_takes_precedence_over_action(self, session: Session) -> None:
        """Story-15 precedence: operator-overridden > drove action > no-op."""
        start_at = _NOW - timedelta(days=5)
        _add_invocation(session, invocation_id="inv-1", start_at=start_at)
        session.flush()
        _add_pm_decision(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(seconds=5),
            entry_id="pm-1",
            verdict=PMVerdict.APPROVE,
        )
        _add_position_closed(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(seconds=10),
            entry_id="close-1",
        )
        _add_order_cancelled(
            session,
            invocation_id="inv-1",
            timestamp=start_at + timedelta(minutes=2),
            entry_id="cancel-1",
            source=EventSource.OPERATOR_CONSOLE,
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.classification == Classification.OPERATOR_OVERRIDDEN


# ---------------------------------------------------------------------------
# False-positive rate + saturation
# ---------------------------------------------------------------------------


def _seed_emergency_with_classification(
    session: Session,
    *,
    invocation_id: str,
    start_at: datetime,
    classification: Classification,
    trigger_reason: str = "regime_skip_emergency",
) -> None:
    _add_invocation(
        session,
        invocation_id=invocation_id,
        start_at=start_at,
        trigger_reason=trigger_reason,
    )
    session.flush()
    if classification == Classification.OPERATOR_OVERRIDDEN:
        _add_order_cancelled(
            session,
            invocation_id=invocation_id,
            timestamp=start_at + timedelta(minutes=2),
            entry_id=f"{invocation_id}-cancel",
            source=EventSource.OPERATOR_CONSOLE,
        )
    elif classification == Classification.DROVE_REMEDIATING_ACTION:
        _add_pm_decision(
            session,
            invocation_id=invocation_id,
            timestamp=start_at + timedelta(seconds=5),
            entry_id=f"{invocation_id}-pm",
            verdict=PMVerdict.APPROVE,
        )
        _add_position_closed(
            session,
            invocation_id=invocation_id,
            timestamp=start_at + timedelta(seconds=10),
            entry_id=f"{invocation_id}-close",
        )
    # Else PRODUCED_NO_OP — leave the activity log empty.


class TestFalsePositiveRate:
    def test_rate_combines_no_op_and_overridden(self, session: Session) -> None:
        # 1 action, 2 no-op, 1 overridden → 3/4 = 0.75
        _seed_emergency_with_classification(
            session,
            invocation_id="inv-act",
            start_at=_NOW - timedelta(days=10),
            classification=Classification.DROVE_REMEDIATING_ACTION,
        )
        _seed_emergency_with_classification(
            session,
            invocation_id="inv-noop-1",
            start_at=_NOW - timedelta(days=11),
            classification=Classification.PRODUCED_NO_OP,
        )
        _seed_emergency_with_classification(
            session,
            invocation_id="inv-noop-2",
            start_at=_NOW - timedelta(days=12),
            classification=Classification.PRODUCED_NO_OP,
        )
        _seed_emergency_with_classification(
            session,
            invocation_id="inv-over",
            start_at=_NOW - timedelta(days=13),
            classification=Classification.OPERATOR_OVERRIDDEN,
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        bucket = report.by_trigger_reason[0]

        assert bucket.total == 4
        assert bucket.drove_remediating_action == 1
        assert bucket.produced_no_op == 2
        assert bucket.operator_overridden == 1
        assert bucket.false_positive_rate == pytest.approx(0.75)
        assert bucket.saturation == SaturationLevel.REVIEW

    def test_small_n_suppresses_review_verdict(self, session: Session) -> None:
        """1 of 1 no-op stays HEALTHY — small-N suppression per story 15."""
        _seed_emergency_with_classification(
            session,
            invocation_id="inv-noop",
            start_at=_NOW - timedelta(days=10),
            classification=Classification.PRODUCED_NO_OP,
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        bucket = report.by_trigger_reason[0]

        assert bucket.false_positive_rate == pytest.approx(1.0)
        assert bucket.saturation == SaturationLevel.HEALTHY

    def test_low_rate_at_action_threshold_stays_healthy(self, session: Session) -> None:
        """4 of 4 actions → 0% false-positive → HEALTHY even with N=4."""
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.DROVE_REMEDIATING_ACTION,
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        bucket = report.by_trigger_reason[0]

        assert bucket.false_positive_rate == pytest.approx(0.0)
        assert bucket.saturation == SaturationLevel.HEALTHY


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


class TestRecommendations:
    def test_review_triggers_recommendation_with_regime_skip_cross_ref(
        self, session: Session
    ) -> None:
        """Story-15 spec: regime_skip_emergency REVIEW links verify_regime_transition.py."""
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.PRODUCED_NO_OP,
                trigger_reason="regime_skip_emergency",
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)

        assert len(report.recommendations) == 1
        rec = report.recommendations[0]
        assert rec.trigger_reason == "regime_skip_emergency"
        assert rec.false_positive_rate == pytest.approx(1.0)
        assert "verify_regime_transition.py" in rec.message
        assert "regime_skip_emergency_trigger" in rec.message

    def test_healthy_buckets_produce_no_recommendation(self, session: Session) -> None:
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.DROVE_REMEDIATING_ACTION,
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        assert report.recommendations == ()

    def test_review_for_non_regime_reason_omits_regime_cross_ref(self, session: Session) -> None:
        """Cross-ref to verify_regime_transition.py is regime_skip-specific."""
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.PRODUCED_NO_OP,
                trigger_reason="guardrail_progressive_tier_breach",
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        assert len(report.recommendations) == 1
        rec = report.recommendations[0]
        assert "verify_regime_transition.py" not in rec.message


# ---------------------------------------------------------------------------
# Regime narrative
# ---------------------------------------------------------------------------


class TestRegimeNarrative:
    def test_regime_skip_emergency_surfaces_prior_and_new_label(self, session: Session) -> None:
        start_at = _NOW - timedelta(days=5)
        _add_invocation(
            session,
            invocation_id="inv-skip",
            start_at=start_at,
            trigger_reason="regime_skip_emergency",
        )
        session.flush()
        # Regime row at the same timestamp — describes the post-skip label.
        _add_regime_state(
            session,
            as_of=start_at,
            regime_label="vol_expansion",
            prior_label="low_vol_compression",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert "low_vol_compression" in detail.regime_narrative
        assert "vol_expansion" in detail.regime_narrative

    def test_missing_regime_state_falls_back_to_unavailable(self, session: Session) -> None:
        _add_invocation(
            session,
            invocation_id="inv-skip",
            start_at=_NOW - timedelta(days=5),
            trigger_reason="regime_skip_emergency",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.regime_narrative == "regime narrative unavailable"

    def test_non_regime_skip_invocations_have_unavailable_narrative(self, session: Session) -> None:
        """The narrative is regime-skip-specific per the story spec."""
        _add_invocation(
            session,
            invocation_id="inv-tier",
            start_at=_NOW - timedelta(days=5),
            trigger_reason="guardrail_progressive_tier_breach",
        )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        detail = report.per_invocation[0]
        assert detail.regime_narrative == "regime narrative unavailable"


# ---------------------------------------------------------------------------
# Text & JSON output
# ---------------------------------------------------------------------------


class TestTextOutput:
    def test_text_output_renders_summary_buckets_per_invocation_and_recommendations(
        self, session: Session
    ) -> None:
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.PRODUCED_NO_OP,
                trigger_reason="regime_skip_emergency",
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        text = format_report_text(report)

        assert "Emergency invocation review" in text
        assert "Emergency invocations:" in text
        assert "BY TRIGGER REASON" in text
        assert "regime_skip_emergency" in text
        assert "drove remediating action" in text
        assert "produced no-op" in text
        assert "PER-INVOCATION DETAIL" in text
        assert "RECOMMENDATION" in text
        assert "REVIEW" in text


class TestJsonOutput:
    def test_json_output_carries_documented_top_level_keys(self, session: Session) -> None:
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.PRODUCED_NO_OP,
                trigger_reason="regime_skip_emergency",
            )
        session.commit()

        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        payload = json.loads(format_report_json(report))

        assert set(payload) == {
            "summary",
            "by_trigger_reason",
            "per_invocation_detail",
            "recommendations",
        }
        assert payload["summary"]["emergency_count"] == 4
        assert payload["summary"]["window_days"] == 90
        bucket = payload["by_trigger_reason"][0]
        assert bucket["trigger_reason"] == "regime_skip_emergency"
        assert bucket["total"] == 4
        assert bucket["saturation"] == "REVIEW"
        assert payload["recommendations"][0]["trigger_reason"] == "regime_skip_emergency"
        # per-invocation entries carry classification + narrative
        per_inv = payload["per_invocation_detail"]
        assert len(per_inv) == 4
        assert per_inv[0]["classification"] == "produced_no_op"
        assert "regime_narrative" in per_inv[0]

    def test_json_output_with_empty_window_carries_empty_lists(self, session: Session) -> None:
        report = compute_emergency_invocation_report(session=session, now=_NOW, window_days=90)
        payload = json.loads(format_report_json(report))
        assert payload["by_trigger_reason"] == []
        assert payload["per_invocation_detail"] == []
        assert payload["recommendations"] == []
        assert payload["summary"]["emergency_count"] == 0


# ---------------------------------------------------------------------------
# CLI exit code
# ---------------------------------------------------------------------------


class TestCliExitCode:
    def test_main_returns_zero_when_review_verdicts_surface(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Story-15 invariant: REVIEW verdicts inform; they don't gate."""
        for i in range(4):
            _seed_emergency_with_classification(
                session,
                invocation_id=f"inv-{i}",
                start_at=_NOW - timedelta(days=10 + i),
                classification=Classification.PRODUCED_NO_OP,
                trigger_reason="regime_skip_emergency",
            )
        session.commit()

        from alphamind.scripts import report_emergency_invocations as mod

        def _fake_engine(_path: str | None) -> Any:
            return session.bind

        monkeypatch.setattr(mod, "make_engine", _fake_engine)

        rc = main(["--window-days", "90"])
        assert rc == 0

    def test_main_emits_json_when_requested(
        self,
        session: Session,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from alphamind.scripts import report_emergency_invocations as mod

        def _fake_engine(_path: str | None) -> Any:
            return session.bind

        monkeypatch.setattr(mod, "make_engine", _fake_engine)

        rc = main(["--window-days", "90", "--output", "json"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert "summary" in payload
