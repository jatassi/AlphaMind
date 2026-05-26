"""Tests for ``alphamind.scheduler.control.verbs`` (ALP-664).

The five verbs are pure-ish functions over injected Protocols.  Tests
fake each Protocol in-memory; production wiring constructs the concrete
adapters in ``app.py``'s lifespan.

Coverage per the schema's per-verb error table:

* pause / resume — happy path + idempotent no-op (already paused / already running).
* trigger_emergency_invocation — happy path + cooldown_active + precondition_failed (running).
* switch_profile — happy path + not_found.
* run_universe_validation — happy path + internal_error (validator failure).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from alphamind.config.control_handlers.profile_switch import ProfileNotFoundError
from alphamind.config.models.main import Profile
from alphamind.scheduler.control import verbs

# ---------------------------------------------------------------------------
# Fake primitives — in-memory implementations of the verbs.py Protocols.
# ---------------------------------------------------------------------------


@dataclass
class FakeSchedulerControl:
    paused: bool = False
    pause_applied_at: datetime | None = None
    resume_applied_at: datetime | None = None
    next_run_preview_value: tuple[datetime, str] | None = None

    def is_paused(self) -> bool:
        return self.paused

    def pause(self, *, reason: str, now: datetime) -> datetime:
        # `reason` is logged at the caller boundary (route handler writes
        # it onto the activity-log entry on the proxy side); not used here.
        del reason
        if self.paused:
            assert self.pause_applied_at is not None
            return self.pause_applied_at
        self.paused = True
        self.pause_applied_at = now
        return now

    def resume(self, *, now: datetime) -> datetime:
        self.paused = False
        self.resume_applied_at = now
        return now

    def next_run_preview(self) -> tuple[datetime, str] | None:
        return self.next_run_preview_value


@dataclass
class FakeEmergencyTrigger:
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
class FakeUniverseValidator:
    report: verbs.UniverseValidationReportRecord | None = None
    raise_error: Exception | None = None

    def validate(self, *, as_of: date) -> verbs.UniverseValidationReportRecord:
        del as_of  # validator backend uses as_of; tests don't care.
        if self.raise_error is not None:
            raise self.raise_error
        assert self.report is not None
        return self.report


_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestPause:
    def test_pause_sets_flag_and_returns_applied_at(self) -> None:
        sched = FakeSchedulerControl()
        result = verbs.pause(scheduler=sched, reason="manual hold", now=_NOW)
        assert sched.paused is True
        assert result.applied_at == _NOW

    def test_pause_on_already_paused_returns_original_applied_at(self) -> None:
        """Idempotent — schema: 'pause on already-paused returns accepted with the
        original applied_at unchanged'."""
        sched = FakeSchedulerControl()
        first_now = datetime(2026, 5, 26, 10, 0, 0, tzinfo=UTC)
        second_now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
        result1 = verbs.pause(scheduler=sched, reason="r1", now=first_now)
        result2 = verbs.pause(scheduler=sched, reason="r2", now=second_now)
        assert result1.applied_at == first_now
        assert result2.applied_at == first_now  # unchanged


class TestResume:
    def test_resume_clears_flag(self) -> None:
        sched = FakeSchedulerControl(paused=True, pause_applied_at=_NOW)
        result = verbs.resume(scheduler=sched, now=_NOW)
        assert sched.paused is False
        assert result.applied_at == _NOW

    def test_resume_on_already_running_is_idempotent(self) -> None:
        sched = FakeSchedulerControl(paused=False)
        result = verbs.resume(scheduler=sched, now=_NOW)
        assert sched.paused is False
        assert result.applied_at == _NOW


# ---------------------------------------------------------------------------
# trigger_emergency_invocation
# ---------------------------------------------------------------------------


class TestTriggerEmergencyInvocation:
    async def test_happy_path_returns_invocation_id(self) -> None:
        trigger = FakeEmergencyTrigger(next_invocation_id="inv-emerg-42")
        result = await verbs.trigger_emergency_invocation(
            emergency=trigger,
            reason="margin call",
            now=_NOW,
        )
        assert result.invocation_id == "inv-emerg-42"
        assert result.applied_at == _NOW
        assert trigger.triggered == [("margin call", "operator_console", _NOW)]

    async def test_cooldown_active_raises_with_details(self) -> None:
        started = datetime(2026, 5, 26, 11, 40, 0, tzinfo=UTC)
        trigger = FakeEmergencyTrigger(
            cooldown_remaining=1200,
            cooldown_started_at=started,
        )
        with pytest.raises(verbs.CooldownActiveError) as exc_info:
            await verbs.trigger_emergency_invocation(
                emergency=trigger,
                reason="r",
                now=_NOW,
            )
        assert exc_info.value.cooldown_remaining_seconds == 1200
        assert exc_info.value.cooldown_started_at == started
        # Trigger was not invoked.
        assert trigger.triggered == []

    async def test_running_invocation_raises_precondition_failed(self) -> None:
        trigger = FakeEmergencyTrigger(running_invocation_id="inv-running-99")
        with pytest.raises(verbs.PreconditionFailedError) as exc_info:
            await verbs.trigger_emergency_invocation(
                emergency=trigger,
                reason="r",
                now=_NOW,
            )
        assert exc_info.value.running_invocation_id == "inv-running-99"


