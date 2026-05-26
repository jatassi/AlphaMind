"""Control-surface vocabulary for the command-center proxy (story 02 / ALP-666).

Three primitives downstream stories consume:

* :class:`ControlVerb` — the eight operator-action verbs the FastAPI
  ``/api/control/*`` surface exposes (five pipeline + three monitor).
  Mirrors the verb tables in :doc:`docs/design/command-center.md` § Control
  surface and the per-verb summaries in the pipeline / monitor schema docs.
* :class:`ControlErrorCode` — the union of error-envelope ``error.code``
  values both schemas emit, as a StrEnum. The proxy maps upstream error
  envelopes onto these before re-emitting to the browser; the alert-engine
  ack/snooze surface (story 05a) reuses the validation codes.
* :class:`ControlResult` — the frozen-dataclass return value the verb
  dispatch path emits. Either carries ``applied_at`` (success) or
  ``error_code`` + ``error_detail`` (failure); never both.

The vocabularies are pinned at story 02 so stories 04a / 05a can name
them without re-deriving the catalog. Adding a verb later requires
updating the StrEnum here + the route table; adding an error code
requires updating both the StrEnum and the schema-docs error tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ControlErrorCode", "ControlResult", "ControlVerb"]


class ControlVerb(StrEnum):
    """The operator-action verbs the command center proxies + emits.

    Five pipeline verbs (``pause`` / ``resume`` /
    ``trigger_emergency_invocation`` / ``switch_profile`` /
    ``run_universe_validation``) and three monitor verbs (``cancel_order``
    / ``force_close_position`` / ``set_halt_mode``) plus two alert-
    surface verbs (``acknowledge_alert`` / ``snooze_alert``) from
    story 05a (ALP-671). The StrEnum value is the snake-case verb name —
    identical to the FastAPI route path segment and the activity-log
    audit row's ``verb`` field.
    """

    # Pipeline verbs (docs/design/pipeline-control-and-events-schema.md
    # § Per-verb summary).
    PAUSE = "pause"
    RESUME = "resume"
    TRIGGER_EMERGENCY_INVOCATION = "trigger_emergency_invocation"
    SWITCH_PROFILE = "switch_profile"
    RUN_UNIVERSE_VALIDATION = "run_universe_validation"

    # Monitor verbs (docs/design/monitor-control-and-events-schema.md
    # § Per-verb summary).
    CANCEL_ORDER = "cancel_order"
    FORCE_CLOSE_POSITION = "force_close_position"
    SET_HALT_MODE = "set_halt_mode"

    # Alert verbs (docs/design/command-center.md § Alerting — Acknowledge
    # and snooze). The alert-engine surface (story 05a / ALP-671) wraps
    # these in operator_invocation() so the activity-log explorer's
    # "Operator actions" saved filter view surfaces them alongside the
    # pipeline / monitor verbs.
    ACKNOWLEDGE_ALERT = "acknowledge_alert"
    SNOOZE_ALERT = "snooze_alert"


class ControlErrorCode(StrEnum):
    """Error-envelope ``error.code`` vocabulary the proxy emits to the browser.

    Mirrors the union of error codes the pipeline and monitor schemas list
    across all eight verbs. Each value is the snake-case string the
    schemas' ``error.code`` field carries; the proxy maps upstream error
    envelopes onto these before re-emitting downstream.
    """

    # 400-class
    VALIDATION_FAILED = "validation_failed"
    # 404
    NOT_FOUND = "not_found"
    # 409
    PRECONDITION_FAILED = "precondition_failed"
    COOLDOWN_ACTIVE = "cooldown_active"
    # 502 (monitor cancel_order / force_close_position only)
    BROKER_ERROR = "broker_error"
    # 500
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Frozen result of one verb dispatch.

    Either a success carrying the upstream's ``applied_at`` timestamp, or
    a failure carrying the error envelope. Constructed via the
    :meth:`success` / :meth:`failure` factories so callers never assemble
    a half-shaped instance (e.g. ``ok=True`` with a non-None
    ``error_code``).
    """

    ok: bool
    applied_at: str | None = None
    error_code: ControlErrorCode | None = None
    error_detail: str | None = None

    @classmethod
    def success(cls, *, applied_at: str) -> ControlResult:
        """Construct a successful result with the upstream's ``applied_at``."""
        return cls(ok=True, applied_at=applied_at)

    @classmethod
    def failure(cls, *, error_code: ControlErrorCode, error_detail: str) -> ControlResult:
        """Construct a failure result with the upstream's error envelope."""
        return cls(ok=False, error_code=error_code, error_detail=error_detail)
