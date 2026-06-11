"""Synchronous verb implementations backing ``POST /control/*`` (ALP-664).

Each verb is a pure-ish function over injected Protocols
(:class:`SchedulerControl`, :class:`EmergencyTrigger`,
:class:`UniverseValidator`) plus the existing
``config.control_handlers.profile_switch`` direct call.  Routes consume
the result records (frozen dataclasses) and convert them back into
Pydantic envelopes at the boundary.

Per the ALP-128 architectural invariant (Pydantic at boundaries only),
NO Pydantic models cross into this module — the only models present are
typed result records and typed error signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol

from alphamind.config.control_handlers.profile_switch import (
    ProfileNotFoundError,
    ProfileSwitchOutcome,
    switch_active_profile,
)
from alphamind.config.models.main import Profile

__all__ = [
    "ControlResult",
    "CooldownActiveError",
    "CooldownInfo",
    "EmergencyTrigger",
    "PreconditionFailedError",
    "ProfileNotFoundError",
    "ProfileSwitchOutcome",
    "RunUniverseValidationResult",
    "RunningInfo",
    "SchedulerControl",
    "SwitchProfileResult",
    "TriggerEmergencyInvocationResult",
    "UniverseValidationCriterionRecord",
    "UniverseValidationFailedError",
    "UniverseValidationReportRecord",
    "UniverseValidationTickerRecord",
    "UniverseValidator",
    "ValidationFailedError",
    "VerbError",
    "pause",
    "resume",
    "run_universe_validation",
    "switch_profile",
    "trigger_emergency_invocation",
]


# ---------------------------------------------------------------------------
# Protocols — the three injected primitives.
# ---------------------------------------------------------------------------


class SchedulerControl(Protocol):
    """Adapter over the APScheduler driver's pause/resume + next-run preview.

    Concrete implementation lives next to :mod:`alphamind.scheduler.driver`.
    Tests fake the Protocol directly.
    """

    def is_paused(self) -> bool: ...

    def pause(self, *, reason: str, now: datetime) -> datetime:
        """Set the scheduler-pause flag; return the ``applied_at`` timestamp.

        Idempotent: a pause-while-paused returns the ORIGINAL applied_at,
        not ``now`` — per the schema's "idempotent verbs return accepted
        on no-op application as well".
        """
        ...

    def resume(self, *, now: datetime) -> datetime:
        """Clear the scheduler-pause flag; return the ``applied_at`` timestamp."""
        ...

    def next_run_preview(self) -> tuple[datetime, str] | None:
        """Return ``(next_trigger_at, next_trigger_type)`` or ``None`` if no jobs scheduled."""
        ...


@dataclass(frozen=True, slots=True)
class CooldownInfo:
    """Cooldown-status payload returned by ``EmergencyTrigger.cooldown_info``."""

    cooldown_remaining_seconds: int
    cooldown_started_at: datetime


@dataclass(frozen=True, slots=True)
class RunningInfo:
    """Running-invocation payload returned by ``EmergencyTrigger.running_info``."""

    running_invocation_id: str


class EmergencyTrigger(Protocol):
    """Adapter over the emergency-invocation trigger path.

    Concrete implementation routes through
    :mod:`alphamind.scheduler.emergency` (the existing emergency-receiver
    primitive); on the verb's synchronous request path we want a direct
    dispatch rather than the activity-log poll.  The concrete adapter
    lives next to the receiver.
    """

    async def trigger(self, *, reason: str, source: str, now: datetime) -> str:
        """Start an emergency invocation; return the assigned ``invocation_id``."""
        ...

    def cooldown_info(self) -> CooldownInfo | None:
        """Return cooldown status when the cooldown window is active, else ``None``."""
        ...

    def running_info(self) -> RunningInfo | None:
        """Return running-invocation info when one is already in flight, else ``None``."""
        ...


@dataclass(frozen=True, slots=True)
class UniverseValidationCriterionRecord:
    """One criterion's verdict for a single ticker.

    Schema-shaped frozen-dataclass mirror of
    :class:`alphamind.scheduler.control.models.UniverseValidationCriterionRow`.
    """

    criterion: Literal["adv", "analyst_coverage", "beta", "market_cap", "options_oi"]
    verdict: Literal["pass", "fail", "unknown"]
    computed_value: float | dict[str, float] | None = None
    threshold: float | dict[str, float] | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseValidationTickerRecord:
    """One ticker's aggregate verdict + per-criterion rows."""

    ticker: str
    verdict: Literal["pass", "fail", "unknown"]
    criteria: tuple[UniverseValidationCriterionRecord, ...]


