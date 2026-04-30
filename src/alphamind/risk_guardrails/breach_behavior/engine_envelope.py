"""Engine-originated envelope assembler (story 06)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.risk_guardrails.breach_behavior.types import (
    _ENVELOPE_ID_PATTERN,
    BreachDetails,
    EngineCloseCommand,
    EngineEnvelope,
    EngineGuardrailTriggerRecord,
    PositionSelectionAction,
    PositionSelectionResult,
    SecondaryBreachCheckResult,
)


def envelope_id_for(*, monitor_session_id: str, trigger_id: int) -> str:
    """Return the canonical envelope_id ``MON.{session}.{trigger}``."""
    if not monitor_session_id:
        msg = "monitor_session_id must be a non-empty string"
        raise ValueError(msg)
    if "." in monitor_session_id:
        msg = (
            f"monitor_session_id must not contain '.' (the field separator); "
            f"got {monitor_session_id!r}"
        )
        raise ValueError(msg)
    if trigger_id < 1:
        msg = f"trigger_id must be >= 1; got {trigger_id}"
        raise ValueError(msg)
    return f"MON.{monitor_session_id}.{trigger_id}"


def command_id_for(*, envelope_id: str, ordinal: int = 1) -> str:
    """Return the canonical command_id ``{envelope_id}.{ordinal}``."""
    if not _ENVELOPE_ID_PATTERN.fullmatch(envelope_id):
        msg = f"envelope_id must match {_ENVELOPE_ID_PATTERN.pattern!r}; got {envelope_id!r}"
        raise ValueError(msg)
    if ordinal < 1:
        msg = f"ordinal must be >= 1; got {ordinal}"
        raise ValueError(msg)
    return f"{envelope_id}.{ordinal}"


def compose_guardrail_trigger_record(
    *,
    rule_breached: str,
    trigger_timestamp: datetime,
    breach_details: BreachDetails,
    position_selection: PositionSelectionResult,
    cascade_id: str | None = None,
    secondary_breach_check: SecondaryBreachCheckResult | None = None,
) -> EngineGuardrailTriggerRecord:
    """Assemble the EngineGuardrailTriggerRecord from breach context inputs."""
    if not rule_breached:
        msg = "rule_breached must be a non-empty string"
        raise ValueError(msg)
    return EngineGuardrailTriggerRecord(
        rule_breached=rule_breached,
        trigger_timestamp=trigger_timestamp,
        breach_details=breach_details,
        position_selection_rationale=position_selection.rationale,
        cascade_id=cascade_id,
        secondary_breach_check_result=secondary_breach_check,
    )


def compose_engine_envelope(  # noqa: PLR0913 — signature dictated by story 06 AC assembler surface
    *,
    monitor_session_id: str,
    trigger_id: int,
    trigger_timestamp: datetime,
    rule_breached: str,
    breach_details: BreachDetails,
    position_selection: PositionSelectionResult,
    positions_by_id: dict[str, PositionRecord],
    portfolio_value_usd: float,
    cascade_id: str | None = None,
    secondary_breach_check: SecondaryBreachCheckResult | None = None,
    execution_method: Literal["market", "limit"] = "market",
    limit_price: float | None = None,
) -> EngineEnvelope:
    """Compose a complete EngineEnvelope from breach + position-selection inputs."""
    if portfolio_value_usd <= 0:
        msg = f"portfolio_value_usd must be positive; got {portfolio_value_usd}"
        raise ValueError(msg)
    if position_selection.position_id not in positions_by_id:
        msg = (
            f"position_selection.position_id {position_selection.position_id!r} "
            f"is not present in positions_by_id"
        )
        raise ValueError(msg)
    envelope_id = envelope_id_for(monitor_session_id=monitor_session_id, trigger_id=trigger_id)
    command = EngineCloseCommand(
        command_id=command_id_for(envelope_id=envelope_id, ordinal=1),
        position_id=position_selection.position_id,
        quantity_or_all=_quantity_or_all_for(
            position_selection=position_selection,
            portfolio_value_usd=portfolio_value_usd,
        ),
        execution_method=execution_method,
        limit_price=limit_price,
    )
    trigger_record = compose_guardrail_trigger_record(
        rule_breached=rule_breached,
        trigger_timestamp=trigger_timestamp,
        breach_details=breach_details,
        position_selection=position_selection,
        cascade_id=cascade_id,
        secondary_breach_check=secondary_breach_check,
    )
    return EngineEnvelope(
        envelope_id=envelope_id,
        trigger_timestamp=trigger_timestamp,
        guardrail_trigger_record=trigger_record,
        command=command,
    )


def _quantity_or_all_for(
    *,
    position_selection: PositionSelectionResult,
    portfolio_value_usd: float,
) -> Literal["all"] | float:
    """Translate position-selection action into the close command's quantity_or_all field."""
    if position_selection.action == PositionSelectionAction.FULL_CLOSE:
        return "all"
    target_pct = position_selection.target_post_action_size_pct_of_portfolio
    # PARTIAL_TRIM invariant enforced by PositionSelectionResult; guard explicitly so
    # the error survives ``python -O`` and surfaces a clear message if the upstream
    # invariant ever drifts.
    if target_pct is None:
        msg = "PARTIAL_TRIM action requires target_post_action_size_pct_of_portfolio"
        raise ValueError(msg)
    return target_pct / 100.0 * portfolio_value_usd


__all__ = [
    "command_id_for",
    "compose_engine_envelope",
    "compose_guardrail_trigger_record",
    "envelope_id_for",
]
