"""Tier 2 — append-only lifecycle/event records per ``state-persistence.md`` § Tier 2.

Activity-log details are split across per-event-group submodules
(``position_lifecycle``, ``order_lifecycle``, ``bracket``, ``thesis``,
``cash_margin``, ``risk_guardrail``, ``pm_decision``, ``corporate_action``,
``reconciliation``, ``configuration``) — each frozen-dataclass detail payload
sits alongside its peers and registers itself via the module's ``_REGISTRY``
list.

This ``__init__`` aggregates the per-submodule registries into the two
dispatch dicts (``EVENT_TYPE_TO_DETAIL_CLASS``, ``EVENT_TYPE_TO_GROUP``) and
re-exports every public symbol — preserving the ``from
alphamind.portfolio_state.events.activity_log import X`` import shape for
existing consumers via the ``activity_log`` shim re-export.

The codec helpers (``encode_detail``, ``decode_detail``) live in
``portfolio_state.events.codec`` and replace the previous Pydantic
``model_dump_json`` / ``model_validate_json`` pair — they round-trip
frozen-dataclass instances through SQLite TEXT columns with Decimal-exact
precision (see story 05b's ``DecimalText`` pattern).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from alphamind.portfolio_state.events import (
    bracket,
    cash_margin,
    configuration,
    corporate_action,
    distillation_anomaly,
    order_lifecycle,
    pm_decision,
    position_lifecycle,
    reconciliation,
    risk_guardrail,
    thesis,
)
from alphamind.portfolio_state.events.bracket import (
    BracketActivatedDetail,
    BracketCancelledCorporateActionDetail,
    BracketCompletedDetail,
    BracketDissolvedDetail,
    BracketIncompleteWarningDetail,
    BracketModifiedDetail,
)
from alphamind.portfolio_state.events.cash_margin import (
    BorrowCostAccruedDetail,
    CapitalReleasedDetail,
    CapitalReservedDetail,
    CashCreditedDetail,
    CashDebitedDetail,
    MarginCallDetail,
    MarginCallResolvedDetail,
    MarginLiquidationDetail,
)
from alphamind.portfolio_state.events.codec import decode_detail, encode_detail
from alphamind.portfolio_state.events.configuration import (
    DistillationConfigChange,
    DistillationConfigChangeDetail,
    ProfileSwitchedDetail,
)
from alphamind.portfolio_state.events.corporate_action import (
    CorporateActionAppliedDetail,
)
from alphamind.portfolio_state.events.distillation_anomaly import (
    DistillationAnomalyFlagDetail,
)
from alphamind.portfolio_state.events.order_lifecycle import (
    OrderCancelledDetail,
    OrderExpiredDetail,
    OrderFilledDetail,
    OrderModifiedDetail,
    OrderPartiallyFilledDetail,
    OrderRejectedDetail,
    OrderSubmittedDetail,
)
from alphamind.portfolio_state.events.pm_decision import (
    CommandAbandonedDetail,
    EnvelopeParseFailedDetail,
    EnvelopeRejectionDetail,
    PMDecisionDetail,
)
from alphamind.portfolio_state.events.position_lifecycle import (
    PositionAddedDetail,
    PositionClosedDetail,
    PositionOpenedDetail,
    PositionReducedDetail,
)
from alphamind.portfolio_state.events.reconciliation import (
    ReconciliationAlertDetail,
    ReconciliationCorrectionDetail,
)
from alphamind.portfolio_state.events.risk_guardrail import (
    EmergencyInvocationRequestedDetail,
    GreeksRefreshFailedDetail,
    GuardrailRejectionDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
    RiskLimitApproachedDetail,
    RiskParameterChangedDetail,
)
from alphamind.portfolio_state.events.thesis import (
    ThesisComponentAddedDetail,
    ThesisComponentUpdatedDetail,
    ThesisCreatedDetail,
    ThesisResolvedDetail,
    ThesisStatusChangedDetail,
)
from alphamind.portfolio_state.events.types import (
    ActivityLogEntry,
    BracketModificationSource,
    CashCreditReason,
    CashDebitReason,
    CorporateActionType,
    EventGroup,
    EventSource,
    EventType,
    OrderRejectionSource,
    PMVerdict,
    PositionExitMethod,
    PositionOpenMechanism,
)

# ---------------------------------------------------------------------------
# Dispatch dicts — derived from per-submodule registries (single source of
# truth). Hand-maintained dispatch was a structural defect — adding a new
# event type required edits to two parallel dicts. The per-submodule
# ``_REGISTRY`` ties each EventType to its detail class + group at definition
# time, then this aggregation reduces over the union.
# ---------------------------------------------------------------------------

_ALL_REGISTRIES: list[tuple[EventType, type, EventGroup]] = [
    *position_lifecycle._REGISTRY,
    *order_lifecycle._REGISTRY,
    *bracket._REGISTRY,
    *thesis._REGISTRY,
    *cash_margin._REGISTRY,
    *risk_guardrail._REGISTRY,
    *pm_decision._REGISTRY,
    *corporate_action._REGISTRY,
    *reconciliation._REGISTRY,
    *configuration._REGISTRY,
    *distillation_anomaly._REGISTRY,
]

EVENT_TYPE_TO_DETAIL_CLASS: dict[EventType, type] = {
    event_type: detail_class for event_type, detail_class, _ in _ALL_REGISTRIES
}

EVENT_TYPE_TO_GROUP: dict[EventType, EventGroup] = {
    event_type: group for event_type, _, group in _ALL_REGISTRIES
}


def build_activity_log_entry(  # noqa: PLR0913 — pure-core signature dictated by ALP-923 (the shared builder the OMS/analysis/operator-console emitters delegate to)
    *,
    invocation_id: str,
    event_type: EventType,
    position_id: str | None,
    order_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource,
    entry_id: str | None = None,
) -> ActivityLogEntry:
    """Build a typed ``ActivityLogEntry`` — the pure functional core.

    ``event_group`` is derived from the ``EVENT_TYPE_TO_GROUP`` registry so no
    caller has to hand-pass (and risk drifting) the group. ``entry_id`` defaults
    to the ``{invocation_id}-{event_type.value}-{uuid4.hex}`` token shared by the
    OMS / analysis / operator-console emitters; deterministic-idempotent and
    custom-token callers override it via the ``entry_id`` argument.

    Pure: no I/O, no SQL, no clock read (the caller supplies ``timestamp``). The
    sqlalchemy-free home (``portfolio_state/events``) lets every layer —
    distillation included — reach the builder on a legal downward import edge.
    """
    resolved_entry_id = (
        entry_id
        if entry_id is not None
        else f"{invocation_id}-{event_type.value}-{uuid.uuid4().hex}"
    )
    return ActivityLogEntry(
        entry_id=resolved_entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=event_type,
        event_group=EVENT_TYPE_TO_GROUP[event_type],
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=detail,
    )


# Exhaustiveness check at import time — the catalog must cover every
# EventType member exactly once. Catches accidental omissions in a new
# event-group submodule before they reach a writeback path.
_missing = set(EventType) - set(EVENT_TYPE_TO_DETAIL_CLASS)
if _missing:
    missing_names = sorted(member.name for member in _missing)
    msg = f"EVENT_TYPE_TO_DETAIL_CLASS missing entries for: {missing_names}"
    raise RuntimeError(msg)
del _missing
_duplicates = [
    event_type
    for event_type, _, _ in _ALL_REGISTRIES
    if sum(1 for et, _, _ in _ALL_REGISTRIES if et is event_type) > 1
]
if _duplicates:
    duplicate_names = sorted({d.name for d in _duplicates})
    msg = f"EventType registered in multiple submodules: {duplicate_names}"
    raise RuntimeError(msg)
del _duplicates


# ---------------------------------------------------------------------------
# AnyDetailType — type union covering every detail-payload class.
# ---------------------------------------------------------------------------

AnyDetailType = (
    PositionOpenedDetail
    | PositionClosedDetail
    | PositionAddedDetail
    | PositionReducedDetail
    | OrderSubmittedDetail
    | OrderFilledDetail
    | OrderPartiallyFilledDetail
    | OrderCancelledDetail
    | OrderExpiredDetail
    | OrderRejectedDetail
    | OrderModifiedDetail
    | BracketActivatedDetail
    | BracketCompletedDetail
    | BracketDissolvedDetail
    | BracketModifiedDetail
    | BracketIncompleteWarningDetail
    | BracketCancelledCorporateActionDetail
    | ThesisCreatedDetail
    | ThesisComponentAddedDetail
    | ThesisComponentUpdatedDetail
    | ThesisResolvedDetail
    | ThesisStatusChangedDetail
    | CashDebitedDetail
    | CashCreditedDetail
    | CapitalReservedDetail
    | CapitalReleasedDetail
    | MarginCallDetail
    | MarginCallResolvedDetail
    | MarginLiquidationDetail
    | BorrowCostAccruedDetail
    | GuardrailRejectionDetail
    | RiskLimitApproachedDetail
    | RiskParameterChangedDetail
    | EmergencyInvocationRequestedDetail
    | HaltActivatedDetail
    | HaltLiftedDetail
    | GreeksRefreshFailedDetail
    | PMDecisionDetail
    | CommandAbandonedDetail
    | EnvelopeParseFailedDetail
    | EnvelopeRejectionDetail
    | CorporateActionAppliedDetail
    | ReconciliationAlertDetail
    | ReconciliationCorrectionDetail
    | DistillationConfigChangeDetail
    | ProfileSwitchedDetail
    | DistillationAnomalyFlagDetail
)


__all__ = [
    "EVENT_TYPE_TO_DETAIL_CLASS",
    "EVENT_TYPE_TO_GROUP",
    "ActivityLogEntry",
    "AnyDetailType",
    "BorrowCostAccruedDetail",
    "BracketActivatedDetail",
    "BracketCancelledCorporateActionDetail",
    "BracketCompletedDetail",
    "BracketDissolvedDetail",
    "BracketIncompleteWarningDetail",
    "BracketModificationSource",
    "BracketModifiedDetail",
    "CapitalReleasedDetail",
    "CapitalReservedDetail",
    "CashCreditReason",
    "CashCreditedDetail",
    "CashDebitReason",
    "CashDebitedDetail",
    "CommandAbandonedDetail",
    "CorporateActionAppliedDetail",
    "CorporateActionType",
    "DistillationAnomalyFlagDetail",
    "DistillationConfigChange",
    "DistillationConfigChangeDetail",
    "EmergencyInvocationRequestedDetail",
    "EnvelopeParseFailedDetail",
    "EnvelopeRejectionDetail",
    "EventGroup",
    "EventSource",
    "EventType",
    "GreeksRefreshFailedDetail",
    "GuardrailRejectionDetail",
    "HaltActivatedDetail",
    "HaltLiftedDetail",
    "MarginCallDetail",
    "MarginCallResolvedDetail",
    "MarginLiquidationDetail",
    "OrderCancelledDetail",
    "OrderExpiredDetail",
    "OrderFilledDetail",
    "OrderModifiedDetail",
    "OrderPartiallyFilledDetail",
    "OrderRejectedDetail",
    "OrderRejectionSource",
    "OrderSubmittedDetail",
    "PMDecisionDetail",
    "PMVerdict",
    "PositionAddedDetail",
    "PositionClosedDetail",
    "PositionExitMethod",
    "PositionOpenMechanism",
    "PositionOpenedDetail",
    "PositionReducedDetail",
    "ProfileSwitchedDetail",
    "ReconciliationAlertDetail",
    "ReconciliationCorrectionDetail",
    "RiskLimitApproachedDetail",
    "RiskParameterChangedDetail",
    "ThesisComponentAddedDetail",
    "ThesisComponentUpdatedDetail",
    "ThesisCreatedDetail",
    "ThesisResolvedDetail",
    "ThesisStatusChangedDetail",
    "build_activity_log_entry",
    "decode_detail",
    "encode_detail",
]