@dataclass(frozen=True, slots=True)
class UniverseValidationReportRecord:
    """Schema-shaped mirror of the universe-validation inline report."""

    validated_at: datetime
    tickers: tuple[UniverseValidationTickerRecord, ...]


class UniverseValidator(Protocol):
    """Adapter over ``scripts/ops/validate_universe.py``'s entrypoint.

    The concrete adapter calls the script's per-ticker validator and
    shapes results into :class:`UniverseValidationReportRecord`.  Tests
    fake the Protocol directly so the verb's outcome surface is
    exercised without hitting Polygon / Finnhub.
    """

    def validate(self, *, as_of: date) -> UniverseValidationReportRecord: ...


# ---------------------------------------------------------------------------
# Result records — frozen-dataclass mirrors of Pydantic response envelopes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Result of a verb that returns the bare envelope (pause, resume, switch_profile)."""

    applied_at: datetime


@dataclass(frozen=True, slots=True)
class TriggerEmergencyInvocationResult:
    """Result of ``trigger_emergency_invocation`` — envelope + invocation_id."""

    applied_at: datetime
    invocation_id: str


@dataclass(frozen=True, slots=True)
class RunUniverseValidationResult:
    """Result of ``run_universe_validation`` — envelope + report."""

    applied_at: datetime
    report: UniverseValidationReportRecord


@dataclass(frozen=True, slots=True)
class SwitchProfileResult:
    """Result of ``switch_profile`` — envelope ``applied_at`` plus the
    underlying :class:`ProfileSwitchOutcome` so the route layer can emit the
    ``PROFILE_SWITCHED`` activity-log entry (story 04a) without re-deriving
    the previous / new profiles.
    """

    applied_at: datetime
    outcome: ProfileSwitchOutcome


# ---------------------------------------------------------------------------
# Typed error signals — the route layer maps each to a ControlErrorEnvelope.
# ---------------------------------------------------------------------------


class VerbError(Exception):
    """Base for typed verb errors; routes.py maps to ControlErrorEnvelope."""


class CooldownActiveError(VerbError):
    """``cooldown_active`` (HTTP 409) — emergency-invocation cooldown is active."""

    def __init__(
        self,
        *,
        cooldown_remaining_seconds: int,
        cooldown_started_at: datetime,
    ) -> None:
        super().__init__(
            f"Emergency-invocation cooldown active; {cooldown_remaining_seconds}s remaining"
        )
        self.cooldown_remaining_seconds = cooldown_remaining_seconds
        self.cooldown_started_at = cooldown_started_at


class PreconditionFailedError(VerbError):
    """``precondition_failed`` (HTTP 409) — an invocation is already running."""

    def __init__(self, *, running_invocation_id: str) -> None:
        super().__init__(
            f"Invocation {running_invocation_id!r} already running; max_instances=1 blocks new"
        )
        self.running_invocation_id = running_invocation_id


class ValidationFailedError(VerbError):
    """``validation_failed`` (HTTP 400) — the request body failed verb-internal validation.

    Pydantic validation handles inbound body shape; this error class
    fires for shape-valid bodies that fail a verb-internal check
    (e.g. ``profile_name`` not a member of the ``Profile`` enum).
    """


class UniverseValidationFailedError(VerbError):
    """``internal_error`` (HTTP 500) — universe validator raised or exited non-zero."""

    def __init__(self, *, cause: Exception) -> None:
        super().__init__(f"Universe validation failed: {cause}")
        self.cause = cause


# ---------------------------------------------------------------------------
# Verbs.
# ---------------------------------------------------------------------------


