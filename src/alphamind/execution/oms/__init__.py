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
:mod:`submit_envelope_mcp` symbols are loaded lazily via :func:`__getattr__`
so that :mod:`alphamind.decision.portfolio_manager.models`'s import of
canonical OMS command types does not create a circular import (the
engine-stub depends on :class:`PMEnvelope`, which depends on canonical
command types).
"""

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
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

_LAZY_SUBMIT_ENVELOPE_MCP_NAMES = frozenset(
    {
        "Acknowledgment",
        "FailedSubmissionEntry",
        "RejectionPayload",
        "SubmissionLogEntry",
        "SubmissionResult",
        "SubmitEnvelopeState",
        "build_initial_submit_envelope_state",
        "build_submit_envelope_mcp_server",
        "get_failed_submission_log",
        "get_submission_log",
    }
)


def __getattr__(name: str) -> Any:
    """Lazy-load engine-stub symbols on first access.

    ``submit_envelope_mcp`` depends on :mod:`alphamind.decision.portfolio_manager`
    (Layer-2/3 validator + canonical envelope types), which itself depends
    on canonical OMS command models exported from this package. Eager
    import here would create a circular import. The engine-stub is loaded
    lazily on first attribute access; module identity is cached on the
    package after the first lookup.
    """
    if name in _LAZY_SUBMIT_ENVELOPE_MCP_NAMES:
        from alphamind.execution.oms import submit_envelope_mcp as _stub

        attr = getattr(_stub, name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
