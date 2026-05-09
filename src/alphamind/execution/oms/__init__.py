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

The :mod:`~alphamind.execution.oms.submit_engine_envelope` module (story 04 /
ALP-375) provides the monitor-facing :func:`submit_engine_envelope` write
function the continuous monitor calls between invocations to persist a
protective CLOSE. Symbols are loaded lazily via :func:`__getattr__` for the
same reason as the engine-stub module.

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
    from alphamind.execution.oms.submit_engine_envelope import (
        SubmitEngineEnvelopeState,
        build_initial_submit_engine_envelope_state,
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

_LAZY_SUBMIT_ENGINE_ENVELOPE_NAMES = frozenset(
    {
        "SubmitEngineEnvelopeState",
        "build_initial_submit_engine_envelope_state",
    }
)


def __getattr__(name: str) -> Any:
    """Lazy-load engine-stub and engine-envelope-submit symbols on first access.

    ``submit_envelope_mcp`` depends on :mod:`alphamind.decision.portfolio_manager`
    (Layer-2/3 validator + canonical envelope types), which itself depends
    on canonical OMS command models exported from this package. Eager
    import here would create a circular import. ``submit_engine_envelope``
    transitively imports ``submit_envelope_mcp`` for the
    :class:`SubmissionResult` / :class:`Acknowledgment` shapes, so it
    inherits the same lazy-load discipline. Modules are loaded lazily on
    first attribute access; module identity is cached on the package after
    the first lookup.

    The ``importlib.import_module`` path is intentional — a ``from … import``
    statement re-enters ``__getattr__`` for the submodule name and recurses
    forever; ``import_module`` looks up the submodule directly on
    ``sys.modules`` after the import completes.
    """
    import importlib

    if name in _LAZY_SUBMIT_ENVELOPE_MCP_NAMES:
        _stub = importlib.import_module("alphamind.execution.oms.submit_envelope_mcp")
        attr = getattr(_stub, name)
        globals()[name] = attr
        return attr
    if name in _LAZY_SUBMIT_ENGINE_ENVELOPE_NAMES:
        _engine = importlib.import_module("alphamind.execution.oms.submit_engine_envelope")
        attr = getattr(_engine, name)
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
    "SubmitEngineEnvelopeState",
    "SubmitEnvelopeState",
    "Target",
    "TargetType",
    "Thesis",
    "ThesisComponent",
    "TimeCondition",
    "TimeLeg",
    "build_initial_submit_engine_envelope_state",
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
