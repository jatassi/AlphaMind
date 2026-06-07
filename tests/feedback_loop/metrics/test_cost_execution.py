"""Cost + key execution-process metrics (ALP-884 story 06b).

Each metric is a pure ``Metric.compute`` over a hand-built ``WindowDataset`` — no DB,
no session in scope. Fixtures build ``AgentCallRecord`` telemetry (the ``agent_calls``
bundle the cost metrics read) and activity-log entries (the ``activity_events`` bundle
the execution-process metrics read).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from alphamind._kernel.money import money, signed_money
from alphamind.feedback_loop.dataset import (
    ActivityEventsBundle,
    OutcomesBundle,
    ThesisOutcome,
    WindowDataset,
)
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.types import (
    UNCONDITIONED,
    Conditioning,
    MetricId,
)
from alphamind.portfolio_state.events.pm_decision import CommandAbandonedDetail, PMDecisionDetail
from alphamind.portfolio_state.events.position_lifecycle import PositionClosedDetail
from alphamind.portfolio_state.events.risk_guardrail import GuardrailRejectionDetail
from alphamind.portfolio_state.events.types import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMVerdict,
    PositionExitMethod,
)
from alphamind.state.tables.agent_calls import AgentCallRecord

from ._outcome_fixtures import make_thesis_outcome

_WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 7, 1, tzinfo=UTC)

_SEQ = [0]


def _agent_call(
    *,
    invocation_id: str = "inv-1",
    agent_name: str = "analyst",
    attempt_number: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    wall_clock_ms: int = 1,
) -> AgentCallRecord:
    _SEQ[0] += 1
    return AgentCallRecord(
        agent_call_id=f"call-{_SEQ[0]}",
        invocation_id=invocation_id,
        agent_name=agent_name,
        attempt_number=attempt_number,
        model_id="claude-opus-4-8",
        prompt_path="prompts/decision/analyst.md",
        prompt_git_sha="a" * 40,
        prompt_content_hash="b" * 64,
        sampling_params_json="{}",
        output_schema_ref=None,
        tools_definition_ref=None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        wall_clock_ms=wall_clock_ms,
        stop_reason="end_turn",
        success=True,
        error_class=None,
        error_message=None,
        output_artifact_ref=None,
    )


_TS = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _entry(
    *,
    event_type: EventType,
    event_group: EventGroup,
    source: EventSource,
    detail: Any,
    position_id: str | None = None,
    order_id: str | None = None,
) -> ActivityLogEntry:
    _SEQ[0] += 1
    return ActivityLogEntry(
        entry_id=f"ent-{_SEQ[0]}",
        invocation_id="inv-1",
        timestamp=_TS,
        event_type=event_type,
        event_group=event_group,
        position_id=position_id,
        order_id=order_id,
        thesis_id=None,
        source=source,
        detail=detail,
    )


def _guardrail_rejection() -> ActivityLogEntry:
    return _entry(
        event_type=EventType.GUARDRAIL_REJECTION,
        event_group=EventGroup.RISK_AND_GUARDRAIL,
        source=EventSource.GUARDRAIL_LAYER,
        detail=GuardrailRejectionDetail(
            command_summary="OPEN AAPL",
            blocking_rule_ids=("position_level_max_loss",),
            current_limit_values_json={},
            headroom_json={},
            suggested_modification=None,
        ),
    )


def _command_abandoned(*, command_type: str = "OPEN") -> ActivityLogEntry:
    _SEQ[0] += 1
    return _entry(
        event_type=EventType.COMMAND_ABANDONED,
        event_group=EventGroup.PM_DECISION,
        source=EventSource.COMMAND_EXECUTOR,
        detail=CommandAbandonedDetail(
            envelope_id=f"ENV-{_SEQ[0]}",
            command_id=f"CMD-{_SEQ[0]}",
            originating_agent="portfolio_manager",
            command_type=command_type,  # type: ignore[arg-type]
            failure_reason="broker_rejected",
            retry_attempt_count=3,
        ),
    )


def _position_closed(*, source: EventSource, order_id: str | None = None) -> ActivityLogEntry:
    return _entry(
        event_type=EventType.POSITION_CLOSED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        source=source,
        position_id="POS-1",
        order_id=order_id,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.STOP_TRIGGERED,
            exit_price=money("100"),
            realized_pnl_usd=signed_money("-50"),
            thesis_resolution_category="invalidated_stopped",
        ),
    )


def _pm_decision_entry(
    *, source_provenance: str, resulting_command_ids: tuple[str, ...] = ()
) -> ActivityLogEntry:
    return _entry(
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        source=EventSource.COMMAND_EXECUTOR,
        detail=PMDecisionDetail(
            envelope_id="ENV-pm",
            source_provenance_json={"source_provenance": source_provenance},
            evaluation_json={},
            modifications_json=[],
            resulting_command_ids=resulting_command_ids,
            verdict=PMVerdict.APPROVE,
            originating_proposal_json={},
        ),
    )


def _dataset(
    *,
    agent_calls: tuple[AgentCallRecord, ...] = (),
    outcomes: tuple[ThesisOutcome, ...] = (),
    activity_events: tuple[ActivityLogEntry, ...] = (),
) -> WindowDataset:
    return WindowDataset(
        start=_WINDOW_START,
        end=_WINDOW_END,
        agent_calls=agent_calls,
        pm_decision_log=(),
        validations=(),
        outcomes=OutcomesBundle(theses=outcomes),
        activity_events=ActivityEventsBundle(entries=activity_events),
    )


def _compute(
    metric_id: str, dataset: WindowDataset, conditioning: Conditioning = UNCONDITIONED
) -> Any:
    metric = get_metric(MetricId(metric_id))
    assert metric is not None, f"{metric_id} not registered"
    return metric.compute(dataset, conditioning)


class TestTokenCostPerInvocation:
    def test_sums_all_token_fields_divided_by_distinct_invocations(self) -> None:
        calls = (
            _agent_call(invocation_id="inv-1", input_tokens=100, output_tokens=10),
            _agent_call(invocation_id="inv-1", input_tokens=50, output_tokens=5),
            _agent_call(invocation_id="inv-2", input_tokens=20, output_tokens=2),
        )
        # total billable tokens = (100+10) + (50+5) + (20+2) = 187 over 2 invocations.
        result = _compute("cost_token_per_invocation", _dataset(agent_calls=calls))
        assert result.value == 187 / 2
        assert result.sample_size == 2

    def test_empty_window_gives_none(self) -> None:
        result = _compute("cost_token_per_invocation", _dataset())
        assert result.value is None
        assert result.sample_size == 0


class TestTokenCostPerAgent:
    def test_one_metric_per_agent_sums_that_agents_tokens(self) -> None:
        calls = (
            _agent_call(agent_name="analyst", input_tokens=100, output_tokens=10),
            _agent_call(agent_name="analyst", input_tokens=50, output_tokens=5),
            _agent_call(agent_name="strategist", input_tokens=20, output_tokens=2),
        )
        dataset = _dataset(agent_calls=calls)
        analyst = _compute("cost_token_per_agent__analyst", dataset)
        assert analyst.value == 165
        assert analyst.sample_size == 2
        strategist = _compute("cost_token_per_agent__strategist", dataset)
        assert strategist.value == 22
        assert strategist.sample_size == 1

    def test_agent_absent_from_window_gives_none(self) -> None:
        dataset = _dataset(agent_calls=(_agent_call(agent_name="analyst"),))
        result = _compute("cost_token_per_agent__synthesizer", dataset)
        assert result.value is None
        assert result.sample_size == 0


class TestCacheHitRatePerAgent:
    def test_rate_is_cache_read_over_cache_read_plus_input(self) -> None:
        calls = (
            _agent_call(agent_name="analyst", input_tokens=100, cache_read_tokens=300),
            _agent_call(agent_name="analyst", input_tokens=100, cache_read_tokens=0),
        )
        # cache_read=300 / (cache_read 300 + input 200) = 0.6
        result = _compute("cache_hit_rate__analyst", _dataset(agent_calls=calls))
        assert result.value == 0.6

    def test_no_calls_for_agent_gives_none(self) -> None:
        dataset = _dataset(agent_calls=(_agent_call(agent_name="analyst"),))
        result = _compute("cache_hit_rate__synthesizer", dataset)
        assert result.value is None


class TestFailureOverhead:
    def test_isolates_attempt_number_gt_1_tokens_over_total(self) -> None:
        calls = (
            _agent_call(attempt_number=1, input_tokens=100, output_tokens=0),
            _agent_call(attempt_number=2, input_tokens=20, output_tokens=5),
            _agent_call(attempt_number=3, input_tokens=10, output_tokens=5),
        )
        # retry tokens = (20+5) + (10+5) = 40 ; total = 100 + 25 + 15 = 140
        result = _compute("failure_overhead", _dataset(agent_calls=calls))
        assert result.value == 40 / 140
        assert result.sample_size == 140

    def test_no_retries_is_zero(self) -> None:
        calls = (_agent_call(attempt_number=1, input_tokens=100),)
        result = _compute("failure_overhead", _dataset(agent_calls=calls))
        assert result.value == 0.0

    def test_empty_window_gives_none(self) -> None:
        result = _compute("failure_overhead", _dataset())
        assert result.value is None


class TestLatencyBudgetHeadroom:
    def test_headroom_is_budget_minus_mean_over_budget(self) -> None:
        # analyst's packaged latency_budget_seconds is 750. Two calls averaging
        # 300_000 ms = 300 s. headroom = (750 - 300) / 750 = 0.6.
        calls = (
            _agent_call(agent_name="analyst", wall_clock_ms=200_000),
            _agent_call(agent_name="analyst", wall_clock_ms=400_000),
        )
        result = _compute("latency_budget_headroom__analyst", _dataset(agent_calls=calls))
        assert result.value == (750 - 300) / 750
        assert result.sample_size == 2

    def test_over_budget_is_negative_headroom(self) -> None:
        # one analyst call at 1500 s against a 750 s budget → (750 - 1500)/750 = -1.0
        calls = (_agent_call(agent_name="analyst", wall_clock_ms=1_500_000),)
        result = _compute("latency_budget_headroom__analyst", _dataset(agent_calls=calls))
        assert result.value == -1.0

    def test_no_calls_for_agent_gives_none(self) -> None:
        dataset = _dataset(agent_calls=(_agent_call(agent_name="analyst"),))
        result = _compute("latency_budget_headroom__synthesizer", dataset)
        assert result.value is None
        assert result.sample_size == 0


class TestCostPerResolvedThesis:
    def test_joins_total_token_cost_to_resolved_thesis_count(self) -> None:
        calls = (
            _agent_call(input_tokens=100, output_tokens=20),
            _agent_call(input_tokens=60, output_tokens=20),
        )
        outcomes = (
            make_thesis_outcome(thesis_id="thes-1"),
            make_thesis_outcome(thesis_id="thes-2"),
        )
        # total billable = 120 + 80 = 200 over 2 resolved theses.
        result = _compute(
            "cost_per_resolved_thesis", _dataset(agent_calls=calls, outcomes=outcomes)
        )
        assert result.value == 100.0
        assert result.sample_size == 2

    def test_no_resolved_theses_gives_none(self) -> None:
        calls = (_agent_call(input_tokens=100),)
        result = _compute("cost_per_resolved_thesis", _dataset(agent_calls=calls))
        assert result.value is None
        assert result.sample_size == 0


class TestGuardrailRejectionCount:
    def test_counts_only_guardrail_rejection_events(self) -> None:
        events = (
            _guardrail_rejection(),
            _guardrail_rejection(),
            _command_abandoned(),  # different event type — not counted
        )
        result = _compute("guardrail_rejection_count", _dataset(activity_events=events))
        assert result.value == 2.0

    def test_zero_when_no_rejections(self) -> None:
        result = _compute(
            "guardrail_rejection_count", _dataset(activity_events=(_command_abandoned(),))
        )
        assert result.value == 0.0


class TestCommandAbandonmentRate:
    def test_abandoned_over_total_commands_attempted(self) -> None:
        events = (
            # 3 commands successfully resulted from PM decisions ...
            _pm_decision_entry(
                source_provenance="pm_analyst", resulting_command_ids=("CMD-a", "CMD-b")
            ),
            _pm_decision_entry(source_provenance="pm_strategist", resulting_command_ids=("CMD-c",)),
            # ... and 1 was abandoned. total attempted = 3 + 1 = 4, abandoned = 1.
            _command_abandoned(),
        )
        result = _compute("command_abandonment_rate", _dataset(activity_events=events))
        assert result.value == 0.25
        assert result.sample_size == 4

    def test_no_commands_gives_none(self) -> None:
        result = _compute(
            "command_abandonment_rate", _dataset(activity_events=(_guardrail_rejection(),))
        )
        assert result.value is None
        assert result.sample_size == 0


class TestEngineOriginatedCloseFrequency:
    def test_counts_monitor_direct_source_closes(self) -> None:
        events = (
            _position_closed(source=EventSource.BRACKET_MANAGER),
            _position_closed(source=EventSource.MARGIN_MONITOR),
            _position_closed(source=EventSource.GUARDRAIL_LAYER),
            # a PM-initiated close (COMMAND_EXECUTOR, no engine cascade) is excluded
            _position_closed(source=EventSource.COMMAND_EXECUTOR, order_id="CMD-pm"),
        )
        result = _compute("engine_originated_close_frequency", _dataset(activity_events=events))
        assert result.value == 3.0

    def test_counts_engine_guardrail_cascade_close(self) -> None:
        events = (
            _pm_decision_entry(
                source_provenance="engine_guardrail", resulting_command_ids=("CMD-eng",)
            ),
            # the POSITION_CLOSED carrying that command id as order_id is a cascade close
            _position_closed(source=EventSource.FILL_PROCESSOR, order_id="CMD-eng"),
            # a fill-processor close not tied to an engine command is excluded
            _position_closed(source=EventSource.FILL_PROCESSOR, order_id="CMD-other"),
        )
        result = _compute("engine_originated_close_frequency", _dataset(activity_events=events))
        assert result.value == 1.0

    def test_zero_when_no_engine_closes(self) -> None:
        result = _compute(
            "engine_originated_close_frequency",
            _dataset(activity_events=(_guardrail_rejection(),)),
        )
        assert result.value == 0.0
