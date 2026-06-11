"""Shared vocabulary for activity-log events — enums and umbrella entry.

The per-event-group submodules (``position_lifecycle``, ``order_lifecycle``,
``bracket``, ``thesis``, ``cash_margin``, ``risk_guardrail``, ``pm_decision``,
``corporate_action``, ``reconciliation``, ``configuration``) import their
discriminator (``EventType`` member) and their group (``EventGroup`` member)
from here. The umbrella ``ActivityLogEntry`` record lives here too because it
wraps every detail-payload class and therefore depends on no specific group.

The dispatch dicts ``EVENT_TYPE_TO_DETAIL_CLASS`` and ``EVENT_TYPE_TO_GROUP``
are NOT defined here — they are derived in ``events/__init__.py`` from the
per-submodule ``_REGISTRY`` lists so the catalog has a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class EventGroup(StrEnum):
    """The catalog groups from state-persistence.md § Activity log entries."""

    POSITION_LIFECYCLE = "POSITION_LIFECYCLE"
    ORDER_LIFECYCLE = "ORDER_LIFECYCLE"
    BRACKET = "BRACKET"
    THESIS = "THESIS"
    CASH_AND_MARGIN = "CASH_AND_MARGIN"
    RISK_AND_GUARDRAIL = "RISK_AND_GUARDRAIL"
    PM_DECISION = "PM_DECISION"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    CONFIGURATION = "CONFIGURATION"
    RECONCILIATION = "RECONCILIATION"
    DISTILLATION_ANOMALY = "DISTILLATION_ANOMALY"


class EventType(StrEnum):
    """One member per event in the state-persistence.md catalog."""

    # Position lifecycle
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    POSITION_ADDED = "POSITION_ADDED"
    POSITION_REDUCED = "POSITION_REDUCED"

    # Order lifecycle
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_MODIFIED = "ORDER_MODIFIED"

    # Bracket events
    BRACKET_ACTIVATED = "BRACKET_ACTIVATED"
    BRACKET_COMPLETED = "BRACKET_COMPLETED"
    BRACKET_DISSOLVED = "BRACKET_DISSOLVED"
    BRACKET_MODIFIED = "BRACKET_MODIFIED"
    BRACKET_INCOMPLETE_WARNING = "BRACKET_INCOMPLETE_WARNING"
    BRACKET_CANCELLED_CORPORATE_ACTION = "BRACKET_CANCELLED_CORPORATE_ACTION"

    # Thesis events
    THESIS_CREATED = "THESIS_CREATED"
    THESIS_COMPONENT_ADDED = "THESIS_COMPONENT_ADDED"
    THESIS_COMPONENT_UPDATED = "THESIS_COMPONENT_UPDATED"
    THESIS_RESOLVED = "THESIS_RESOLVED"
    THESIS_STATUS_CHANGED = "THESIS_STATUS_CHANGED"

    # Cash and margin events
    CASH_DEBITED = "CASH_DEBITED"
    CASH_CREDITED = "CASH_CREDITED"
    CAPITAL_RESERVED = "CAPITAL_RESERVED"
    CAPITAL_RELEASED = "CAPITAL_RELEASED"
    MARGIN_CALL = "MARGIN_CALL"
    MARGIN_CALL_RESOLVED = "MARGIN_CALL_RESOLVED"
    MARGIN_LIQUIDATION = "MARGIN_LIQUIDATION"
    BORROW_COST_ACCRUED = "BORROW_COST_ACCRUED"

    # Risk and guardrail events
    GUARDRAIL_REJECTION = "GUARDRAIL_REJECTION"
    RISK_LIMIT_APPROACHED = "RISK_LIMIT_APPROACHED"
    RISK_PARAMETER_CHANGED = "RISK_PARAMETER_CHANGED"
    EMERGENCY_INVOCATION_REQUESTED = "EMERGENCY_INVOCATION_REQUESTED"
    HALT_ACTIVATED = "HALT_ACTIVATED"
    HALT_LIFTED = "HALT_LIFTED"
    GREEKS_REFRESH_FAILED = "GREEKS_REFRESH_FAILED"

    # PM decision events
    PM_DECISION = "PM_DECISION"
    COMMAND_ABANDONED = "COMMAND_ABANDONED"
    ENVELOPE_PARSE_FAILED = "ENVELOPE_PARSE_FAILED"
    ENVELOPE_REJECTED = "ENVELOPE_REJECTED"

    # Corporate action events
    CORPORATE_ACTION_APPLIED = "CORPORATE_ACTION_APPLIED"

    # Reconciliation events
    RECONCILIATION_ALERT = "RECONCILIATION_ALERT"
    RECONCILIATION_CORRECTION = "RECONCILIATION_CORRECTION"

    # Configuration events
    DISTILLATION_CONFIG_CHANGE = "DISTILLATION_CONFIG_CHANGE"
    PROFILE_SWITCHED = "PROFILE_SWITCHED"

    # Distillation anomaly events
    DISTILLATION_ANOMALY_FLAG = "DISTILLATION_ANOMALY_FLAG"


class EventSource(StrEnum):
    """Subsystems that emit activity log entries.

    OMS subsystems are the original emitters; ``CONFIG_RELOAD`` is emitted by
    the configuration loader at the start of each invocation when a reloaded
    distillation config differs from the prior reload, and ``OPERATOR_CONSOLE``
    is emitted by the [command center](docs/design/command-center.md) on
    operator-driven mutations.
    """

    FILL_PROCESSOR = "FILL_PROCESSOR"
    COMMAND_EXECUTOR = "COMMAND_EXECUTOR"
    BRACKET_MANAGER = "BRACKET_MANAGER"
    MARGIN_MONITOR = "MARGIN_MONITOR"
    BORROW_ACCRUAL_MONITOR = "BORROW_ACCRUAL_MONITOR"
    GUARDRAIL_LAYER = "GUARDRAIL_LAYER"
    CORPORATE_ACTION_PROCESSOR = "CORPORATE_ACTION_PROCESSOR"
    CONFIG_RELOAD = "CONFIG_RELOAD"
    OPERATOR_CONSOLE = "OPERATOR_CONSOLE"
    DISTILLATION_ORCHESTRATOR = "DISTILLATION_ORCHESTRATOR"
    # The deliberative analysis pipeline's thesis-resolution step (ALP-834 /
    # ALP-899): authors ACTIVE → RESOLVED and emits THESIS_RESOLVED. The
    # ``ck_activity_log_source`` CHECK is built from this enum, so a companion
    # migration widens it on alembic-managed DBs created before this member.
    ANALYSIS_PIPELINE = "ANALYSIS_PIPELINE"
    # The option-lifecycle poll (ALP-918): emits POSITION_CLOSED on an option
    # expiry / assignment / exercise so the closed-position thesis resolver can
    # resolve the thesis. ``account_activities/`` emits no other activity-log
    # entry, so no existing source fits. Like ANALYSIS_PIPELINE, the source
    # CHECK is enum-built and a companion migration widens it.
    ACCOUNT_ACTIVITIES_PROCESSOR = "ACCOUNT_ACTIVITIES_PROCESSOR"


class PositionExitMethod(StrEnum):
    """Exit method for a closed position."""

    STOP_TRIGGERED = "STOP_TRIGGERED"
    TARGET_REACHED = "TARGET_REACHED"
    PM_DECISION = "PM_DECISION"
    TIME_EXPIRED = "TIME_EXPIRED"
    MARGIN_LIQUIDATION = "MARGIN_LIQUIDATION"
    FORCED_BUY_IN = "FORCED_BUY_IN"
    CORPORATE_ACTION_CASH_MERGER = "CORPORATE_ACTION_CASH_MERGER"
    # Option-lifecycle close exit methods (ALP-918), mirroring the
    # ``OPEXP``/``OPASN``/``OPEXC`` broker-event distinction. Stored inside the
    # ``PositionClosedDetail.exit_method`` JSON blob (not a CHECK-constrained
    # column), so no migration is required for these members.
    OPTION_EXPIRY = "OPTION_EXPIRY"
    OPTION_ASSIGNMENT = "OPTION_ASSIGNMENT"
    OPTION_EXERCISE = "OPTION_EXERCISE"


class PositionOpenMechanism(StrEnum):
    """Mechanism by which a position was opened."""

    ORDER_FILL = "ORDER_FILL"
    SPIN_OFF_FROM_PARENT = "SPIN_OFF_FROM_PARENT"


class OrderRejectionSource(StrEnum):
    """Source of an order rejection."""

    GUARDRAIL = "GUARDRAIL"
    BROKER = "BROKER"


class CashDebitReason(StrEnum):
    """Reason for a cash debit event.

    Both fill reasons appear in both vocabularies (ALP-944): cash direction is
    side-shaped (every buy debits, every sell credits) while entry/exit is
    role-shaped, so a short's buy-to-cover is a debit with ``EXIT_FILL``.
    Stored inside ``activity_log.detail_json`` (not a CHECK-constrained
    column), so no migration is required for these members.
    """

    ENTRY_FILL = "ENTRY_FILL"
    EXIT_FILL = "EXIT_FILL"
    FEES = "FEES"
    CASH_DIVIDEND_SHORT_OBLIGATION = "CASH_DIVIDEND_SHORT_OBLIGATION"


class CashCreditReason(StrEnum):
    """Reason for a cash credit event.

    Carries ``ENTRY_FILL`` for the same reason ``CashDebitReason`` carries
    ``EXIT_FILL`` — a short's entry is a sell that credits cash (ALP-944).
    """

    ENTRY_FILL = "ENTRY_FILL"
    EXIT_FILL = "EXIT_FILL"
    CASH_DIVIDEND_LONG = "CASH_DIVIDEND_LONG"
    FRACTIONAL_SHARE_CASH_OUT = "FRACTIONAL_SHARE_CASH_OUT"
    CASH_MERGER_PROCEEDS = "CASH_MERGER_PROCEEDS"


class CorporateActionType(StrEnum):
    """Per-action-type matrix from corporate-actions.md.

    The first nine members carry a position / cash mutation (an integration
    handler). The trailing capture-only members (``WORTHLESS_REMOVAL`` /
    ``UNIT_SPLIT`` / ``REDEMPTION``, ALP-849 / W1c) are the v1beta1 types the
    fetcher used to drop: they have no defined position-mutation math, so they
    are captured onto the append-only ``broker_event_log`` (gap-free broker fact)
    without a state mutation.
    """

    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CASH_DIVIDEND_LONG = "CASH_DIVIDEND_LONG"
    CASH_DIVIDEND_SHORT = "CASH_DIVIDEND_SHORT"
    CASH_MERGER = "CASH_MERGER"
    STOCK_MERGER = "STOCK_MERGER"
    SPIN_OFF = "SPIN_OFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    # Capture-only (no position-mutation handler) — gap-free broker fact only.
    WORTHLESS_REMOVAL = "WORTHLESS_REMOVAL"
    UNIT_SPLIT = "UNIT_SPLIT"
    REDEMPTION = "REDEMPTION"


class PMVerdict(StrEnum):
    """Verdict from a PM decision."""

    APPROVE = "APPROVE"
    APPROVE_WITH_MODIFICATION = "APPROVE_WITH_MODIFICATION"
    REJECT = "REJECT"
    OVERRIDE_WITH_CORRECTIVE_ACTION = "OVERRIDE_WITH_CORRECTIVE_ACTION"


class BracketModificationSource(StrEnum):
    """Source of a bracket modification."""

    PM = "PM"
    FILL_ANCHOR_RECALCULATION = "FILL_ANCHOR_RECALCULATION"
    CORPORATE_ACTION_ADJUSTMENT = "CORPORATE_ACTION_ADJUSTMENT"


@dataclass(frozen=True, slots=True)
class ActivityLogEntry:
    """A single entry in the portfolio activity log.

    ``detail`` is one of the per-event-type detail-payload dataclasses defined
    in the per-event-group submodules. The umbrella record validates two
    cross-field invariants in ``__post_init__``:

    * ``timestamp`` must be tz-aware UTC (no naive datetimes).
    * ``entry_id`` and ``invocation_id`` must be non-empty.

    The detail-class / event-group cross-checks (``event_group`` matches the
    EventType's group, ``type(detail)`` matches the EventType's detail class)
    are validated lazily on first construction via the lookup helpers exposed
    from ``events/__init__.py`` (see ``_validate_event_group`` /
    ``_validate_detail_class`` therein) to avoid circular imports with the
    per-group submodules.
    """

    entry_id: str
    invocation_id: str
    timestamp: datetime
    event_type: EventType
    event_group: EventGroup
    position_id: str | None
    order_id: str | None
    thesis_id: str | None
    source: EventSource
    # ``detail`` is the per-event-type detail payload — one of the frozen
    # dataclasses registered in the per-submodule ``_REGISTRY`` lists. The
    # annotation stays ``Any`` (rather than a union) so consumers can narrow
    # via direct attribute access after dispatching on ``event_type`` —
    # matching the legacy Pydantic shape across the migration.
    detail: Any

    def __post_init__(self) -> None:
        if not self.entry_id:
            msg = "entry_id must be non-empty"
            raise ValueError(msg)
        if not self.invocation_id:
            msg = "invocation_id must be non-empty"
            raise ValueError(msg)
        if self.timestamp.tzinfo is None:
            msg = "timestamp must be tz-aware UTC"
            raise ValueError(msg)
        # event_group / detail-class cross-checks happen in events/__init__.py
        # at module import — see _validate_entry_dispatch.
        _validate_entry_dispatch(self)


def _validate_entry_dispatch(entry: ActivityLogEntry) -> None:
    """Validate ``event_group`` / ``detail`` class against the EventType.

    Defined as a module-level function so the per-group submodules can install
    the dispatch tables before the umbrella record is first constructed. The
    function imports the dispatch dicts lazily — at module import time the
    submodules and ``events/__init__.py`` may not have finished setting up the
    cycle.
    """
    from alphamind.portfolio_state.events import (
        EVENT_TYPE_TO_DETAIL_CLASS,
        EVENT_TYPE_TO_GROUP,
    )

    expected_group = EVENT_TYPE_TO_GROUP[entry.event_type]
    if entry.event_group != expected_group:
        msg = (
            f"event_group mismatch: event_type={entry.event_type!r} expects "
            f"{expected_group!r} but got {entry.event_group!r}"
        )
        raise ValueError(msg)

    expected_class = EVENT_TYPE_TO_DETAIL_CLASS[entry.event_type]
    actual_class = type(entry.detail)
    if actual_class is not expected_class:
        msg = (
            f"detail class mismatch: event_type={entry.event_type!r} expects "
            f"{expected_class.__name__} but got {actual_class.__name__}"
        )
        raise ValueError(msg)


__all__ = [
    "ActivityLogEntry",
    "BracketModificationSource",
    "CashCreditReason",
    "CashDebitReason",
    "CorporateActionType",
    "EventGroup",
    "EventSource",
    "EventType",
    "OrderRejectionSource",
    "PMVerdict",
    "PositionExitMethod",
    "PositionOpenMechanism",
]
