"""Tests for ``command_center.control.pipeline_client`` (story 04a / ALP-668).

The :class:`RealPipelineClient` is exercised against an in-memory
:class:`httpx.MockTransport` so the parser logic + error-envelope
mapping is covered without booting the loopback surface. The
:class:`FakePipelineClient` is exercised directly so tests of the
proxy / routes layer can rely on its canned-response shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from alphamind.command_center._kernel.control import ControlErrorCode
from alphamind.command_center.control.pipeline_client import (
    FakePipelineClient,
    PipelineRunUniverseValidationResult,
    PipelineSwitchProfileResult,
    PipelineTriggerEmergencyResult,
    RealPipelineClient,
)
from alphamind.config.control_handlers.profile_switch import ProfileSwitchOutcome
from alphamind.config.models.main import Profile


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> RealPipelineClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return RealPipelineClient(base_url="http://127.0.0.1:8765", http_client=http)


class TestRealPipelineClientHappyPaths:
    async def test_pause_success(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/control/pause"
            assert json.loads(req.content) == {"reason": "halt"}
            return httpx.Response(
                200,
                json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"},
            )

        client = _make_client(handler)
        result = await client.pause(reason="halt")
        assert result.ok is True
        assert result.applied_at == "2026-05-26T12:00:00Z"

    async def test_resume_success_with_empty_body(self) -> None:
        seen: list[bytes] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.content)
            assert req.url.path == "/control/resume"
            return httpx.Response(
                200,
                json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"},
            )

        client = _make_client(handler)
        result = await client.resume()
        assert result.ok is True
        assert seen[0] == b"{}"

    async def test_trigger_emergency_success_returns_invocation_id(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "applied_at": "2026-05-26T12:00:00Z",
                    "invocation_id": "inv-99",
                },
            )

        client = _make_client(handler)
        out = await client.trigger_emergency_invocation(reason="drawdown")
        assert isinstance(out, PipelineTriggerEmergencyResult)
        assert out.result.ok is True
        assert out.invocation_id == "inv-99"

    async def test_switch_profile_returns_outcome_from_payload(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "applied_at": "2026-05-26T12:00:00Z",
                    "outcome": {
                        "previous_profile": "medium",
                        "new_profile": "large",
                        "main_yaml_path": "config/main.yaml",
                        "is_no_op": False,
                    },
                },
            )

        client = _make_client(handler)
        out = await client.switch_profile(profile_name="large")
        assert isinstance(out, PipelineSwitchProfileResult)
        assert out.result.ok is True
        assert out.outcome is not None
        assert out.outcome.previous_profile == Profile.medium
        assert out.outcome.new_profile == Profile.large
        assert out.outcome.is_no_op is False

    async def test_run_universe_validation_returns_report(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "applied_at": "2026-05-26T12:00:00Z",
                    "report": {
                        "validated_at": "2026-05-26T12:00:00+00:00",
                        "tickers": [
                            {
                                "ticker": "AAPL",
                                "verdict": "pass",
                                "criteria": [
                                    {"criterion": "adv", "verdict": "pass"},
                                    {"criterion": "analyst_coverage", "verdict": "pass"},
                                    {"criterion": "beta", "verdict": "pass"},
                                    {"criterion": "market_cap", "verdict": "pass"},
                                    {"criterion": "options_oi", "verdict": "pass"},
                                ],
                            }
                        ],
                    },
                },
            )

        client = _make_client(handler)
        out = await client.run_universe_validation()
        assert isinstance(out, PipelineRunUniverseValidationResult)
        assert out.result.ok is True
        assert out.report is not None
        assert out.report.tickers[0].ticker == "AAPL"
        assert len(out.report.tickers[0].criteria) == 5


class TestRealPipelineClientErrorEnvelopes:
    async def test_404_with_nested_envelope(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"detail": {"error": {"code": "not_found", "detail": "no profile"}}},
            )

        client = _make_client(handler)
        out = await client.switch_profile(profile_name="zzz")
        assert out.result.ok is False
        assert out.result.error_code == ControlErrorCode.NOT_FOUND
        assert out.result.error_detail == "no profile"
        assert out.outcome is None

    async def test_409_cooldown_active(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "error": {
                            "code": "cooldown_active",
                            "detail": "60s remaining",
                            "details": {"cooldown_remaining_seconds": 60},
                        }
                    }
                },
            )

        client = _make_client(handler)
        out = await client.trigger_emergency_invocation(reason="drawdown")
        assert out.result.ok is False
        assert out.result.error_code == ControlErrorCode.COOLDOWN_ACTIVE
        assert out.invocation_id is None

    async def test_400_validation_failed_top_level_envelope(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # Some upstreams emit the envelope without the FastAPI ``detail``
            # wrapper (e.g. when the route returns a raw JSONResponse). The
            # client must accept both shapes.
            return httpx.Response(
                400,
                json={"error": {"code": "validation_failed", "detail": "bad input"}},
            )

        client = _make_client(handler)
        result = await client.pause(reason="anything")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.VALIDATION_FAILED

    async def test_500_falls_back_to_internal_error_on_missing_envelope(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="<html>oops</html>")

        client = _make_client(handler)
        result = await client.pause(reason="x")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.INTERNAL_ERROR

    async def test_transport_error_maps_to_internal_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_client(handler)
        result = await client.pause(reason="x")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.INTERNAL_ERROR
        assert "transport error" in (result.error_detail or "")


class TestRealPipelineClientMalformedSuccessResponses:
    async def test_pause_missing_applied_at_is_internal_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "accepted"})

        client = _make_client(handler)
        result = await client.pause(reason="x")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.INTERNAL_ERROR

    async def test_switch_profile_missing_outcome_is_internal_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            # No outcome field — schema-divergent upstream.
            return httpx.Response(
                200, json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"}
            )

        client = _make_client(handler)
        out = await client.switch_profile(profile_name="large")
        assert out.result.ok is False
        assert out.result.error_code == ControlErrorCode.INTERNAL_ERROR


class TestFakePipelineClient:
    async def test_default_pause_returns_success(self) -> None:
        client = FakePipelineClient()
        result = await client.pause(reason="x")
        assert result.ok is True
        assert client.calls == [("pause", {"reason": "x"})]

    async def test_pause_failure_response_override(self) -> None:
        from alphamind.command_center._kernel.control import ControlResult

        client = FakePipelineClient()
        client.set_pause_response(
            ControlResult.failure(
                error_code=ControlErrorCode.PRECONDITION_FAILED,
                error_detail="already paused",
            )
        )
        result = await client.pause(reason="halt")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.PRECONDITION_FAILED

    async def test_default_switch_profile_returns_non_no_op_outcome(self) -> None:
        client = FakePipelineClient()
        out = await client.switch_profile(profile_name="large")
        assert out.result.ok is True
        assert out.outcome is not None
        assert out.outcome.previous_profile == Profile.medium
        assert out.outcome.new_profile == Profile.large
        assert out.outcome.is_no_op is False

    async def test_default_switch_profile_is_no_op_for_same_profile(self) -> None:
        client = FakePipelineClient()
        out = await client.switch_profile(profile_name="medium")
        assert out.result.ok is True
        assert out.outcome is not None
        assert out.outcome.is_no_op is True

    async def test_set_switch_profile_response_used_when_provided(self) -> None:
        from alphamind.command_center._kernel.control import ControlResult

        canned = PipelineSwitchProfileResult(
            result=ControlResult.success(applied_at="2026-05-26T12:34:56Z"),
            outcome=ProfileSwitchOutcome(
                previous_profile=Profile.small,
                new_profile=Profile.large,
                main_yaml_path=Path("config/main.yaml"),
                is_no_op=False,
            ),
        )
        client = FakePipelineClient()
        client.set_switch_profile_response(canned)
        out = await client.switch_profile(profile_name="large")
        assert out is canned

    async def test_default_run_universe_validation(self) -> None:
        client = FakePipelineClient()
        out = await client.run_universe_validation()
        assert out.result.ok is True
        assert out.report is not None
        assert out.report.tickers[0].ticker == "AAPL"

    async def test_set_current_profile_drives_no_op_path(self) -> None:
        # F13: seed the fake's view of state to ``large`` and confirm
        # a switch to ``large`` synthesizes a no-op outcome.
        client = FakePipelineClient()
        client.set_current_profile(Profile.large)
        out = await client.switch_profile(profile_name="large")
        assert out.result.ok is True
        assert out.outcome is not None
        assert out.outcome.previous_profile == Profile.large
        assert out.outcome.new_profile == Profile.large
        assert out.outcome.is_no_op is True

    async def test_set_current_profile_drives_non_default_transition(self) -> None:
        # F13: switch from ``small`` (seeded) to ``large`` without
        # constructing a full PipelineSwitchProfileResult.
        client = FakePipelineClient()
        client.set_current_profile(Profile.small)
        out = await client.switch_profile(profile_name="large")
        assert out.result.ok is True
        assert out.outcome is not None
        assert out.outcome.previous_profile == Profile.small
        assert out.outcome.new_profile == Profile.large
        assert out.outcome.is_no_op is False

    async def test_successful_switch_updates_current_profile(self) -> None:
        # F13: after a real transition, the fake's current_profile
        # reflects the post-switch baseline so a subsequent switch
        # synthesizes its outcome correctly.
        client = FakePipelineClient()
        # Initial: medium.
        out_1 = await client.switch_profile(profile_name="large")
        assert out_1.outcome is not None
        assert out_1.outcome.previous_profile == Profile.medium
        assert out_1.outcome.new_profile == Profile.large
        # Now the fake's view is ``large``; switching to ``large`` is
        # a no-op, switching to ``small`` is a real transition.
        out_2 = await client.switch_profile(profile_name="large")
        assert out_2.outcome is not None
        assert out_2.outcome.is_no_op is True
        out_3 = await client.switch_profile(profile_name="small")
        assert out_3.outcome is not None
        assert out_3.outcome.previous_profile == Profile.large
        assert out_3.outcome.new_profile == Profile.small


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
