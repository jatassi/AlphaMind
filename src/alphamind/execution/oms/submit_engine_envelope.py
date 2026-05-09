"""Monitor-facing engine envelope submission path — ALP-375 / story 04.

Provides :func:`submit_engine_envelope` — the direct (non-MCP) write function
the continuous monitor calls between invocations to persist a protective CLOSE
issued from a guardrail trigger. Mirrors the PM-side ``submit_envelope_mcp``
envelope-level validation + per-command writeback shape but operates on
engine-originated envelopes.

Per parent ALP-120 decision (D), this is a direct function call (not MCP)
since the continuous monitor is not an LLM agent.

Validation layering:

1. Layer-1 (Pydantic): the typed :class:`EngineEnvelope` already enforces
   structural invariants per ``engine-envelope-schema.md`` (story 02a).
2. Command-ID consistency: if the embedded CLOSE carries a ``command_id``,
   the function verifies it parses cleanly via :func:`parse_engine_command_id`
   and matches the envelope's session/trigger components. If absent, the
   function derives one via :func:`derive_engine_command_id`.
3. Secondary-breach gate: when the trigger record's
   ``secondary_breach_check_result.result == "deferred_to_pm"``, the function
   refuses submission per ``oms-commands.md § Command origins``.
4. Duplicate detection: a ``(monitor_session_id, trigger_id)`` pair seen
   twice within a session is a structural error per
   ``oms-command-ids.md § What happens if a duplicate command ID arrives``.

Broker routing remains deferred per parent decision (C); the function
persists the protective CLOSE via the Phase 2 writeback machinery and
returns a :class:`SubmissionResult` matching the existing PM-side shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from alphamind.execution.oms.command_ids import (
    derive_engine_command_id,
    parse_engine_command_id,
)
from alphamind.execution.oms.engine_envelope import EngineEnvelope
from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    RejectionPayload,
    SubmissionResult,
    _BreachedRule,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)

__all__ = [
    "SubmitEngineEnvelopeState",
    "build_initial_submit_engine_envelope_state",
    "submit_engine_envelope",
]


# Engine envelope IDs are ``MON.{session}.{trigger}`` — three dotted segments.
# Source: ``engine-envelope-schema.md`` ``properties.envelope_id.pattern``.
_ENVELOPE_ID_PATTERN = re.compile(r"^MON\.(?P<session>[^.]+)\.(?P<trigger>[0-9]+)$")


@dataclass
class SubmitEngineEnvelopeState:
    """Mutable per-monitor-session state for :func:`submit_engine_envelope`.

    The state cell tracks the bound ``monitor_session_id`` and the set of
    ``trigger_id`` values already submitted within this session so duplicate
    ``(monitor_session_id, trigger_id)`` pairs raise per
    ``oms-command-ids.md § What happens if a duplicate command ID arrives``.
    A monitor restart creates a new session and a fresh state cell.
    """

    monitor_session_id: str
    seen_trigger_ids: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.monitor_session_id:
            msg = "SubmitEngineEnvelopeState.monitor_session_id must be non-empty"
            raise ValueError(msg)
        if "." in self.monitor_session_id:
            msg = (
                "SubmitEngineEnvelopeState.monitor_session_id must not contain '.'; "
                f"got {self.monitor_session_id!r}"
            )
            raise ValueError(msg)


def build_initial_submit_engine_envelope_state(
    *,
    monitor_session_id: str,
) -> SubmitEngineEnvelopeState:
    """Construct a fresh :class:`SubmitEngineEnvelopeState` for one monitor session."""
    return SubmitEngineEnvelopeState(monitor_session_id=monitor_session_id)


async def submit_engine_envelope(
    envelope: EngineEnvelope,
    *,
    handle: InvocationHandle,
    state: SubmitEngineEnvelopeState,
    config: StatePersistenceConfig,
) -> SubmissionResult:
    """Persist the protective CLOSE embedded in *envelope* and return a SubmissionResult.

    See module docstring for the validation layering. On accepted submission,
    the protective CLOSE persists via the Phase 2 writeback machinery
    (``_writeback_close``) and one ``order_submitted`` activity-log entry is
    appended carrying the engine-guardrail provenance, the trigger record's
    ``position_selection_rationale``, ``rule_breached``, and (when set) the
    cascade_id. On ``deferred_to_pm`` secondary-breach, the function returns
    a rejection without persisting the close.

    Raises :class:`ValueError` for command-ID inconsistencies, monitor-session
    mismatches, or duplicate ``(monitor_session_id, trigger_id)`` submissions
    within a session — all are structural errors, not protocol-level rejections.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    # Parse envelope id components.
    envelope_match = _ENVELOPE_ID_PATTERN.match(envelope.envelope_id)
    if envelope_match is None:
        # Defense-in-depth — Pydantic already rejected this at Layer-1 via the
        # ``EngineEnvelope.envelope_id`` regex. If we get here, the envelope
        # bypassed model construction.
        msg = f"envelope_id {envelope.envelope_id!r} does not parse as MON.{{session}}.{{trigger}}"
        raise ValueError(msg)
    envelope_session = envelope_match["session"]
    envelope_trigger = int(envelope_match["trigger"])

    # The bound state cell's monitor_session_id must match the envelope's.
    # A session change creates a new state cell — submissions across sessions
    # would corrupt the seen_trigger_ids dedup set.
    if envelope_session != state.monitor_session_id:
        msg = (
            f"engine envelope session {envelope_session!r} does not match state's "
            f"monitor_session_id {state.monitor_session_id!r}"
        )
        raise ValueError(msg)

    # Duplicate (monitor_session_id, trigger_id) within session is a structural
    # bug — see oms-command-ids.md § What happens if a duplicate command ID arrives.
    if envelope_trigger in state.seen_trigger_ids:
        msg = (
            f"duplicate engine envelope detected: "
            f"(session={envelope_session!r}, trigger_id={envelope_trigger}) already submitted"
        )
        raise ValueError(msg)

    embedded = envelope.commands[0]

    # Derive or validate the embedded command_id.
    if embedded.command_id is None:
        command_id = derive_engine_command_id(
            monitor_session_id=envelope_session,
            trigger_id=envelope_trigger,
            command_ordinal=0,
        )
    else:
        # parse_engine_command_id raises ValueError for malformed ids — that
        # bubbles up directly as the structural error.
        components = parse_engine_command_id(embedded.command_id)
        if (
            components.monitor_session_id != envelope_session
            or components.trigger_id != envelope_trigger
        ):
            msg = (
                f"embedded command_id {embedded.command_id!r} does not match envelope "
                f"(session={envelope_session!r}, trigger_id={envelope_trigger})"
            )
            raise ValueError(msg)
        command_id = embedded.command_id

    # Secondary-breach gate. The continuous monitor was supposed to defer the
    # CLOSE to the PM rather than submit it; receipt at the OMS is a contract
    # violation per oms-commands.md § Command origins.
    sbcr = envelope.guardrail_trigger_record.secondary_breach_check_result
    if sbcr is not None and sbcr.result == "deferred_to_pm":
        rejection = RejectionPayload(
            rules_breached=(
                _BreachedRule(
                    rule="secondary_breach_deferred_to_pm",
                    current=envelope.guardrail_trigger_record.breach_details.current_value,
                    limit=envelope.guardrail_trigger_record.breach_details.limit_value,
                    overage=envelope.guardrail_trigger_record.breach_details.overage,
                    unit=envelope.guardrail_trigger_record.breach_details.unit or "",
                ),
            ),
            suggested_modification=(
                "Engine submitted a CLOSE flagged secondary_breach_check_result=deferred_to_pm; "
                "the continuous monitor must defer to the PM at the next invocation rather than "
                "submit. See oms-commands.md § Command origins."
            ),
            feature_disabled=None,
        )
        return SubmissionResult(
            command_ordinal=0,
            status="rejected",
            command_id=command_id,
            rejection_payload=rejection,
        )

    # Persist the protective CLOSE via the Phase 2 writeback machinery.
    # Threading engine-guardrail provenance + position_selection_rationale +
    # cascade_id (when set) + rule_breached through extra_metadata so the
    # activity-log detail surfaces them on the order_submitted entry.
    # Lazy import: phase2.py imports submit_envelope_mcp.py for the
    # SubmissionResult dataclass; routing through the package re-export
    # would circle back here, so we import the module directly.
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_engine_envelope_outcome,
    )

    extra_metadata: dict[str, Any] = {
        "source_provenance": "engine_guardrail",
        "envelope_id": envelope.envelope_id,
        "rule_breached": envelope.guardrail_trigger_record.rule_breached,
        "position_selection_rationale": (
            envelope.guardrail_trigger_record.position_selection_rationale
        ),
    }
    if envelope.guardrail_trigger_record.cascade_id is not None:
        extra_metadata["cascade_id"] = envelope.guardrail_trigger_record.cascade_id

    await persist_engine_envelope_outcome(
        handle,
        close_command=embedded,
        command_id=command_id,
        extra_metadata=extra_metadata,
    )

    # Mark the trigger as seen only after a successful persistence; a failure
    # mid-writeback rolls back the surrounding transaction, so the state cell
    # would otherwise be out of sync with the persisted record.
    state.seen_trigger_ids.add(envelope_trigger)

    return SubmissionResult(
        command_ordinal=0,
        status="accepted",
        command_id=command_id,
        acknowledgment=Acknowledgment(
            position_id=embedded.position_id,
            order_id=f"ORD-CLOSE-{embedded.position_id}",
        ),
    )