def pause(*, scheduler: SchedulerControl, reason: str, now: datetime) -> ControlResult:
    """``POST /control/pause`` — set the scheduler-pause flag.

    Idempotent: a pause-while-paused returns the ORIGINAL ``applied_at``
    timestamp per the schema.  ``reason`` is logged at the caller boundary
    (route layer writes it onto the activity-log entry on the proxy side).
    """
    applied_at = scheduler.pause(reason=reason, now=now)
    return ControlResult(applied_at=applied_at)


def resume(*, scheduler: SchedulerControl, now: datetime) -> ControlResult:
    """``POST /control/resume`` — clear the scheduler-pause flag.

    Idempotent: a resume-while-running returns ``accepted``.
    """
    applied_at = scheduler.resume(now=now)
    return ControlResult(applied_at=applied_at)


async def trigger_emergency_invocation(
    *,
    emergency: EmergencyTrigger,
    reason: str,
    now: datetime,
) -> TriggerEmergencyInvocationResult:
    """``POST /control/trigger_emergency_invocation`` — fire out-of-schedule invocation.

    Pre-flight checks:

    * If the cooldown is active, raise :class:`CooldownActiveError`
      (HTTP 409, code=``cooldown_active``).
    * If an invocation is already running, raise
      :class:`PreconditionFailedError` (HTTP 409,
      code=``precondition_failed``).

    Both checks happen before the underlying trigger is invoked so the
    verb is a no-op on the rejection path.
    """
    cooldown = emergency.cooldown_info()
    if cooldown is not None:
        raise CooldownActiveError(
            cooldown_remaining_seconds=cooldown.cooldown_remaining_seconds,
            cooldown_started_at=cooldown.cooldown_started_at,
        )
    running = emergency.running_info()
    if running is not None:
        raise PreconditionFailedError(running_invocation_id=running.running_invocation_id)
    invocation_id = await emergency.trigger(
        reason=reason,
        source="operator_console",
        now=now,
    )
    return TriggerEmergencyInvocationResult(
        applied_at=now,
        invocation_id=invocation_id,
    )


def switch_profile(*, profile_name: str, config_dir: Path, now: datetime) -> SwitchProfileResult:
    """``POST /control/switch_profile`` — rewrite ``main.yaml``'s ``active_profile``.

    Steps:

    1. Coerce ``profile_name`` into the :class:`Profile` enum; a value
       outside the closed set raises :class:`ValidationFailedError`.
    2. Delegate to
       :func:`alphamind.config.control_handlers.profile_switch.switch_active_profile`
       which raises :class:`ProfileNotFoundError` when the YAML file is
       absent.  The route layer maps :class:`ProfileNotFoundError` to
       the ``not_found`` (HTTP 404) envelope.

    Returns :class:`SwitchProfileResult` — the envelope ``applied_at``
    plus the underlying :class:`ProfileSwitchOutcome`.  The outcome is
    needed by story 04a's route handler to emit the ``PROFILE_SWITCHED``
    activity-log entry without re-deriving the previous / new profiles.

    The ``now`` timestamp is the verb's ``applied_at`` value — the
    handler's no-op (idempotent same-profile) path returns ``accepted``
    with the request's ``now`` as well so the response is uniform.
    """
    try:
        profile = Profile(profile_name)
    except ValueError as exc:
        raise ValidationFailedError(
            f"profile_name {profile_name!r} is not a member of the Profile enum"
        ) from exc
    outcome = switch_active_profile(new_profile=profile, config_dir=config_dir)
    return SwitchProfileResult(applied_at=now, outcome=outcome)


def run_universe_validation(
    *,
    validator: UniverseValidator,
    now: datetime,
    as_of: date,
) -> RunUniverseValidationResult:
    """``POST /control/run_universe_validation`` — synchronous validation run.

    The validator is the injected adapter over ``scripts/ops/validate_universe.py``.
    Any exception from the validator is wrapped in
    :class:`UniverseValidationFailedError` so the route layer can map to
    the ``internal_error`` (HTTP 500) envelope without leaking the
    underlying error class.
    """
    try:
        report = validator.validate(as_of=as_of)
    except Exception as exc:
        raise UniverseValidationFailedError(cause=exc) from exc
    return RunUniverseValidationResult(applied_at=now, report=report)
