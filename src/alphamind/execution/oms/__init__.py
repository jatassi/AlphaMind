"""OMS package — engine-stub MCP wrapper plus canonical command models.

The :mod:`~alphamind.execution.oms.command_models` module (story 01a / ALP-370)
holds the broker-grade Pydantic translation of
``docs/design/05-execution-layer/oms-command-schema.md`` — the discriminated
union over OPEN / CLOSE / ADJUST / CANCEL / ADD.

The :mod:`~alphamind.execution.oms.command_ids` module (story 01b / ALP-371)
provides the canonical command-ID derivation utility (PM-originated and
engine-originated).

The :mod:`~alphamind.execution.oms.engine_envelope` module (story 02a /
ALP-372) holds the canonical Pydantic translation of
``docs/design/05-execution-layer/engine-envelope-schema.md`` — the
:class:`EngineEnvelope` plus its embedded
:class:`GuardrailTriggerRecord` / :class:`BreachDetails` /
:class:`SecondaryBreachCheckResult` sub-records.

The engine-stub :mod:`~alphamind.execution.oms.submit_envelope_mcp` module
(story 06c / ALP-328) is the transitional MCP wrapper around the OMS write
API; its public surface is re-exported here for backwards compatibility.
"""

from alphamind.execution.oms.command_ids import (
    EngineCommandIdComponents,
    PMCommandIdComponents,
    compute_attempt_seq,
    derive_engine_command_id,
    derive_pm_command_id,
    is_engine_originated,
    is_pm_originated,
    parse_engine_command_id,
    parse_pm_command_id,
)
from alphamind.execution.oms.command_models import (
    AddCommand,
    AdjustCommand,
    AssetType,
    BracketAdjustment,
    BracketOrderParameters,
    BracketOrderType,
    CancelCommand,
    CloseCommand,
    CloseRationaleType,
    CommandType,
    Comparator,
    ComponentType,
    ContractType,
    Direction,
    EntryOrder,
    EntryOrderType,
    EquityInstrument,
    EventCondition,
    EventLeg,
    Instrument,
    InvalidationLeg,
    LegType,
    NewEventInvalidation,
    NewStopLevel,
    NewTargetLevel,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    RiskManagementSubtype,
    StrategyInstrument,
    StrategyLeg,
    StrategyType,
    Target,
    TargetType,
    Thesis,
    ThesisComponent,
    TimeCondition,
    TimeLeg,
    oms_command_schema,
)
from alphamind.execution.oms.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
    SecondaryBreachCheckResult,
    SecondaryBreachResult,
    SourceProvenance,
    engine_envelope_schema,
)
from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    FailedSubmissionEntry,
    RejectionPayload,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
    build_initial_submit_envelope_state,
    build_submit_envelope_mcp_server,
    get_failed_submission_log,
    get_submission_log,
)

__all__ = [
    "Acknowledgment",
    "AddCommand",
    "AdjustCommand",
    "AssetType",
    "BracketAdjustment",
    "BracketOrderParameters",
    "BracketOrderType",
    "BreachDetails",
    "CancelCommand",
    "CloseCommand",
    "CloseRationaleType",
    "CommandType",
    "Comparator",
    "ComponentType",
    "ContractType",
    "Direction",
    "EngineCommandIdComponents",
    "EngineEnvelope",
    "EntryOrder",
    "EntryOrderType",
    "EquityInstrument",
    "EventCondition",
    "EventLeg",
    "FailedSubmissionEntry",
    "GuardrailTriggerRecord",
    "Instrument",
    "InvalidationLeg",
    "LegType",
    "NewEventInvalidation",
    "NewStopLevel",
    "NewTargetLevel",
    "OMSCommand",
    "OpenCommand",
    "OptionInstrument",
    "PMCommandIdComponents",
    "PositionSize",
    "PriceCondition",
    "PriceLeg",
    "RejectionPayload",
    "RiskManagementSubtype",
    "SecondaryBreachCheckResult",
    "SecondaryBreachResult",
    "SourceProvenance",
    "StrategyInstrument",
    "StrategyLeg",
    "StrategyType",
    "SubmissionLogEntry",
    "SubmissionResult",
    "SubmitEnvelopeState",
    "Target",
    "TargetType",
    "Thesis",
    "ThesisComponent",
    "TimeCondition",
    "TimeLeg",
    "build_initial_submit_envelope_state",
    "build_submit_envelope_mcp_server",
    "compute_attempt_seq",
    "derive_engine_command_id",
    "derive_pm_command_id",
    "engine_envelope_schema",
    "get_failed_submission_log",
    "get_submission_log",
    "is_engine_originated",
    "is_pm_originated",
    "oms_command_schema",
    "parse_engine_command_id",
    "parse_pm_command_id",
]
