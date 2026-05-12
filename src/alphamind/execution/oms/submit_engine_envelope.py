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
from typing import TYPE_CHECKING, Any, Literal

from alphamind.commands.engine_envelope import EngineEnvelope
from alphamind.commands.submission_results import (
    Acknowledgment,
    RejectionPayload,
    SubmissionResult,
    _BreachedRule,
)
from alphamind.execution.oms.command_ids import (
    derive_engine_command_id,
    parse_engine_command_id,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.execution.broker_adapter import AccountStateQueries

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
    client: TradingClient | None = None,
    queries: AccountStateQueries | None = None,
    execution_config: ExecutionConfig | None = None,
) -> SubmissionResult:
    """Persist the protective CLOSE embedded in *envelope* and return a SubmissionResult.

    See module docstring for the validation layering. On accepted submission,
    the protective CLOSE persists via the Phase 2 writeback machinery
    (``_writeback_close``) and one ``order_submitted`` activity-log entry is
    appended carrying the engine-guardrail provenance, the trigger record's
    ``position_selection_rationale``, ``rule_breached``, and (when set) the
    cascade_id. On ``deferred_to_pm`` secondary-breach, the function returns
    a rejection without persisting the close.

    When ``client`` + ``queries`` + ``execution_config`` are supplied (engine-stub
    coordinated swap, story 03e / ALP-390), the embedded CLOSE additionally
    routes through :func:`dispatch_command_to_broker` before persistence; the
    persisted order carries Alpaca's real ``alpaca_order_id`` and the
    acknowledgment surfaces it. When the broker context is omitted (legacy
    fixture-only path), the synthetic acknowledgment behavior is preserved.

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

    # Optionally route through the broker adapter before persistence (engine-stub
    # coordinated swap — story 03e / ALP-390). When the runner supplies a
    # ``TradingClient`` + ``AccountStateQueries`` + ``ExecutionConfig``, the
    # CLOSE submits to Alpaca first; the persisted order carries the broker's
    # real ``alpaca_order_id``. Otherwise (legacy fixture-only path), the
    # synthetic acknowledgment behavior is preserved.
    submitted_alpaca_order_id: str | None = None
    submitted_ack_order_id: str = f"ORD-CLOSE-{embedded.position_id}"
    if client is not None and queries is not None and execution_config is not None:
        dispatch_outcome = await _dispatch_engine_close(
            embedded,
            handle=handle,
            client=client,
            queries=queries,
            execution_config=execution_config,
            client_order_id=command_id,
        )
        if isinstance(dispatch_outcome, str):
            submitted_alpaca_order_id = dispatch_outcome
            submitted_ack_order_id = dispatch_outcome
        else:
            return _build_engine_gateway_failure_result(
                command_id=command_id,
                envelope=envelope,
                reason=dispatch_outcome,
            )

    # Persist the protective CLOSE via the Phase 2 writeback machinery.
    # Threading engine-guardrail provenance + position_selection_rationale +
    # cascade_id (when set) + rule_breached through extra_metadata so the
    # activity-log detail surfaces them on the order_submitted entry.
    # Inline import kept to defer SQLAlchemy load until first use; the
    # ALP-458 split eliminated the formerly-circular path through PM models.
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_engine_envelope_outcome,
    )

    # ``source_provenance`` is intentionally NOT threaded into extra_metadata:
    # ``_writeback_close`` already records ``risk_management_subtype="engine_guardrail"``
    # from ``command.risk_management_subtype`` (see phase2.py rationale_metadata
    # build), so adding ``source_provenance="engine_guardrail"`` would duplicate
    # the same fact under a second key on the activity-log detail.
    extra_metadata: dict[str, Any] = {
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
        submitted_alpaca_order_id=submitted_alpaca_order_id,
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
            order_id=submitted_ack_order_id,
        ),
    )


# ---------------------------------------------------------------------------
# Broker-dispatch helpers (story 03e / ALP-390)
# ---------------------------------------------------------------------------


async def _dispatch_engine_close(
    close_command: Any,
    *,
    handle: InvocationHandle,
    client: TradingClient,
    queries: AccountStateQueries,
    execution_config: ExecutionConfig,
    client_order_id: str,
) -> str | _BrokerFailure:
    """Dispatch the engine-originated CLOSE through the broker adapter.

    Resolves position context (symbol / quantity / side) from the persisted
    position record under *handle*'s session — engine envelopes carry only
    ``position_id`` and the dispatcher needs the broker-grade fields per the
    canonical CLOSE → broker translation contract.

    Returns the broker's ``alpaca_order_id`` on success or a
    :class:`_BrokerFailure` carrying the failure reason.
    """
    from alphamind.execution.broker_adapter import (
        GatewaySubmissionFailed,
        Submitted,
    )
    from alphamind.execution.broker_adapter.errors import classify_alpaca_error
    from alphamind.execution.broker_adapter.order_options import (
        PermanentRejectionError,
    )
    from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker
    from alphamind.execution.state_persistence.tables.positions import PositionRow
    from alphamind.execution.state_persistence.tables.positions_codec import (
        row_to_record as position_row_to_record,
    )

    pos_row = await handle.session.get(PositionRow, close_command.position_id)
    if pos_row is None:
        msg = f"engine CLOSE references missing position_id={close_command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    dispatch_kwargs = _engine_close_dispatch_kwargs(position, position_id=close_command.position_id)

    try:
        outcome = await dispatch_command_to_broker(
            close_command,
            client=client,
            queries=queries,
            execution=execution_config,
            client_order_id=client_order_id,
            **dispatch_kwargs,
        )
    except PermanentRejectionError as exc:
        return _BrokerFailure(reason=f"permanent_rejection: code={exc.rejection.code}")
    except Exception as exc:
        # Equity / mleg translators re-raise the raw alpaca-py APIError on
        # permanent failure rather than wrapping in PermanentRejectionError.
        # Mirror the PM-side ``_route_through_broker`` and translate via
        # ``classify_alpaca_error`` so the engine envelope path returns a
        # uniform broker-rejection shape regardless of which translator
        # produced the error. ``BaseException`` (CancelledError, etc.)
        # propagates so external interruptions are never re-classified as
        # broker rejections.
        rejection = classify_alpaca_error(exc)
        if rejection is None:
            raise
        return _BrokerFailure(reason=f"permanent_rejection: code={rejection.code}")

    if isinstance(outcome, GatewaySubmissionFailed):
        return _BrokerFailure(
            reason=(
                f"gateway_submission_failed: {outcome.reason} "
                f"(last_error={outcome.last_error_class}, attempts={outcome.attempt_count})"
            )
        )
    assert isinstance(outcome, Submitted)
    return outcome.payload.alpaca_order_id


def _engine_close_dispatch_kwargs(
    position: Any,
    *,
    position_id: str,
) -> dict[str, Any]:
    """Project the persisted *position* into the dispatcher's per-asset kwargs.

    Equity → symbol/qty/side. Options → OCC + sell-to-close intent. Strategy
    → open_legs + strategy_type + units. Mirrors the per-asset routing the
    PM-side ``_close_command_context`` performs; surfaces engine-close on
    options / strategy positions so the substrate (``submit_options_close`` /
    ``submit_mleg_close``) is exercised end-to-end rather than blocked behind
    a hardcoded ``NotImplementedError``.
    """
    from typing import cast as _cast

    from alphamind.commands.command_models import StrategyType
    from alphamind.execution.broker_adapter import MLEGLegAck
    from alphamind.execution.broker_adapter.order_options import build_occ_symbol
    from alphamind.portfolio_state.records.positions import (
        EquityPositionDetails,
        OptionsPositionDetails,
        StrategyPositionDetails,
    )

    if isinstance(position.details, EquityPositionDetails):
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_qty": position.details.share_count,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        occ = build_occ_symbol(
            position.details.underlying_ticker,
            position.details.expiration_date,
            position.details.contract_type,
            position.details.strike_price,
        )
        return {
            "position_asset_type": "option",
            "occ_symbol": occ,
            "position_qty": position.details.contract_count,
            "position_intent": (
                "sell_to_close" if position.direction.value == "LONG" else "buy_to_close"
            ),
        }
    if isinstance(position.details, StrategyPositionDetails):
        legs: list[Any] = []
        for leg in position.details.legs:
            opt = leg.options
            occ = build_occ_symbol(
                opt.underlying_ticker, opt.expiration_date, opt.contract_type, opt.strike_price
            )
            leg_direction = leg.direction
            if leg_direction is None:
                msg = (
                    f"engine CLOSE on strategy position {position_id!r} has leg "
                    f"{leg.leg_id!r} with no direction set"
                )
                raise ValueError(msg)
            side: Literal["buy", "sell"] = "buy" if leg_direction.value == "LONG" else "sell"
            intent: Literal["buy_to_open", "sell_to_open"] = (
                "buy_to_open" if side == "buy" else "sell_to_open"
            )
            legs.append(
                MLEGLegAck(
                    occ_symbol=occ,
                    side=side,
                    ratio_qty=1,
                    position_intent=intent,
                )
            )
        return {
            "position_asset_type": "strategy",
            "open_legs": tuple(legs),
            "strategy_type": _cast(StrategyType, position.details.strategy_type_label),
            "position_units": None,
        }
    msg = (
        f"engine CLOSE on position {position_id!r} carries unsupported details "
        f"type {type(position.details).__name__}"
    )
    raise NotImplementedError(msg)


@dataclass(frozen=True)
class _BrokerFailure:
    """Internal carrier surfacing a broker-side failure to the engine envelope path."""

    reason: str


def _build_engine_gateway_failure_result(
    *,
    command_id: str,
    envelope: EngineEnvelope,
    reason: _BrokerFailure,
) -> SubmissionResult:
    """Build a rejected SubmissionResult for a broker gateway failure.

    Engine envelopes that fail at the broker do NOT mark the trigger as seen
    (the caller's ``state.seen_trigger_ids.add(...)`` is skipped via the
    early return) — the continuous monitor may retry on the next trigger.
    """
    rejection = RejectionPayload(
        rules_breached=(
            _BreachedRule(
                rule="broker_gateway_failure",
                current=envelope.guardrail_trigger_record.breach_details.current_value,
                limit=envelope.guardrail_trigger_record.breach_details.limit_value,
                overage=envelope.guardrail_trigger_record.breach_details.overage,
                unit=envelope.guardrail_trigger_record.breach_details.unit or "",
            ),
        ),
        suggested_modification=reason.reason,
        feature_disabled=None,
    )
    return SubmissionResult(
        command_ordinal=0,
        status="rejected",
        command_id=command_id,
        rejection_payload=rejection,
    )