# ---------------------------------------------------------------------------
# switch_profile
# ---------------------------------------------------------------------------


class TestSwitchProfile:
    def test_happy_path_dispatches_to_handler(self, tmp_path: Path) -> None:
        # Real handler is invoked; build a minimal config directory.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "profiles").mkdir()
        (config_dir / "profiles" / "medium.yaml").write_text("dummy: true\n")
        (config_dir / "profiles" / "large.yaml").write_text("dummy: true\n")
        (config_dir / "main.yaml").write_text(
            "active_profile: medium\n"
            "execution_mode: paper\n"
            "paths:\n"
            "  database: /tmp/db\n"
            "  logs: /tmp/logs\n"
            "  archive: /tmp/arch\n"
            "  prompts: prompts/\n",
        )
        result = verbs.switch_profile(
            profile_name="large",
            config_dir=config_dir,
            now=_NOW,
        )
        assert result.applied_at == _NOW
        # The verb exposes the handler's outcome so the route layer can emit
        # the PROFILE_SWITCHED activity-log entry without re-deriving the
        # previous / new profiles (story 04a integration point).
        assert result.outcome.previous_profile == Profile.medium
        assert result.outcome.new_profile == Profile.large
        assert result.outcome.is_no_op is False
        # main.yaml mutation occurred.
        new_text = (config_dir / "main.yaml").read_text()
        assert "active_profile: large" in new_text

    def test_no_op_path_exposes_outcome(self, tmp_path: Path) -> None:
        """A same-profile request still returns an outcome flagged ``is_no_op``."""
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
        result = verbs.switch_profile(
            profile_name="medium",
            config_dir=config_dir,
            now=_NOW,
        )
        assert result.applied_at == _NOW
        assert result.outcome.is_no_op is True
        assert result.outcome.previous_profile == Profile.medium
        assert result.outcome.new_profile == Profile.medium

    def test_unknown_profile_value_raises_validation(self, tmp_path: Path) -> None:
        # Profile names outside the Profile StrEnum are rejected before
        # reaching the file-existence check.
        with pytest.raises(verbs.ValidationFailedError):
            verbs.switch_profile(
                profile_name="enormous",
                config_dir=tmp_path,
                now=_NOW,
            )

    def test_profile_yaml_missing_raises_not_found(self, tmp_path: Path) -> None:
        # Valid Profile enum value, but no shipped YAML — handler raises
        # ProfileNotFoundError; verb maps to ProfileNotFoundError surface.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "profiles").mkdir()  # empty
        (config_dir / "main.yaml").write_text(
            "active_profile: medium\n"
            "execution_mode: paper\n"
            "paths:\n"
            "  database: /tmp/db\n"
            "  logs: /tmp/logs\n"
            "  archive: /tmp/arch\n"
            "  prompts: prompts/\n",
        )
        with pytest.raises(ProfileNotFoundError):
            verbs.switch_profile(
                profile_name="large",
                config_dir=config_dir,
                now=_NOW,
            )


# ---------------------------------------------------------------------------
# run_universe_validation
# ---------------------------------------------------------------------------


class TestRunUniverseValidation:
    def test_happy_path_returns_report(self) -> None:
        report = verbs.UniverseValidationReportRecord(
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
                        verbs.UniverseValidationCriterionRecord(
                            criterion="market_cap", verdict="pass"
                        ),
                        verbs.UniverseValidationCriterionRecord(
                            criterion="options_oi", verdict="pass"
                        ),
                    ),
                ),
            ),
        )
        validator = FakeUniverseValidator(report=report)
        result = verbs.run_universe_validation(
            validator=validator,
            now=_NOW,
            as_of=_NOW.date(),
        )
        assert result.report.tickers[0].ticker == "AAPL"
        assert result.applied_at == _NOW

    def test_validator_failure_raises_internal_error(self) -> None:
        validator = FakeUniverseValidator(raise_error=RuntimeError("script crashed"))
        with pytest.raises(verbs.UniverseValidationFailedError):
            verbs.run_universe_validation(
                validator=validator,
                now=_NOW,
                as_of=_NOW.date(),
            )


# ---------------------------------------------------------------------------
# Profile enum coverage — independent of switch_profile to keep ValidationFailedError
# pinned even if Profile gains a new member.
# ---------------------------------------------------------------------------


class TestProfileNameValidation:
    @pytest.mark.parametrize("name", [p.value for p in Profile])
    def test_every_documented_profile_name_passes_validation(
        self, name: str, tmp_path: Path
    ) -> None:
        # Build minimal main.yaml + matching profile YAML.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "profiles").mkdir()
        for p in Profile:
            (config_dir / "profiles" / f"{p.value}.yaml").write_text("dummy: true\n")
        # Match each value's own pre-existing main.yaml so the no-op path is hit
        # when the requested profile equals the previous.
        (config_dir / "main.yaml").write_text(
            f"active_profile: {name}\n"
            "execution_mode: paper\n"
            "paths:\n"
            "  database: /tmp/db\n"
            "  logs: /tmp/logs\n"
            "  archive: /tmp/arch\n"
            "  prompts: prompts/\n",
        )
        result = verbs.switch_profile(profile_name=name, config_dir=config_dir, now=_NOW)
        assert result.applied_at == _NOW
