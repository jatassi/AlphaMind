"""Tests for ``command_center._kernel.control`` (story 02 / ALP-666).

Covers the StrEnum vocabularies (``ControlVerb`` / ``ControlErrorCode``) and
the ``ControlResult`` frozen dataclass that the future control proxy (story
04a) consumes when dispatching the eight operator verbs.

The control vocabularies are pinned to the schema docs:

* Pipeline verbs: ``pipeline-control-and-events-schema.md`` § Per-verb summary
  — pause / resume / trigger_emergency_invocation / switch_profile /
  run_universe_validation.
* Monitor verbs: ``monitor-control-and-events-schema.md`` § Per-verb summary
  — cancel_order / force_close_position / set_halt_mode.
* Error codes: the union of both schemas' error-envelope ``error.code``
  values plus the alert-engine ack/snooze surface (story 05a).
"""

from __future__ import annotations

import pytest

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
    ControlVerb,
)


class TestControlVerb:
    @pytest.mark.parametrize(
        "verb",
        [
            ControlVerb.PAUSE,
            ControlVerb.RESUME,
            ControlVerb.TRIGGER_EMERGENCY_INVOCATION,
            ControlVerb.SWITCH_PROFILE,
            ControlVerb.RUN_UNIVERSE_VALIDATION,
            ControlVerb.CANCEL_ORDER,
            ControlVerb.FORCE_CLOSE_POSITION,
            ControlVerb.SET_HALT_MODE,
        ],
    )
    def test_each_documented_verb_is_a_member(self, verb: ControlVerb) -> None:
        # Every verb the parent issue's control surface tables list must be
        # present; later stories (04a) consume these by name when wiring routes.
        assert verb.value
        assert isinstance(verb.value, str)

    def test_verb_value_matches_lowercase_member_name(self) -> None:
        # The StrEnum value is the snake-case verb name — the same string the
        # FastAPI route path uses (``POST /api/control/pause``) and the
        # activity-log audit row's ``verb`` field carries.
        assert ControlVerb.PAUSE.value == "pause"
        assert ControlVerb.TRIGGER_EMERGENCY_INVOCATION.value == "trigger_emergency_invocation"
        assert ControlVerb.SET_HALT_MODE.value == "set_halt_mode"

    def test_str_returns_value(self) -> None:
        # StrEnum semantics: ``str(member)`` == ``member.value``. Useful so the
        # verb can be interpolated into log / activity-log messages without an
        # explicit ``.value`` access.
        assert str(ControlVerb.PAUSE) == "pause"


class TestControlErrorCode:
    @pytest.mark.parametrize(
        "code",
        [
            ControlErrorCode.VALIDATION_FAILED,
            ControlErrorCode.PRECONDITION_FAILED,
            ControlErrorCode.NOT_FOUND,
            ControlErrorCode.COOLDOWN_ACTIVE,
            ControlErrorCode.BROKER_ERROR,
            ControlErrorCode.INTERNAL_ERROR,
        ],
    )
    def test_each_documented_error_code_is_a_member(self, code: ControlErrorCode) -> None:
        # The control schemas enumerate these six error codes across the eight
        # verbs; the proxy (story 04a) maps upstream error envelopes to these
        # before re-emitting to the browser. Each one must be on the StrEnum.
        assert code.value
        assert isinstance(code.value, str)

    def test_error_code_values_match_snake_case(self) -> None:
        # The wire-level ``error.code`` field is snake_case per the schemas.
        assert ControlErrorCode.VALIDATION_FAILED.value == "validation_failed"
        assert ControlErrorCode.COOLDOWN_ACTIVE.value == "cooldown_active"


class TestControlResult:
    def test_success_result_carries_envelope_fields(self) -> None:
        # ``success`` carries the ``status`` + ``applied_at`` envelope the
        # pipeline / monitor return on 2xx; the proxy threads this through.
        result = ControlResult.success(applied_at="2026-05-26T00:00:00Z")
        assert result.ok is True
        assert result.applied_at == "2026-05-26T00:00:00Z"
        assert result.error_code is None
        assert result.error_detail is None

    def test_failure_result_carries_error_envelope(self) -> None:
        result = ControlResult.failure(
            error_code=ControlErrorCode.PRECONDITION_FAILED,
            error_detail="order already filled",
        )
        assert result.ok is False
        assert result.error_code is ControlErrorCode.PRECONDITION_FAILED
        assert result.error_detail == "order already filled"
        assert result.applied_at is None

    def test_result_is_frozen(self) -> None:
        result = ControlResult.success(applied_at="2026-05-26T00:00:00Z")
        # Frozen dataclasses raise FrozenInstanceError on assignment.
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            result.ok = False  # type: ignore[misc]
