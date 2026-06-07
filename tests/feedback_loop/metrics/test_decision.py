"""Decision-layer process metrics (ALP-883 story 06a).

Each metric is a pure ``Metric.compute`` over a hand-built ``WindowDataset`` — no DB,
no session in scope. Fixtures build ``PM_DECISION`` activity-log entries carrying a
``PMDecisionDetail`` (the ``pm_decision_log`` bundle) and ``AgentCallRecord`` telemetry
(the ``agent_calls`` bundle, used only by the conditioning slice).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind.feedback_loop.dataset import WindowDataset
from alphamind.feedback_loop.metrics import get_metric, list_metrics
from alphamind.feedback_loop.metrics.types import (
    UNCONDITIONED,
    Conditioning,
    ConditioningDimension,
    MetricId,
)
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMVerdict,
)
from alphamind.state.tables.agent_calls import AgentCallRecord

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)
_TS = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

_PASS = {"status": "pass", "note": None}
_FAIL = {"status": "fail", "note": "trips here"}

_ANALYST_CRITERIA = (
    "falsifiability",
    "sizing_proportionality",
    "portfolio_coherence",
    "timing_plausibility",
    "counterargument_consideration",
)
_STRATEGIST_CRITERIA = (
    "status_classification_warrant",
    "action_status_alignment",
    "action_specific_justification",
    "portfolio_coherence",
)


def _analyst_evaluation(failed: tuple[str, ...] = ()) -> dict[str, Any]:
    return {c: (_FAIL if c in failed else _PASS) for c in _ANALYST_CRITERIA}


def _strategist_evaluation(failed: tuple[str, ...] = ()) -> dict[str, Any]:
    return {c: (_FAIL if c in failed else _PASS) for c in _STRATEGIST_CRITERIA}


def _analyst_proposal(conviction_level: int) -> dict[str, Any]:
    return {"conviction_level": conviction_level}


def _strategist_assessment(
    *,
    thesis_status: str,
    recommended_action: str,
    prior_status: str | None = None,
) -> dict[str, Any]:
    return {
        "thesis_status": thesis_status,
        "prior_status": prior_status,
        "recommended_action": recommended_action,
    }


_ENTRY_SEQ = [0]


def _pm_entry(
    *,
    verdict: PMVerdict,
    source_provenance: str = "pm_analyst",
    recommendation_type: str = "new_entry",
    evaluation_json: dict[str, Any] | None = None,
    modifications_json: list[dict[str, Any]] | None = None,
    originating_proposal_json: dict[str, Any] | None = None,
    invocation_id: str = "inv-1",
    timestamp: datetime = _TS,
) -> ActivityLogEntry:
    _ENTRY_SEQ[0] += 1
    detail = PMDecisionDetail(
        envelope_id=f"ENV-{_ENTRY_SEQ[0]}",
        source_provenance_json={
            "source_provenance": source_provenance,
            "source_recommendation_id": f"REC-{_ENTRY_SEQ[0]}",
            "recommendation_type": recommendation_type,
            "position_id": None if source_provenance == "pm_analyst" else "POS-1",
        },
        evaluation_json=evaluation_json or {},
        modifications_json=modifications_json or [],
        resulting_command_ids=(),
        verdict=verdict,
        originating_proposal_json=originating_proposal_json or {},
    )
    return ActivityLogEntry(
        entry_id=f"ent-{_ENTRY_SEQ[0]}",
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


def _agent_call(*, invocation_id: str, model_id: str, agent_name: str = "portfolio_manager") -> AgentCallRecord:
    _ENTRY_SEQ[0] += 1
    return AgentCallRecord(
        agent_call_id=f"call-{_ENTRY_SEQ[0]}",
        invocation_id=invocation_id,
        agent_name=agent_name,
        attempt_number=1,
        model_id=model_id,
        prompt_path="prompts/decision/pm.md",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        wall_clock_ms=1,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


def _dataset(
    pm_decision_log: tuple[ActivityLogEntry, ...] = (),
    agent_calls: tuple[AgentCallRecord, ...] = (),
) -> WindowDataset:
    return WindowDataset(
        start=_WINDOW_START,
        end=_WINDOW_END,
        agent_calls=agent_calls,
        pm_decision_log=pm_decision_log,
        validations=(),
    )


def _compute(metric_id: str, dataset: WindowDataset, conditioning: Conditioning = UNCONDITIONED) -> Any:
    metric = get_metric(MetricId(metric_id))
    assert metric is not None, f"{metric_id} not registered"
    return metric.compute(dataset, conditioning)


def _four_verdict_log() -> tuple[ActivityLogEntry, ...]:
    return (
        _pm_entry(verdict=PMVerdict.APPROVE),
        _pm_entry(verdict=PMVerdict.APPROVE_WITH_MODIFICATION),
        _pm_entry(verdict=PMVerdict.REJECT),
        _pm_entry(verdict=PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION),
    )


class TestPmVerdictMetrics:
    def test_approval_rate_counts_approve_and_modify(self) -> None:
        result = _compute("pm_approval_rate", _dataset(_four_verdict_log()))
        assert result.value == 0.5
        assert result.sample_size == 4

    def test_modification_rate_counts_only_modify(self) -> None:
        result = _compute("pm_modification_rate", _dataset(_four_verdict_log()))
        assert result.value == 0.25
        assert result.sample_size == 4

    def test_verdict_distribution_one_bin_per_verdict(self) -> None:
        log = (
            _pm_entry(verdict=PMVerdict.APPROVE),
            _pm_entry(verdict=PMVerdict.APPROVE),
            _pm_entry(verdict=PMVerdict.REJECT),
            _pm_entry(verdict=PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION),
        )
        dataset = _dataset(log)
        assert _compute("pm_verdict_rate__approve", dataset).value == 0.5
        assert _compute("pm_verdict_rate__reject", dataset).value == 0.25
        assert _compute("pm_verdict_rate__approve_with_modification", dataset).value == 0.0
        assert _compute("pm_verdict_rate__override_with_corrective_action", dataset).value == 0.25

    def test_empty_log_gives_none_value(self) -> None:
        result = _compute("pm_approval_rate", _dataset(()))
        assert result.value is None
        assert result.sample_size == 0


class TestPmCriterionFailRate:
    def test_analyst_criterion_fail_rate_over_analyst_envelopes_only(self) -> None:
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_analyst",
                evaluation_json=_analyst_evaluation(failed=("falsifiability",)),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_analyst",
                evaluation_json=_analyst_evaluation(),
            ),
            # A strategist envelope must not count toward an analyst criterion.
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                evaluation_json=_strategist_evaluation(failed=("portfolio_coherence",)),
            ),
        )
        result = _compute("pm_analyst_criterion_fail_rate__falsifiability", _dataset(log))
        assert result.value == 0.5
        assert result.sample_size == 2

    def test_portfolio_coherence_split_by_source(self) -> None:
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                evaluation_json=_strategist_evaluation(failed=("portfolio_coherence",)),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_analyst",
                evaluation_json=_analyst_evaluation(),
            ),
        )
        dataset = _dataset(log)
        strat = _compute("pm_strategist_criterion_fail_rate__portfolio_coherence", dataset)
        analyst = _compute("pm_analyst_criterion_fail_rate__portfolio_coherence", dataset)
        assert strat.value == 1.0
        assert strat.sample_size == 1
        assert analyst.value == 0.0
        assert analyst.sample_size == 1


class TestPmModificationCategory:
    def test_modification_category_distribution(self) -> None:
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE_WITH_MODIFICATION,
                modifications_json=[
                    {"adjustment_category": "risk_reduction"},
                    {"adjustment_category": "conviction_disagreement"},
                ],
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE_WITH_MODIFICATION,
                modifications_json=[{"adjustment_category": "risk_reduction"}],
            ),
        )
        dataset = _dataset(log)
        # Denominator is the total modification count (3), not envelopes.
        assert _compute("pm_modification_category_rate__risk_reduction", dataset).value == 2 / 3
        rate = _compute("pm_modification_category_rate__conviction_disagreement", dataset)
        assert rate.value == 1 / 3
        assert rate.sample_size == 3
        assert _compute("pm_modification_category_rate__capital_constraint", dataset).value == 0.0


class TestAnalystConvictionDistribution:
    def test_conviction_histogram_over_1_to_5(self) -> None:
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_analyst",
                originating_proposal_json=_analyst_proposal(conviction_level=5),
            ),
            _pm_entry(
                verdict=PMVerdict.REJECT,
                source_provenance="pm_analyst",
                originating_proposal_json=_analyst_proposal(conviction_level=5),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_analyst",
                originating_proposal_json=_analyst_proposal(conviction_level=2),
            ),
            # A strategist envelope carries no conviction — excluded from denominator.
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold"
                ),
            ),
        )
        dataset = _dataset(log)
        five = _compute("analyst_conviction_rate__5", dataset)
        assert five.value == 2 / 3
        assert five.sample_size == 3
        assert _compute("analyst_conviction_rate__2", dataset).value == 1 / 3
        assert _compute("analyst_conviction_rate__1", dataset).value == 0.0
        assert _compute("analyst_conviction_rate__3", dataset).value == 0.0
        assert _compute("analyst_conviction_rate__4", dataset).value == 0.0


def _strategist_log() -> tuple[ActivityLogEntry, ...]:
    return (
        _pm_entry(
            verdict=PMVerdict.APPROVE,
            source_provenance="pm_strategist",
            recommendation_type="position_assessment",
            originating_proposal_json=_strategist_assessment(
                thesis_status="on-track", recommended_action="hold", prior_status="at-risk"
            ),
        ),
        _pm_entry(
            verdict=PMVerdict.APPROVE,
            source_provenance="pm_strategist",
            recommendation_type="position_assessment",
            originating_proposal_json=_strategist_assessment(
                thesis_status="at-risk", recommended_action="reduce", prior_status="on-track"
            ),
        ),
        _pm_entry(
            verdict=PMVerdict.APPROVE,
            source_provenance="pm_strategist",
            recommendation_type="position_assessment",
            originating_proposal_json=_strategist_assessment(
                thesis_status="at-risk", recommended_action="hold", prior_status="at-risk"
            ),
        ),
    )


class TestStrategistDistributions:
    def test_status_distribution(self) -> None:
        dataset = _dataset(_strategist_log())
        assert _compute("strategist_status_rate__at_risk", dataset).value == 2 / 3
        on_track = _compute("strategist_status_rate__on_track", dataset)
        assert on_track.value == 1 / 3
        assert on_track.sample_size == 3
        assert _compute("strategist_status_rate__invalidated", dataset).value == 0.0

    def test_action_distribution_uses_snake_case_bins(self) -> None:
        dataset = _dataset(_strategist_log())
        assert _compute("strategist_action_rate__hold", dataset).value == 2 / 3
        assert _compute("strategist_action_rate__reduce", dataset).value == 1 / 3
        # adjust-bracket wire value maps to an adjust_bracket snake_case bin id.
        assert _compute("strategist_action_rate__adjust_bracket", dataset).value == 0.0


class TestStrategistTransitionMatrix:
    def test_transition_rate_from_prior_to_current(self) -> None:
        # Three assessments with prior_status: at-risk->on-track (x2), on-track->at-risk.
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold", prior_status="at-risk"
                ),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold", prior_status="at-risk"
                ),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="at-risk", recommended_action="reduce", prior_status="on-track"
                ),
            ),
        )
        dataset = _dataset(log)
        ar_to_ot = _compute(
            "strategist_status_transition_rate__at_risk__to__on_track", dataset
        )
        assert ar_to_ot.value == 2 / 3
        assert ar_to_ot.sample_size == 3
        assert (
            _compute(
                "strategist_status_transition_rate__on_track__to__at_risk", dataset
            ).value
            == 1 / 3
        )
        assert (
            _compute(
                "strategist_status_transition_rate__stale__to__invalidated", dataset
            ).value
            == 0.0
        )

    def test_assessments_without_prior_status_are_excluded(self) -> None:
        # prior_status=None must not count toward the transition denominator.
        log = (
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold", prior_status=None
                ),
            ),
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold", prior_status="at-risk"
                ),
            ),
        )
        result = _compute(
            "strategist_status_transition_rate__at_risk__to__on_track", _dataset(log)
        )
        assert result.value == 1.0
        assert result.sample_size == 1


class TestStrategistHoldOnNonOnTrack:
    def test_hold_on_at_risk_or_stale_over_at_risk_plus_stale(self) -> None:
        log = (
            # at-risk + hold -> counts
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="at-risk", recommended_action="hold"
                ),
            ),
            # stale + reduce -> in denominator, not numerator
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="stale", recommended_action="reduce"
                ),
            ),
            # on-track + hold -> excluded entirely
            _pm_entry(
                verdict=PMVerdict.APPROVE,
                source_provenance="pm_strategist",
                recommendation_type="position_assessment",
                originating_proposal_json=_strategist_assessment(
                    thesis_status="on-track", recommended_action="hold"
                ),
            ),
        )
        result = _compute("strategist_hold_on_non_on_track_rate", _dataset(log))
        assert result.value == 0.5
        assert result.sample_size == 2
