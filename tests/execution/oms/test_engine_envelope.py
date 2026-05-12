"""Tests for engine envelope Pydantic models — ALP-372 / story 02a.

Verifies the canonical Pydantic translation of
``docs/design/05-execution-layer/engine-envelope-schema.md``:

* every model is frozen
* the happy path constructs a valid envelope and round-trips via ``TypeAdapter``
* every per-design ``model_validator`` raises ``ValidationError`` on the
  negative cases enumerated in the story's acceptance criteria
* :func:`engine_envelope_schema` returns a JSON Schema dict whose ``commands``
  property has ``minItems: 1, maxItems: 1`` and whose ``source_provenance``
  property is ``const: "engine_guardrail"``
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

# Import portfolio_manager.models first to break the latent cycle between
# alphamind.execution.oms (engine-stub MCP) and alphamind.decision.portfolio_manager.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind.commands.command_models import CloseCommand
from alphamind.commands.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
    SecondaryBreachCheckResult,
    engine_envelope_schema,
)

# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


_TRIGGER_TS = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)


def _close_command(
    *,
    close_rationale_type: str = "risk_management",
    risk_management_subtype: str | None = "engine_guardrail",
) -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id="pos-1",
        quantity="all",
        order_type="market",
        close_rationale_type=close_rationale_type,  # type: ignore[arg-type]
        risk_management_subtype=risk_management_subtype,  # type: ignore[arg-type]
    )


def _breach_details() -> BreachDetails:
    return BreachDetails(
        current_value=12_500.0,
        limit_value=10_000.0,
        overage=2_500.0,
        unit="usd",
        regime_at_breach="risk_off",
    )


def _trigger_record(
    *,
    cascade_id: str | None = None,
    secondary_breach_check_result: SecondaryBreachCheckResult | None = None,
    trigger_timestamp: datetime = _TRIGGER_TS,
) -> GuardrailTriggerRecord:
    return GuardrailTriggerRecord(
        rule_breached="per_position_max_loss",
        trigger_timestamp=trigger_timestamp,
        breach_details=_breach_details(),
        position_selection_rationale="position triggering the position-level max loss limit",
        cascade_id=cascade_id,
        secondary_breach_check_result=secondary_breach_check_result,
    )


def _envelope(
    *,
    envelope_id: str = "MON.session-abc.42",
    invocation_id: None = None,
    trigger_timestamp: datetime = _TRIGGER_TS,
    commands: tuple[CloseCommand, ...] | None = None,
    trigger_record: GuardrailTriggerRecord | None = None,
) -> EngineEnvelope:
    if commands is None:
        commands = (_close_command(),)
    if trigger_record is None:
        trigger_record = _trigger_record(trigger_timestamp=trigger_timestamp)
    return EngineEnvelope(
        envelope_id=envelope_id,
        invocation_id=invocation_id,
        trigger_timestamp=trigger_timestamp,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=trigger_record,
        commands=commands,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_constructs_with_required_fields(self) -> None:
        env = _envelope()
        assert env.envelope_id == "MON.session-abc.42"
        assert env.invocation_id is None
        assert env.trigger_timestamp == _TRIGGER_TS
        assert env.source_provenance == "engine_guardrail"
        assert len(env.commands) == 1
        assert env.commands[0].close_rationale_type == "risk_management"
        assert env.commands[0].risk_management_subtype == "engine_guardrail"

    def test_round_trip_via_type_adapter(self) -> None:
        adapter: TypeAdapter[EngineEnvelope] = TypeAdapter(EngineEnvelope)
        payload: Any = adapter.dump_python(_envelope())
        restored = adapter.validate_python(payload)
        assert isinstance(restored, EngineEnvelope)
        assert restored.envelope_id == "MON.session-abc.42"


# ---------------------------------------------------------------------------
# Frozen invariants
# ---------------------------------------------------------------------------


class TestFrozen:
    def test_breach_details_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _breach_details().current_value = 99.0

    def test_secondary_breach_check_result_frozen(self) -> None:
        sbc = SecondaryBreachCheckResult(result="no_secondary_breach")
        with pytest.raises(ValidationError):
            sbc.result = "deferred_to_pm"

    def test_guardrail_trigger_record_frozen(self) -> None:
        rec = _trigger_record()
        with pytest.raises(ValidationError):
            rec.rule_breached = "other_rule"

    def test_engine_envelope_frozen(self) -> None:
        env = _envelope()
        with pytest.raises(ValidationError):
            env.envelope_id = "MON.other.0"


# ---------------------------------------------------------------------------
# Negative cases — model_validator and field constraints
# ---------------------------------------------------------------------------


class TestEnvelopeIdPattern:
    def test_rejects_non_matching_envelope_id(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(envelope_id="ENV-REC-1")

    def test_rejects_envelope_id_missing_trigger(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(envelope_id="MON.session-abc")

    def test_rejects_envelope_id_with_extra_segment(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(envelope_id="MON.session-abc.42.0")


class TestInvocationIdNull:
    def test_rejects_non_none_invocation_id(self) -> None:
        with pytest.raises(ValidationError):
            EngineEnvelope(
                envelope_id="MON.session-abc.42",
                invocation_id="inv-1",  # type: ignore[arg-type]
                trigger_timestamp=_TRIGGER_TS,
                source_provenance="engine_guardrail",
                guardrail_trigger_record=_trigger_record(),
                commands=(_close_command(),),
            )

    def test_accepts_none_invocation_id_implicitly(self) -> None:
        env = EngineEnvelope(
            envelope_id="MON.session-abc.42",
            trigger_timestamp=_TRIGGER_TS,
            source_provenance="engine_guardrail",
            guardrail_trigger_record=_trigger_record(),
            commands=(_close_command(),),
        )
        assert env.invocation_id is None


class TestCommandsArrayConstraints:
    def test_rejects_empty_commands(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(commands=())

    def test_rejects_two_commands(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(commands=(_close_command(), _close_command()))


class TestEmbeddedCloseConstraints:
    def test_rejects_close_with_non_risk_management_rationale(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(
                commands=(
                    _close_command(
                        close_rationale_type="target_reached",
                        risk_management_subtype=None,
                    ),
                )
            )

    def test_rejects_close_with_pm_directed_subtype(self) -> None:
        with pytest.raises(ValidationError):
            _envelope(
                commands=(
                    _close_command(
                        close_rationale_type="risk_management",
                        risk_management_subtype="pm_directed",
                    ),
                )
            )


class TestTriggerTimestampEquality:
    def test_rejects_top_level_timestamp_mismatch(self) -> None:
        rec = _trigger_record(trigger_timestamp=_TRIGGER_TS)
        other_ts = datetime(2026, 5, 9, 15, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            EngineEnvelope(
                envelope_id="MON.session-abc.42",
                trigger_timestamp=other_ts,
                source_provenance="engine_guardrail",
                guardrail_trigger_record=rec,
                commands=(_close_command(),),
            )


# ---------------------------------------------------------------------------
# Cascade & secondary-breach permissive cases
# ---------------------------------------------------------------------------


class TestCascadeId:
    def test_envelope_with_cascade_id_parses_cleanly(self) -> None:
        env = _envelope(
            trigger_record=_trigger_record(cascade_id="cascade-123"),
        )
        assert env.guardrail_trigger_record.cascade_id == "cascade-123"

    def test_envelope_without_cascade_id_parses_cleanly(self) -> None:
        env = _envelope(trigger_record=_trigger_record(cascade_id=None))
        assert env.guardrail_trigger_record.cascade_id is None


class TestSecondaryBreachCheckResult:
    @pytest.mark.parametrize(
        "result",
        ["no_secondary_breach", "secondary_breach_avoided", "deferred_to_pm"],
    )
    def test_each_literal_parses_cleanly(self, result: str) -> None:
        sbc = SecondaryBreachCheckResult(result=result)  # type: ignore[arg-type]
        env = _envelope(
            trigger_record=_trigger_record(secondary_breach_check_result=sbc),
        )
        assert env.guardrail_trigger_record.secondary_breach_check_result is not None
        assert env.guardrail_trigger_record.secondary_breach_check_result.result == result

    def test_notes_field_optional(self) -> None:
        sbc_no_notes = SecondaryBreachCheckResult(result="no_secondary_breach")
        sbc_with_notes = SecondaryBreachCheckResult(
            result="secondary_breach_avoided", notes="alt position selected"
        )
        assert sbc_no_notes.notes is None
        assert sbc_with_notes.notes == "alt position selected"


class TestBreachDetailsOptionalFields:
    def test_unit_and_regime_optional(self) -> None:
        bd = BreachDetails(current_value=1.0, limit_value=0.5, overage=0.5)
        assert bd.unit is None
        assert bd.regime_at_breach is None


# ---------------------------------------------------------------------------
# Schema export accessor
# ---------------------------------------------------------------------------


class TestSchemaExport:
    def test_returns_dict(self) -> None:
        schema = engine_envelope_schema()
        assert isinstance(schema, dict)

    def test_commands_constrained_to_min_max_one(self) -> None:
        schema = engine_envelope_schema()
        commands = schema["properties"]["commands"]
        assert commands["minItems"] == 1
        assert commands["maxItems"] == 1

    def test_source_provenance_const(self) -> None:
        schema = engine_envelope_schema()
        sp = schema["properties"]["source_provenance"]
        assert sp.get("const") == "engine_guardrail"


# ---------------------------------------------------------------------------
# Public surface re-exports
# ---------------------------------------------------------------------------


class TestPackageReExports:
    def test_engine_envelope_models_importable_from_commands_package(self) -> None:
        """After ALP-458 the engine-envelope wire-format types live in
        :mod:`alphamind.commands`; ``alphamind.commands`` re-exports them.
        """
        from alphamind.commands import (
            BreachDetails as PkgBreachDetails,
        )
        from alphamind.commands import (
            EngineEnvelope as PkgEngineEnvelope,
        )
        from alphamind.commands import (
            GuardrailTriggerRecord as PkgGuardrailTriggerRecord,
        )
        from alphamind.commands import (
            SecondaryBreachCheckResult as PkgSecondaryBreachCheckResult,
        )
        from alphamind.commands import (
            engine_envelope_schema as pkg_engine_envelope_schema,
        )

        assert PkgEngineEnvelope is EngineEnvelope
        assert PkgGuardrailTriggerRecord is GuardrailTriggerRecord
        assert PkgBreachDetails is BreachDetails
        assert PkgSecondaryBreachCheckResult is SecondaryBreachCheckResult
        assert pkg_engine_envelope_schema is engine_envelope_schema
