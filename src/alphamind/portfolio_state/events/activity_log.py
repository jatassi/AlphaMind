"""Activity log records — typed entry and per-event-type detail-payload classes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Top-level enums
# ---------------------------------------------------------------------------


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

    # Risk and guardrail events
    GUARDRAIL_REJECTION = "GUARDRAIL_REJECTION"
    RISK_LIMIT_APPROACHED = "RISK_LIMIT_APPROACHED"
    RISK_PARAMETER_CHANGED = "RISK_PARAMETER_CHANGED"

    # PM decision events
    PM_DECISION = "PM_DECISION"
    COMMAND_ABANDONED = "COMMAND_ABANDONED"
    ENVELOPE_PARSE_FAILED = "ENVELOPE_PARSE_FAILED"

    # Corporate action events
    CORPORATE_ACTION_APPLIED = "CORPORATE_ACTION_APPLIED"

    # Configuration events
    DISTILLATION_CONFIG_CHANGE = "DISTILLATION_CONFIG_CHANGE"


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
    GUARDRAIL_LAYER = "GUARDRAIL_LAYER"
    CORPORATE_ACTION_PROCESSOR = "CORPORATE_ACTION_PROCESSOR"
    CONFIG_RELOAD = "CONFIG_RELOAD"
    OPERATOR_CONSOLE = "OPERATOR_CONSOLE"


class PositionExitMethod(StrEnum):
    """Exit method for a closed position."""

    STOP_TRIGGERED = "STOP_TRIGGERED"
    TARGET_REACHED = "TARGET_REACHED"
    PM_DECISION = "PM_DECISION"
    TIME_EXPIRED = "TIME_EXPIRED"
    MARGIN_LIQUIDATION = "MARGIN_LIQUIDATION"
    FORCED_BUY_IN = "FORCED_BUY_IN"
    CORPORATE_ACTION_CASH_MERGER = "CORPORATE_ACTION_CASH_MERGER"


class PositionOpenMechanism(StrEnum):
    """Mechanism by which a position was opened."""

    ORDER_FILL = "ORDER_FILL"
    SPIN_OFF_FROM_PARENT = "SPIN_OFF_FROM_PARENT"


class OrderRejectionSource(StrEnum):
    """Source of an order rejection."""

    GUARDRAIL = "GUARDRAIL"
    BROKER = "BROKER"


class CashDebitReason(StrEnum):
    """Reason for a cash debit event."""

    ENTRY_FILL = "ENTRY_FILL"
    FEES = "FEES"
    CASH_DIVIDEND_SHORT_OBLIGATION = "CASH_DIVIDEND_SHORT_OBLIGATION"


class CashCreditReason(StrEnum):
    """Reason for a cash credit event."""

    EXIT_FILL = "EXIT_FILL"
    CASH_DIVIDEND_LONG = "CASH_DIVIDEND_LONG"
    FRACTIONAL_SHARE_CASH_OUT = "FRACTIONAL_SHARE_CASH_OUT"
    CASH_MERGER_PROCEEDS = "CASH_MERGER_PROCEEDS"


class CorporateActionType(StrEnum):
    """Per-action-type matrix from corporate-actions.md."""

    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    CASH_DIVIDEND_LONG = "CASH_DIVIDEND_LONG"
    CASH_DIVIDEND_SHORT = "CASH_DIVIDEND_SHORT"
    CASH_MERGER = "CASH_MERGER"
    STOCK_MERGER = "STOCK_MERGER"
    SPIN_OFF = "SPIN_OFF"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


class PMVerdict(StrEnum):
    """Verdict from a PM decision."""

    APPROVE = "APPROVE"
    APPROVE_WITH_MODIFICATION = "APPROVE_WITH_MODIFICATION"
    REJECT = "REJECT"


class BracketModificationSource(StrEnum):
    """Source of a bracket modification."""

    PM = "PM"
    FILL_ANCHOR_RECALCULATION = "FILL_ANCHOR_RECALCULATION"
    CORPORATE_ACTION_ADJUSTMENT = "CORPORATE_ACTION_ADJUSTMENT"


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Position lifecycle
# ---------------------------------------------------------------------------


class PositionOpenedDetail(BaseModel):
    """Detail payload for POSITION_OPENED events."""

    model_config = {"frozen": True}

    ticker: str
    direction: str
    fill_price: float
    quantity: float
    thesis_id: str | None
    bracket_id: str | None
    mechanism: PositionOpenMechanism
    parent_position_id: str | None

    @model_validator(mode="after")
    def _validate_parent_position_id(self) -> PositionOpenedDetail:
        if (
            self.mechanism == PositionOpenMechanism.ORDER_FILL
            and self.parent_position_id is not None
        ):
            msg = "parent_position_id must be None when mechanism is ORDER_FILL"
            raise ValueError(msg)
        return self


class PositionClosedDetail(BaseModel):
    """Detail payload for POSITION_CLOSED events."""

    model_config = {"frozen": True}

    exit_method: PositionExitMethod
    exit_price: float
    realized_pnl_usd: float
    thesis_resolution_category: str


class PositionAddedDetail(BaseModel):
    """Detail payload for POSITION_ADDED events."""

    model_config = {"frozen": True}

    additional_quantity: float
    new_average_cost_basis: float
    addition_thesis_component_id: str


class PositionReducedDetail(BaseModel):
    """Detail payload for POSITION_REDUCED events."""

    model_config = {"frozen": True}

    reduced_quantity: float
    partial_realized_pnl_usd: float
    close_rationale_classification: str


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Order lifecycle
# ---------------------------------------------------------------------------


class OrderSubmittedDetail(BaseModel):
    """Detail payload for ORDER_SUBMITTED events."""

    model_config = {"frozen": True}

    order_parameters_json: dict[str, Any]
    pm_command_id: str


class OrderFilledDetail(BaseModel):
    """Detail payload for ORDER_FILLED events."""

    model_config = {"frozen": True}

    fill_price: float
    fill_quantity: float
    slippage: float
    fees: float


class OrderPartiallyFilledDetail(BaseModel):
    """Detail payload for ORDER_PARTIALLY_FILLED events."""

    model_config = {"frozen": True}

    fill_price: float
    fill_quantity: float
    remaining_quantity: float


class OrderCancelledDetail(BaseModel):
    """Detail payload for ORDER_CANCELLED events."""

    model_config = {"frozen": True}

    cancel_reason: str
    filled_quantity_at_cancellation: int


class OrderExpiredDetail(BaseModel):
    """Detail payload for ORDER_EXPIRED events."""

    model_config = {"frozen": True}

    filled_quantity_at_expiration: int


class OrderRejectedDetail(BaseModel):
    """Detail payload for ORDER_REJECTED events."""

    model_config = {"frozen": True}

    rejection_reason: str
    rejection_source: OrderRejectionSource


class OrderModifiedDetail(BaseModel):
    """Detail payload for ORDER_MODIFIED events."""

    model_config = {"frozen": True}

    field_changed: str
    old_value: str
    new_value: str
    pm_rationale: str


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Bracket events
# ---------------------------------------------------------------------------


class BracketActivatedDetail(BaseModel):
    """Detail payload for BRACKET_ACTIVATED events."""

    model_config = {"frozen": True}

    bracket_id: str
    protective_leg_order_ids: tuple[str, ...]


class BracketCompletedDetail(BaseModel):
    """Detail payload for BRACKET_COMPLETED events."""

    model_config = {"frozen": True}

    triggered_leg_id: str
    fill_details_json: dict[str, Any]


class BracketDissolvedDetail(BaseModel):
    """Detail payload for BRACKET_DISSOLVED events."""

    model_config = {"frozen": True}

    cancelled_leg_order_ids: tuple[str, ...]


class BracketModifiedDetail(BaseModel):
    """Detail payload for BRACKET_MODIFIED events."""

    model_config = {"frozen": True}

    source: BracketModificationSource
    field_changed: str
    old_value: str
    new_value: str
    rationale: str | None

    @model_validator(mode="after")
    def _validate_rationale(self) -> BracketModifiedDetail:
        if (
            self.source != BracketModificationSource.FILL_ANCHOR_RECALCULATION
            and self.rationale is None
        ):
            msg = "rationale must be non-None when source is not FILL_ANCHOR_RECALCULATION"
            raise ValueError(msg)
        return self


class BracketIncompleteWarningDetail(BaseModel):
    """Detail payload for BRACKET_INCOMPLETE_WARNING events."""

    model_config = {"frozen": True}

    missing_leg_types: tuple[str, ...]
    expected_resolution: str


class BracketCancelledCorporateActionDetail(BaseModel):
    """Detail payload for BRACKET_CANCELLED_CORPORATE_ACTION events."""

    model_config = {"frozen": True}

    bracket_id: str
    cancellation_reason: str
    cancelled_leg_order_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Thesis events
# ---------------------------------------------------------------------------


class ThesisCreatedDetail(BaseModel):
    """Detail payload for THESIS_CREATED events."""

    model_config = {"frozen": True}

    thesis_id: str
    summary: str


class ThesisComponentAddedDetail(BaseModel):
    """Detail payload for THESIS_COMPONENT_ADDED events."""

    model_config = {"frozen": True}

    component_id: str
    component_type: str


class ThesisComponentUpdatedDetail(BaseModel):
    """Detail payload for THESIS_COMPONENT_UPDATED events."""

    model_config = {"frozen": True}

    component_id: str
    field_changed: str
    old_value: str
    new_value: str


class ThesisResolvedDetail(BaseModel):
    """Detail payload for THESIS_RESOLVED events."""

    model_config = {"frozen": True}

    resolution_category: str
    component_outcomes_json: dict[str, str]


class ThesisStatusChangedDetail(BaseModel):
    """Detail payload for THESIS_STATUS_CHANGED events."""

    model_config = {"frozen": True}

    old_status: str
    new_status: str


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Cash and margin events
# ---------------------------------------------------------------------------


class CashDebitedDetail(BaseModel):
    """Detail payload for CASH_DEBITED events."""

    model_config = {"frozen": True}

    amount_usd: float
    reason: CashDebitReason
    new_balance_usd: float


class CashCreditedDetail(BaseModel):
    """Detail payload for CASH_CREDITED events."""

    model_config = {"frozen": True}

    amount_usd: float
    reason: CashCreditReason
    new_balance_usd: float


class CapitalReservedDetail(BaseModel):
    """Detail payload for CAPITAL_RESERVED events."""

    model_config = {"frozen": True}

    order_id: str
    amount_usd: float


class CapitalReleasedDetail(BaseModel):
    """Detail payload for CAPITAL_RELEASED events."""

    model_config = {"frozen": True}

    order_id: str
    amount_usd: float


class MarginCallDetail(BaseModel):
    """Detail payload for MARGIN_CALL events."""

    model_config = {"frozen": True}

    position_id: str
    margin_required_usd: float
    margin_available_usd: float
    deficit_usd: float


class MarginCallResolvedDetail(BaseModel):
    """Detail payload for MARGIN_CALL_RESOLVED events."""

    model_config = {"frozen": True}

    resolution_method: str


class MarginLiquidationDetail(BaseModel):
    """Detail payload for MARGIN_LIQUIDATION events."""

    model_config = {"frozen": True}

    position_id: str
    liquidation_price: float
    loss_usd: float


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Risk and guardrail events
# ---------------------------------------------------------------------------


class GuardrailRejectionDetail(BaseModel):
    """Detail payload for GUARDRAIL_REJECTION events."""

    model_config = {"frozen": True}

    command_summary: str
    blocking_rule_ids: tuple[str, ...]
    current_limit_values_json: dict[str, float]
    headroom_json: dict[str, float]
    suggested_modification: str | None


class RiskLimitApproachedDetail(BaseModel):
    """Detail payload for RISK_LIMIT_APPROACHED events."""

    model_config = {"frozen": True}

    metric_id: str
    current_value: float
    threshold_value: float
    limit_value: float


class RiskParameterChangedDetail(BaseModel):
    """Detail payload for RISK_PARAMETER_CHANGED events."""

    model_config = {"frozen": True}

    old_parameter_set_json: dict[str, Any]
    new_parameter_set_json: dict[str, Any]
    regime_label: str


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — PM decision events
# ---------------------------------------------------------------------------


class PMDecisionDetail(BaseModel):
    """Detail payload for PM_DECISION events."""

    model_config = {"frozen": True}

    envelope_id: str
    source_provenance_json: dict[str, Any]
    evaluation_json: dict[str, Any]
    modifications_json: list[dict[str, Any]]
    resulting_command_ids: tuple[str, ...]
    verdict: PMVerdict


class CommandAbandonedDetail(BaseModel):
    """Detail payload for COMMAND_ABANDONED events."""

    model_config = {"frozen": True}

    envelope_id: str
    command_id: str
    originating_agent: str
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
    failure_reason: str
    retry_attempt_count: int


class EnvelopeParseFailedDetail(BaseModel):
    """Detail payload for ENVELOPE_PARSE_FAILED events.

    Persists the Layer-1 (Pydantic) parse failure surfaced by the
    ``submit_envelope`` MCP wrapper before any per-command processing runs.
    Mirrors the in-memory ``FailedSubmissionEntry`` so the audit trail
    survives process restart.
    """

    model_config = {"frozen": True}

    attempted_envelope_id: str
    attempted_command_id: str
    validation_error_repr: str
    raw_args_json: str


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Configuration events
# ---------------------------------------------------------------------------


class DistillationConfigChange(BaseModel):
    """One per-key change inside a ``DistillationConfigChangeDetail.changes`` tuple.

    ``key_path`` is the dotted path matching the ``DistillationConfig`` field
    structure (for example ``anomaly_detection.volume_anomaly_sigma``).
    ``old_value`` and ``new_value`` carry whatever the Pydantic JSON dump
    produced — scalars, ints, floats, bools, or string-valued enums.
    """

    model_config = {"frozen": True}

    key_path: str = Field(min_length=1)
    old_value: Any
    new_value: Any


class DistillationConfigChangeDetail(BaseModel):
    """Detail payload for ``DISTILLATION_CONFIG_CHANGE`` events.

    Emitted by the configuration loader at the start of an invocation when the
    reloaded ``DistillationConfig`` differs from the prior reload, or when no
    prior reload exists. Identical reloads produce no entry — see
    ``build_distillation_config_change_entry`` in
    ``alphamind.portfolio_state.computations.activity_log``.
    """

    model_config = {"frozen": True}

    config_file: str = Field(default="config/distillation.yaml", min_length=1)
    prior_hash: str | None
    new_hash: str = Field(min_length=1)
    changes: tuple[DistillationConfigChange, ...]
    git_sha: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_changes_sorted(self) -> DistillationConfigChangeDetail:
        for prev, curr in zip(self.changes, self.changes[1:], strict=False):
            if prev.key_path >= curr.key_path:
                msg = (
                    "changes must be sorted by key_path ascending "
                    f"(got {prev.key_path!r} then {curr.key_path!r})"
                )
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Per-event-type detail-payload classes — Corporate action events
# ---------------------------------------------------------------------------


class CorporateActionAppliedDetail(BaseModel):
    """Detail payload for CORPORATE_ACTION_APPLIED events."""

    model_config = {"frozen": True}

    action_type: CorporateActionType
    alpaca_activity_id: str
    ticker: str
    new_ticker: str | None
    ratio_or_amount: float
    pre_action_quantity: float
    post_action_quantity: float
    pre_action_cost_basis: float
    post_action_cost_basis: float
    signed_cash_impact_usd: float
    parent_position_id: str | None
    resulting_position_status: str


# ---------------------------------------------------------------------------
# Type alias — covers all 35 detail-payload classes
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
    | GuardrailRejectionDetail
    | RiskLimitApproachedDetail
    | RiskParameterChangedDetail
    | PMDecisionDetail
    | CommandAbandonedDetail
    | EnvelopeParseFailedDetail
    | CorporateActionAppliedDetail
    | DistillationConfigChangeDetail
)

# ---------------------------------------------------------------------------
# Module-level static mappings
# ---------------------------------------------------------------------------

EVENT_TYPE_TO_DETAIL_CLASS: dict[EventType, type] = {
    EventType.POSITION_OPENED: PositionOpenedDetail,
    EventType.POSITION_CLOSED: PositionClosedDetail,
    EventType.POSITION_ADDED: PositionAddedDetail,
    EventType.POSITION_REDUCED: PositionReducedDetail,
    EventType.ORDER_SUBMITTED: OrderSubmittedDetail,
    EventType.ORDER_FILLED: OrderFilledDetail,
    EventType.ORDER_PARTIALLY_FILLED: OrderPartiallyFilledDetail,
    EventType.ORDER_CANCELLED: OrderCancelledDetail,
    EventType.ORDER_EXPIRED: OrderExpiredDetail,
    EventType.ORDER_REJECTED: OrderRejectedDetail,
    EventType.ORDER_MODIFIED: OrderModifiedDetail,
    EventType.BRACKET_ACTIVATED: BracketActivatedDetail,
    EventType.BRACKET_COMPLETED: BracketCompletedDetail,
    EventType.BRACKET_DISSOLVED: BracketDissolvedDetail,
    EventType.BRACKET_MODIFIED: BracketModifiedDetail,
    EventType.BRACKET_INCOMPLETE_WARNING: BracketIncompleteWarningDetail,
    EventType.BRACKET_CANCELLED_CORPORATE_ACTION: BracketCancelledCorporateActionDetail,
    EventType.THESIS_CREATED: ThesisCreatedDetail,
    EventType.THESIS_COMPONENT_ADDED: ThesisComponentAddedDetail,
    EventType.THESIS_COMPONENT_UPDATED: ThesisComponentUpdatedDetail,
    EventType.THESIS_RESOLVED: ThesisResolvedDetail,
    EventType.THESIS_STATUS_CHANGED: ThesisStatusChangedDetail,
    EventType.CASH_DEBITED: CashDebitedDetail,
    EventType.CASH_CREDITED: CashCreditedDetail,
    EventType.CAPITAL_RESERVED: CapitalReservedDetail,
    EventType.CAPITAL_RELEASED: CapitalReleasedDetail,
    EventType.MARGIN_CALL: MarginCallDetail,
    EventType.MARGIN_CALL_RESOLVED: MarginCallResolvedDetail,
    EventType.MARGIN_LIQUIDATION: MarginLiquidationDetail,
    EventType.GUARDRAIL_REJECTION: GuardrailRejectionDetail,
    EventType.RISK_LIMIT_APPROACHED: RiskLimitApproachedDetail,
    EventType.RISK_PARAMETER_CHANGED: RiskParameterChangedDetail,
    EventType.PM_DECISION: PMDecisionDetail,
    EventType.COMMAND_ABANDONED: CommandAbandonedDetail,
    EventType.ENVELOPE_PARSE_FAILED: EnvelopeParseFailedDetail,
    EventType.CORPORATE_ACTION_APPLIED: CorporateActionAppliedDetail,
    EventType.DISTILLATION_CONFIG_CHANGE: DistillationConfigChangeDetail,
}

EVENT_TYPE_TO_GROUP: dict[EventType, EventGroup] = {
    EventType.POSITION_OPENED: EventGroup.POSITION_LIFECYCLE,
    EventType.POSITION_CLOSED: EventGroup.POSITION_LIFECYCLE,
    EventType.POSITION_ADDED: EventGroup.POSITION_LIFECYCLE,
    EventType.POSITION_REDUCED: EventGroup.POSITION_LIFECYCLE,
    EventType.ORDER_SUBMITTED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_FILLED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_PARTIALLY_FILLED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_CANCELLED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_EXPIRED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_REJECTED: EventGroup.ORDER_LIFECYCLE,
    EventType.ORDER_MODIFIED: EventGroup.ORDER_LIFECYCLE,
    EventType.BRACKET_ACTIVATED: EventGroup.BRACKET,
    EventType.BRACKET_COMPLETED: EventGroup.BRACKET,
    EventType.BRACKET_DISSOLVED: EventGroup.BRACKET,
    EventType.BRACKET_MODIFIED: EventGroup.BRACKET,
    EventType.BRACKET_INCOMPLETE_WARNING: EventGroup.BRACKET,
    EventType.BRACKET_CANCELLED_CORPORATE_ACTION: EventGroup.BRACKET,
    EventType.THESIS_CREATED: EventGroup.THESIS,
    EventType.THESIS_COMPONENT_ADDED: EventGroup.THESIS,
    EventType.THESIS_COMPONENT_UPDATED: EventGroup.THESIS,
    EventType.THESIS_RESOLVED: EventGroup.THESIS,
    EventType.THESIS_STATUS_CHANGED: EventGroup.THESIS,
    EventType.CASH_DEBITED: EventGroup.CASH_AND_MARGIN,
    EventType.CASH_CREDITED: EventGroup.CASH_AND_MARGIN,
    EventType.CAPITAL_RESERVED: EventGroup.CASH_AND_MARGIN,
    EventType.CAPITAL_RELEASED: EventGroup.CASH_AND_MARGIN,
    EventType.MARGIN_CALL: EventGroup.CASH_AND_MARGIN,
    EventType.MARGIN_CALL_RESOLVED: EventGroup.CASH_AND_MARGIN,
    EventType.MARGIN_LIQUIDATION: EventGroup.CASH_AND_MARGIN,
    EventType.GUARDRAIL_REJECTION: EventGroup.RISK_AND_GUARDRAIL,
    EventType.RISK_LIMIT_APPROACHED: EventGroup.RISK_AND_GUARDRAIL,
    EventType.RISK_PARAMETER_CHANGED: EventGroup.RISK_AND_GUARDRAIL,
    EventType.PM_DECISION: EventGroup.PM_DECISION,
    EventType.COMMAND_ABANDONED: EventGroup.PM_DECISION,
    EventType.ENVELOPE_PARSE_FAILED: EventGroup.PM_DECISION,
    EventType.CORPORATE_ACTION_APPLIED: EventGroup.CORPORATE_ACTION,
    EventType.DISTILLATION_CONFIG_CHANGE: EventGroup.CONFIGURATION,
}

# ---------------------------------------------------------------------------
# ActivityLogEntry — the wrapping record
# ---------------------------------------------------------------------------


class ActivityLogEntry(BaseModel):
    """A single entry in the portfolio activity log."""

    model_config = {"frozen": True}

    entry_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    timestamp: datetime
    event_type: EventType
    event_group: EventGroup
    position_id: str | None
    order_id: str | None
    thesis_id: str | None
    source: EventSource
    detail: Any

    @model_validator(mode="after")
    def _validate_timestamp_tz_aware(self) -> ActivityLogEntry:
        if self.timestamp.tzinfo is None:
            msg = "timestamp must be tz-aware UTC"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_event_group(self) -> ActivityLogEntry:
        expected = EVENT_TYPE_TO_GROUP[self.event_type]
        if self.event_group != expected:
            msg = (
                f"event_group mismatch: event_type={self.event_type!r} expects "
                f"{expected!r} but got {self.event_group!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_detail_class(self) -> ActivityLogEntry:
        expected_class = EVENT_TYPE_TO_DETAIL_CLASS[self.event_type]
        actual_class = type(self.detail)
        if actual_class is not expected_class:
            msg = (
                f"detail class mismatch: event_type={self.event_type!r} expects "
                f"{expected_class.__name__} but got {actual_class.__name__}"
            )
            raise ValueError(msg)
        return self
