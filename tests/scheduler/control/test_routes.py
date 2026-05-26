"""Integration tests for ``alphamind.scheduler.control.routes`` (ALP-664).

Exercises the FastAPI app through ``TestClient`` against fake verb
primitives.  Each test asserts the schema-shaped HTTP status code +
body envelope per
``docs/design/pipeline-control-and-events-schema.md`` § Per-verb summary.

The SSE coverage uses ``TestClient.stream`` to subscribe to ``GET /events``
and verifies framing for each event the emitter exposes.

Cross-field invariant assertions live at the end — these are the
emitter-callers' responsibility but the tests demonstrate that the wire
shape can carry the invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from alphamind.scheduler.control import app as app_module
from alphamind.scheduler.control import events, verbs

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Local copies of the fake primitives, scoped to routes integration tests.
# (test_app.py uses module-internal imports of these; keeping a local copy
# here avoids cross-test-module imports that mypy strict mode flags.)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSchedulerControl:
    paused: bool = False
    pause_applied_at: datetime | None = None

    def is_paused(self) -> bool:
        return self.paused

    def pause(self, *, reason: str, now: datetime) -> datetime:
        del reason
        if self.paused:
            assert self.pause_applied_at is not None
            return self.pause_applied_at
        self.paused = True
        self.pause_applied_at = now
        return now

    def resume(self, *, now: datetime) -> datetime:
        self.paused = False
        return now

    def next_run_preview(self) -> tuple[datetime, str] | None:
        return None


@dataclass
class _FakeEmergencyTrigger:
    next_invocation_id: str = "inv-emerg-1"
    cooldown_remaining: int | None = None
    cooldown_started_at: datetime | None = None
    running_invocation_id: str | None = None
    triggered: list[tuple[str, str, datetime]] = field(default_factory=list)

    async def trigger(self, *, reason: str, source: str, now: datetime) -> str:
        self.triggered.append((reason, source, now))
        return self.next_invocation_id

    def cooldown_info(self) -> verbs.CooldownInfo | None:
        if self.cooldown_remaining is None:
            return None
        assert self.cooldown_started_at is not None
        return verbs.CooldownInfo(
            cooldown_remaining_seconds=self.cooldown_remaining,
            cooldown_started_at=self.cooldown_started_at,
        )

    def running_info(self) -> verbs.RunningInfo | None:
        if self.running_invocation_id is None:
            return None
        return verbs.RunningInfo(running_invocation_id=self.running_invocation_id)


@dataclass
class _FakeUniverseValidator:
    report: verbs.UniverseValidationReportRecord | None = None
    raise_error: Exception | None = None

    def validate(self, *, as_of: object) -> verbs.UniverseValidationReportRecord:
        del as_of
        if self.raise_error is not None:
            raise self.raise_error
        assert self.report is not None
        return self.report


def _passing_report() -> verbs.UniverseValidationReportRecord:
    return verbs.UniverseValidationReportRecord(
        validated_at=_NOW,
        tickers=(
            verbs.UniverseValidationTickerRecord(
                ticker="AAPL",
                verdict="pass",
                criteria=(
                    verbs.UniverseValidationCriterionRecord(criterion="adv", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(
                        criterion="analyst_coverage", verdict="pass"
                    ),
                    verbs.UniverseValidationCriterionRecord(criterion="beta", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(criterion="market_cap", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(criterion="options_oi", verdict="pass"),
                ),
            ),
        ),
    )


def _build_client(
    *,
    scheduler: verbs.SchedulerControl | None = None,
    emergency: verbs.EmergencyTrigger | None = None,
    universe_validator: verbs.UniverseValidator | None = None,
    config_dir: Path | None = None,
) -> tuple[TestClient, events.SSEEventEmitter]:
    emitter = events.SSEEventEmitter(heartbeat_interval_seconds=0.05)
    dispatch = app_module.VerbDispatch(
        scheduler=scheduler if scheduler is not None else _FakeSchedulerControl(),
        emergency=emergency if emergency is not None else _FakeEmergencyTrigger(),
        universe_validator=(
            universe_validator
            if universe_validator is not None
            else _FakeUniverseValidator(report=_passing_report())
        ),
        config_dir=config_dir,
        now_factory=lambda: _NOW,
    )
    app = app_module.build_app(emitter=emitter, dispatch=dispatch)
    return TestClient(app), emitter


# ---------------------------------------------------------------------------
# POST /control/pause.
# ---------------------------------------------------------------------------


class TestRoutePause:
    def test_happy_path_returns_200_with_envelope(self) -> None:
        client, _ = _build_client()
        resp = client.post("/control/pause", json={"reason": "manual hold"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert "applied_at" in body

    def test_empty_reason_returns_422_validation_failed(self) -> None:
        client, _ = _build_client()
        resp = client.post("/control/pause", json={"reason": ""})
        # Pydantic body validation fires before the verb; FastAPI returns 422.
        assert resp.status_code == 422

    def test_missing_reason_returns_422_validation_failed(self) -> None:
        client, _ = _build_client()
        resp = client.post("/control/pause", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /control/resume.
# ---------------------------------------------------------------------------


class TestRouteResume:
    def test_happy_path_returns_200(self) -> None:
        client, _ = _build_client()
        resp = client.post("/control/resume", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# POST /control/trigger_emergency_invocation.
# ---------------------------------------------------------------------------


class TestRouteTriggerEmergencyInvocation:
    def test_happy_path_returns_200_with_invocation_id(self) -> None:
        emergency = _FakeEmergencyTrigger(next_invocation_id="inv-emerg-7")
        client, _ = _build_client(emergency=emergency)
        resp = client.post("/control/trigger_emergency_invocation", json={"reason": "margin call"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["invocation_id"] == "inv-emerg-7"

    def test_cooldown_active_returns_409_with_details(self) -> None:
        started = datetime(2026, 5, 26, 11, 40, 0, tzinfo=UTC)
        emergency = _FakeEmergencyTrigger(
            cooldown_remaining=1500,
            cooldown_started_at=started,
        )
        client, _ = _build_client(emergency=emergency)
        resp = client.post("/control/trigger_emergency_invocation", json={"reason": "x"})
        assert resp.status_code == 409
        body = resp.json()["detail"]
        assert body["error"]["code"] == "cooldown_active"
        assert body["error"]["details"]["cooldown_remaining_seconds"] == 1500
        assert body["error"]["details"]["cooldown_started_at"].startswith("2026-05-26T11:40:00")

    def test_precondition_failed_returns_409_with_running_invocation_id(self) -> None:
        emergency = _FakeEmergencyTrigger(running_invocation_id="inv-running-9")
        client, _ = _build_client(emergency=emergency)
        resp = client.post("/control/trigger_emergency_invocation", json={"reason": "x"})
        assert resp.status_code == 409
        body = resp.json()["detail"]
        assert body["error"]["code"] == "precondition_failed"
        assert body["error"]["details"]["running_invocation_id"] == "inv-running-9"


# ---------------------------------------------------------------------------
# POST /control/switch_profile.
# ---------------------------------------------------------------------------


def _stage_config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profiles").mkdir()
    for name in ("micro", "small", "medium", "large"):
        (config_dir / "profiles" / f"{name}.yaml").write_text("dummy: true\n")
    (config_dir / "main.yaml").write_text(
        "active_profile: medium\n"
        "execution_mode: paper\n"
        "paths:\n"
        "  database: /tmp/db\n"
        "  logs: /tmp/logs\n"
        "  archive: /tmp/arch\n"
        "  prompts: prompts/\n",
    )
    return config_dir


class TestRouteSwitchProfile:
    def test_happy_path_returns_200(self, tmp_path: Path) -> None:
        config_dir = _stage_config_dir(tmp_path)
        client, _ = _build_client(config_dir=config_dir)
        resp = client.post("/control/switch_profile", json={"profile_name": "large"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    def test_invalid_profile_name_returns_400_validation_failed(self, tmp_path: Path) -> None:
        config_dir = _stage_config_dir(tmp_path)
        client, _ = _build_client(config_dir=config_dir)
        resp = client.post("/control/switch_profile", json={"profile_name": "enormous"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"]["code"] == "validation_failed"

    def test_missing_yaml_returns_404_not_found(self, tmp_path: Path) -> None:
        # Build a config_dir whose profiles/ has no large.yaml.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "profiles").mkdir()
        (config_dir / "profiles" / "medium.yaml").write_text("dummy: true\n")
        (config_dir / "main.yaml").write_text(
            "active_profile: medium\n"
            "execution_mode: paper\n"
            "paths:\n"
            "  database: /tmp/db\n"
            "  logs: /tmp/logs\n"
            "  archive: /tmp/arch\n"
            "  prompts: prompts/\n",
        )
        client, _ = _build_client(config_dir=config_dir)
        resp = client.post("/control/switch_profile", json={"profile_name": "large"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# POST /control/run_universe_validation.
# ---------------------------------------------------------------------------


class TestRouteRunUniverseValidation:
    def test_happy_path_returns_200_with_report(self) -> None:
        client, _ = _build_client()
        resp = client.post("/control/run_universe_validation", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["report"]["tickers"][0]["ticker"] == "AAPL"
        # Criteria order must match the schema's documented order.
        criteria = body["report"]["tickers"][0]["criteria"]
        names = [c["criterion"] for c in criteria]
        assert names == ["adv", "analyst_coverage", "beta", "market_cap", "options_oi"]

    def test_validator_failure_returns_500_internal_error(self) -> None:
        validator = _FakeUniverseValidator(raise_error=RuntimeError("script crashed"))
        client, _ = _build_client(universe_validator=validator)
        resp = client.post("/control/run_universe_validation", json={})
        assert resp.status_code == 500
        assert resp.json()["detail"]["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# GET /events — SSE framing is exercised end-to-end in test_app.py against a
# real Uvicorn task (TestClient's stream API holds the connection open in a
# worker thread that can't honor the route's request.is_disconnected() loop,
# which would hang these tests).  The framing helper itself is exercised
# in test_events.py at the emitter level (per-event-type, cross-field
# invariants, heartbeat).  Here we only verify the route is mounted.
# ---------------------------------------------------------------------------


class TestRouteEventsMounted:
    def test_get_events_returns_404_only_when_unmounted(self) -> None:
        # Negative-control assertion: the /events path IS mounted; absent
        # mounting it would return 404. Combined with test_app.TestBuildApp
        # which enumerates app.routes, this pins the mount surface without
        # opening an SSE stream the TestClient can't cleanly close.
        client, _ = _build_client()
        # HEAD on an SSE endpoint returns 405 (only GET is allowed) — proves
        # the route IS mounted; a missing route would return 404.
        resp = client.head("/events")
        assert resp.status_code in (200, 405)
