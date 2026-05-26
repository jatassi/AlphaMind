"""Boundary-model validation tests for ``/api/control/*`` (story 04a / ALP-668)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.command_center.control.models import (
    CancelOrderRequest,
    ControlError,
    ControlErrorEnvelope,
    ControlResponseEnvelope,
    ForceClosePositionRequest,
    ForceClosePositionResponse,
    PauseRequest,
    ResumeRequest,
    RunUniverseValidationRequest,
    RunUniverseValidationResponse,
    SetHaltModeRequest,
    SwitchProfileRequest,
    TriggerEmergencyInvocationRequest,
    TriggerEmergencyInvocationResponse,
    UniverseValidationCriterionRow,
    UniverseValidationReport,
    UniverseValidationTickerRow,
)


class TestRequestBodiesForbidExtra:
    def test_pause_request_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            PauseRequest.model_validate({"reason": "regime jump", "ttl": 5})

    def test_resume_request_rejects_any_field(self) -> None:
        with pytest.raises(ValidationError):
            ResumeRequest.model_validate({"reason": "drop me"})

    def test_trigger_emergency_request_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            TriggerEmergencyInvocationRequest.model_validate(
                {"reason": "halt event", "priority": "high"}
            )

    def test_switch_profile_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            SwitchProfileRequest.model_validate({"profile_name": "medium", "force": True})

    def test_cancel_order_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrderRequest.model_validate({"order_id": "ord-1", "reason": "drop"})

    def test_force_close_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ForceClosePositionRequest.model_validate(
                {"position_id": "pos-1", "rationale": "exit", "limit": 0.0}
            )

    def test_set_halt_mode_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            SetHaltModeRequest.model_validate({"enabled": True, "reason": "vol spike", "tier": 3})


class TestRequestBodiesValidate:
    def test_pause_requires_non_empty_reason(self) -> None:
        with pytest.raises(ValidationError):
            PauseRequest.model_validate({"reason": ""})

    def test_resume_accepts_empty(self) -> None:
        body = ResumeRequest.model_validate({})
        assert isinstance(body, ResumeRequest)

    def test_switch_profile_requires_non_empty_profile_name(self) -> None:
        with pytest.raises(ValidationError):
            SwitchProfileRequest.model_validate({"profile_name": ""})

    def test_cancel_order_requires_non_empty_order_id(self) -> None:
        with pytest.raises(ValidationError):
            CancelOrderRequest.model_validate({"order_id": ""})

    def test_force_close_requires_both_fields(self) -> None:
        with pytest.raises(ValidationError):
            ForceClosePositionRequest.model_validate({"position_id": "pos-1"})

    def test_set_halt_mode_round_trips(self) -> None:
        body = SetHaltModeRequest.model_validate({"enabled": True, "reason": "drawdown"})
        assert body.enabled is True
        assert body.reason == "drawdown"

    def test_universe_validation_request_accepts_empty(self) -> None:
        body = RunUniverseValidationRequest.model_validate({})
        assert isinstance(body, RunUniverseValidationRequest)


class TestResponseEnvelopes:
    def test_control_response_envelope_requires_aware_datetime(self) -> None:
        # Deliberately naive — exercises the AwareDatetime field's
        # rejection path. DTZ001 suppressed for this single line.
        naive = datetime(2026, 5, 26)  # noqa: DTZ001
        with pytest.raises(ValidationError):
            ControlResponseEnvelope.model_validate({"status": "accepted", "applied_at": naive})

    def test_control_response_envelope_round_trip(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        env = ControlResponseEnvelope(status="accepted", applied_at=applied_at)
        payload = env.model_dump(mode="json")
        assert payload["status"] == "accepted"
        assert payload["applied_at"].startswith("2026-05-26T12:00:00")

    def test_trigger_emergency_response_requires_invocation_id(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            TriggerEmergencyInvocationResponse.model_validate(
                {"status": "accepted", "applied_at": applied_at.isoformat()}
            )

    def test_trigger_emergency_response_round_trip(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        env = TriggerEmergencyInvocationResponse(
            status="accepted", applied_at=applied_at, invocation_id="inv-1"
        )
        assert env.invocation_id == "inv-1"

    def test_force_close_response_validates_envelope_id_pattern(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            ForceClosePositionResponse.model_validate(
                {
                    "status": "accepted",
                    "applied_at": applied_at.isoformat(),
                    "envelope_id": "BAD",
                }
            )

    def test_force_close_response_accepts_canonical_envelope_id(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        env = ForceClosePositionResponse(
            status="accepted", applied_at=applied_at, envelope_id="MON.s1.42"
        )
        assert env.envelope_id == "MON.s1.42"


class TestUniverseValidationReportShape:
    def _make_criterion_rows(self) -> list[UniverseValidationCriterionRow]:
        return [
            UniverseValidationCriterionRow(criterion="adv", verdict="pass"),
            UniverseValidationCriterionRow(criterion="analyst_coverage", verdict="pass"),
            UniverseValidationCriterionRow(criterion="beta", verdict="pass"),
            UniverseValidationCriterionRow(criterion="market_cap", verdict="pass"),
            UniverseValidationCriterionRow(criterion="options_oi", verdict="pass"),
        ]

    def test_ticker_row_requires_exactly_5_criteria(self) -> None:
        rows = self._make_criterion_rows()
        with pytest.raises(ValidationError):
            UniverseValidationTickerRow(ticker="AAPL", verdict="pass", criteria=rows[:4])

    def test_report_requires_at_least_one_ticker(self) -> None:
        with pytest.raises(ValidationError):
            UniverseValidationReport(validated_at=datetime(2026, 5, 26, tzinfo=UTC), tickers=[])

    def test_run_universe_validation_response_round_trip(self) -> None:
        applied_at = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        rows = self._make_criterion_rows()
        report = UniverseValidationReport(
            validated_at=applied_at,
            tickers=[UniverseValidationTickerRow(ticker="AAPL", verdict="pass", criteria=rows)],
        )
        env = RunUniverseValidationResponse(status="accepted", applied_at=applied_at, report=report)
        assert env.report.tickers[0].ticker == "AAPL"


class TestErrorEnvelope:
    def test_error_envelope_requires_known_code(self) -> None:
        with pytest.raises(ValidationError):
            ControlErrorEnvelope.model_validate({"error": {"code": "wat", "detail": "nope"}})

    def test_error_envelope_round_trip(self) -> None:
        env = ControlErrorEnvelope(
            error=ControlError(
                code="cooldown_active",
                detail="60s remaining",
                details={"cooldown_remaining_seconds": 60},
            )
        )
        payload = env.model_dump(mode="json")
        assert payload["error"]["code"] == "cooldown_active"
        assert payload["error"]["details"]["cooldown_remaining_seconds"] == 60

    def test_error_envelope_details_optional(self) -> None:
        env = ControlErrorEnvelope(error=ControlError(code="not_found", detail="missing"))
        assert env.error.details is None
