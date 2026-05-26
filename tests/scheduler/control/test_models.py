"""Tests for ``alphamind.scheduler.control.models`` (ALP-664).

Pydantic boundary models — every request body, response envelope, error
envelope, and per-event payload defined in
``docs/design/pipeline-control-and-events-schema.md`` § ``$defs`` has a
matching Pydantic class here.  These tests exercise the schema's
shape constraints (extra-forbid on empty-body verbs, minLength=1,
enum membership, required-field set) against the design doc.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.scheduler.control import models


class TestPauseRequest:
    def test_accepts_non_empty_reason(self) -> None:
        body = models.PauseRequest(reason="halt for due-diligence review")
        assert body.reason == "halt for due-diligence review"

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            models.PauseRequest(reason="")

    def test_rejects_unknown_fields(self) -> None:
        # Schema notes additionalProperties:false on empty-body verbs; the
        # request bodies that DO carry a body still use extra=forbid for
        # tight schema discipline.
        with pytest.raises(ValidationError):
            models.PauseRequest(reason="x", extra_field="nope")


class TestResumeRequest:
    def test_accepts_empty_body(self) -> None:
        body = models.ResumeRequest()
        assert body is not None

    def test_rejects_any_field(self) -> None:
        # Schema: additionalProperties:false on the empty body.
        with pytest.raises(ValidationError):
            models.ResumeRequest(reason="surprise")


class TestTriggerEmergencyInvocationRequest:
    def test_accepts_non_empty_reason(self) -> None:
        body = models.TriggerEmergencyInvocationRequest(reason="margin call")
        assert body.reason == "margin call"

    def test_rejects_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            models.TriggerEmergencyInvocationRequest(reason="")


class TestSwitchProfileRequest:
    def test_accepts_non_empty_profile_name(self) -> None:
        body = models.SwitchProfileRequest(profile_name="medium")
        assert body.profile_name == "medium"

    def test_rejects_empty_profile_name(self) -> None:
        with pytest.raises(ValidationError):
            models.SwitchProfileRequest(profile_name="")


class TestRunUniverseValidationRequest:
    def test_accepts_empty_body(self) -> None:
        body = models.RunUniverseValidationRequest()
        assert body is not None

    def test_rejects_any_field(self) -> None:
        with pytest.raises(ValidationError):
            models.RunUniverseValidationRequest(ticker="AAPL")


class TestControlResponseEnvelope:
    def test_status_must_be_accepted(self) -> None:
        env = models.ControlResponseEnvelope(
            status="accepted",
            applied_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert env.status == "accepted"

    def test_rejects_other_status_values(self) -> None:
        with pytest.raises(ValidationError):
            models.ControlResponseEnvelope(
                status="ok",
                applied_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            )


class TestControlErrorEnvelope:
    def test_round_trip_each_documented_code(self) -> None:
        for code in (
            "precondition_failed",
            "validation_failed",
            "not_found",
            "cooldown_active",
            "internal_error",
        ):
            env = models.ControlErrorEnvelope(
                error=models.ControlError(code=code, detail="explanation"),
            )
            assert env.error.code == code

    def test_rejects_unknown_code(self) -> None:
        with pytest.raises(ValidationError):
            models.ControlErrorEnvelope(
                error=models.ControlError(code="weird", detail="x"),
            )

    def test_details_field_is_optional_dict(self) -> None:
        env = models.ControlErrorEnvelope(
            error=models.ControlError(
                code="cooldown_active",
                detail="cooldown",
                details={
                    "cooldown_remaining_seconds": 1500,
                    "cooldown_started_at": "2026-05-26T12:00:00Z",
                },
            ),
        )
        assert env.error.details is not None
        assert env.error.details["cooldown_remaining_seconds"] == 1500


class TestEmergencyInvocationResponse:
    def test_extends_envelope_with_invocation_id(self) -> None:
        env = models.TriggerEmergencyInvocationResponse(
            status="accepted",
            applied_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            invocation_id="inv-123",
        )
        assert env.invocation_id == "inv-123"

    def test_invocation_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            models.TriggerEmergencyInvocationResponse(
                status="accepted",
                applied_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            )


class TestRunUniverseValidationResponse:
    def test_round_trip_with_report(self) -> None:
        env = models.RunUniverseValidationResponse(
            status="accepted",
            applied_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            report=models.UniverseValidationReport(
                validated_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
                tickers=[
                    models.UniverseValidationTickerRow(
                        ticker="AAPL",
                        verdict="pass",
                        criteria=[
                            models.UniverseValidationCriterionRow(criterion="adv", verdict="pass"),
                            models.UniverseValidationCriterionRow(
                                criterion="analyst_coverage", verdict="pass"
                            ),
                            models.UniverseValidationCriterionRow(criterion="beta", verdict="pass"),
                            models.UniverseValidationCriterionRow(
                                criterion="market_cap", verdict="pass"
                            ),
                            models.UniverseValidationCriterionRow(
                                criterion="options_oi", verdict="pass"
                            ),
                        ],
                    ),
                ],
            ),
        )
        assert env.report.tickers[0].ticker == "AAPL"

    def test_rejects_fewer_than_five_criteria(self) -> None:
        with pytest.raises(ValidationError):
            models.UniverseValidationTickerRow(
                ticker="AAPL",
                verdict="pass",
                criteria=[
                    models.UniverseValidationCriterionRow(criterion="adv", verdict="pass"),
                ],
            )

    def test_rejects_more_than_five_criteria(self) -> None:
        rows = [
            models.UniverseValidationCriterionRow(criterion=c, verdict="pass")
            for c in ("adv", "analyst_coverage", "beta", "market_cap", "options_oi")
        ]
        # Adding a sixth row — schema caps criteria at exactly 5 (min=max=5).
        with pytest.raises(ValidationError):
            models.UniverseValidationTickerRow(
                ticker="AAPL",
                verdict="pass",
                criteria=[
                    *rows,
                    models.UniverseValidationCriterionRow(criterion="adv", verdict="pass"),
                ],
            )


class TestVerdictEnums:
    @pytest.mark.parametrize("verdict", ["pass", "fail", "unknown"])
    def test_accepts_documented_verdicts(self, verdict: str) -> None:
        row = models.UniverseValidationCriterionRow(criterion="adv", verdict=verdict)
        assert row.verdict == verdict

    def test_rejects_unknown_verdict(self) -> None:
        with pytest.raises(ValidationError):
            models.UniverseValidationCriterionRow(
                criterion="adv",
                verdict="maybe",
            )


class TestEventPayloads:
    """One Pydantic class per event_name in the schema's `oneOf`."""

    def test_invocation_started_event_required_fields(self) -> None:
        event = models.InvocationStartedEvent(
            invocation_id="inv-1",
            run_type="emergency",
            started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert event.run_type == "emergency"

    def test_invocation_started_event_rejects_unknown_run_type(self) -> None:
        with pytest.raises(ValidationError):
            models.InvocationStartedEvent(
                invocation_id="inv-1",
                run_type="random",
                started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            )

    def test_phase_transition_event_rejects_unknown_phase(self) -> None:
        with pytest.raises(ValidationError):
            models.PhaseTransitionEvent(
                invocation_id="inv-1",
                phase="frobnicate",
                phase_started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            )

    @pytest.mark.parametrize(
        "phase",
        ["collect", "distill", "analyze", "decide", "execute"],
    )
    def test_phase_transition_accepts_all_documented_phases(self, phase: str) -> None:
        event = models.PhaseTransitionEvent(
            invocation_id="inv-1",
            phase=phase,
            phase_started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        )
        assert event.phase == phase

    def test_agent_started_event_requires_positive_latency_budget(self) -> None:
        with pytest.raises(ValidationError):
            models.AgentStartedEvent(
                invocation_id="inv-1",
                agent_name="analyst",
                started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
                latency_budget_seconds=0.0,
            )

    def test_agent_succeeded_event_tokens_used_required_fields(self) -> None:
        # input + output required; cache_read, cache_creation optional.
        event = models.AgentSucceededEvent(
            invocation_id="inv-1",
            agent_name="analyst",
            duration_seconds=12.5,
            tokens_used=models.TokensUsed(input=1000, output=200),
        )
        assert event.tokens_used.input == 1000

    def test_agent_succeeded_event_tokens_used_accepts_cache_fields(self) -> None:
        event = models.AgentSucceededEvent(
            invocation_id="inv-1",
            agent_name="analyst",
            duration_seconds=12.5,
            tokens_used=models.TokensUsed(
                input=1000,
                output=200,
                cache_read=500,
                cache_creation=100,
            ),
        )
        assert event.tokens_used.cache_read == 500

    def test_agent_retrying_event_attempt_min_is_two(self) -> None:
        with pytest.raises(ValidationError):
            models.AgentRetryingEvent(
                invocation_id="inv-1",
                agent_name="analyst",
                attempt=1,
                reason="timeout",
            )

    @pytest.mark.parametrize(
        "failure_mode",
        ["timeout", "malformed_output", "context_overflow", "model_api_error", "tool_use_error"],
    )
    def test_agent_retrying_event_accepts_all_failure_modes(self, failure_mode: str) -> None:
        event = models.AgentRetryingEvent(
            invocation_id="inv-1",
            agent_name="analyst",
            attempt=2,
            reason=failure_mode,
        )
        assert event.reason == failure_mode

    def test_agent_failed_event_required_fields(self) -> None:
        event = models.AgentFailedEvent(
            invocation_id="inv-1",
            agent_name="analyst",
            failure_mode="timeout",
        )
        assert event.failure_mode == "timeout"

    @pytest.mark.parametrize(
        "status",
        ["completed", "failed", "partial", "skipped_paused"],
    )
    def test_invocation_ended_event_accepts_all_statuses(self, status: str) -> None:
        event = models.InvocationEndedEvent(
            invocation_id="inv-1",
            status=status,
            commands_issued=0,
        )
        assert event.status == status

    def test_invocation_ended_event_rejects_negative_commands(self) -> None:
        with pytest.raises(ValidationError):
            models.InvocationEndedEvent(
                invocation_id="inv-1",
                status="completed",
                commands_issued=-1,
            )

    def test_next_trigger_changed_event_required_fields(self) -> None:
        event = models.NextTriggerChangedEvent(
            next_trigger_at=datetime(2026, 5, 26, 13, 0, 0, tzinfo=UTC),
            next_trigger_type="market_hours_rolling",
        )
        assert event.next_trigger_type == "market_hours_rolling"

    def test_heartbeat_event_required_fields(self) -> None:
        event = models.HeartbeatEvent(
            timestamp=datetime(2026, 5, 26, 12, 0, 15, tzinfo=UTC),
        )
        assert event.timestamp.tzinfo is not None


class TestAgentNameEnum:
    @pytest.mark.parametrize(
        "agent_name",
        [
            "domain_researcher_tech_semis",
            "domain_researcher_financials",
            "domain_researcher_energy",
            "qualitative_researcher",
            "adaptive_researcher",
            "synthesizer",
            "analyst",
            "strategist",
            "portfolio_manager",
        ],
    )
    def test_accepts_all_documented_agent_names(self, agent_name: str) -> None:
        event = models.AgentStartedEvent(
            invocation_id="inv-1",
            agent_name=agent_name,
            started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
            latency_budget_seconds=60.0,
        )
        assert event.agent_name == agent_name

    def test_rejects_unknown_agent_name(self) -> None:
        with pytest.raises(ValidationError):
            models.AgentStartedEvent(
                invocation_id="inv-1",
                agent_name="proposal_pre_processor",  # NOT an LLM agent
                started_at=datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
                latency_budget_seconds=60.0,
            )
