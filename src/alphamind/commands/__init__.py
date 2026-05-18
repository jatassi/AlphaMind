"""LLM↔engine wire-format kernel — the commands/ package.

A top-level package below the decision and execution layers carrying the
shared command/envelope contract types and the injection-seam Protocols
that decouple them. Both ``decision/`` and ``execution/`` import
``commands/*`` downward; neither imports the other.

The package hosts:

* Canonical OMS command discriminated union and sub-records
  (:mod:`~alphamind.commands.command_models`) — the typed Pydantic
  translation of ``docs/design/05-execution-layer/oms-command-schema.md``.
* The engine-originated envelope shape
  (:mod:`~alphamind.commands.engine_envelope`) — Pydantic for
  ``docs/design/05-execution-layer/engine-envelope-schema.md``.
* The PM-originated envelope shape and completion sentinel
  (:mod:`~alphamind.commands.pm_envelope`) — Pydantic for
  ``docs/design/04-decision-layer/pm-envelope-schema.md``.
* Per-command submission-result shapes
  (:mod:`~alphamind.commands.submission_results`) — the wire contract
  ``submit_envelope`` returns, identical for stub and real-engine paths.
* Submission-log entry types
  (:mod:`~alphamind.commands.submission_log`) — what the submit_envelope
  wrapper records per call for downstream Phase 2 writeback.
* Validation-result wire types
  (:mod:`~alphamind.commands.validation_results`) — Layer-2/3 violations
  and warnings that ride from the validator into persistence.
* The injection-seam Protocol
  (:mod:`~alphamind.commands.protocols`) — ``BrokerDispatch``.

Per ALP-458, every module under ``alphamind.commands.*`` imports zero
first-party ``alphamind.*`` modules outside ``alphamind._kernel.*`` and
``alphamind.commands.*`` itself — the package is the decoupling kernel.
"""

from alphamind.commands.command_models import (
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
from alphamind.commands.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
    SecondaryBreachCheckResult,
    SecondaryBreachResult,
    engine_envelope_schema,
)
from alphamind.commands.engine_envelope import (
    SourceProvenance as EngineSourceProvenance,
)
from alphamind.commands.pm_envelope import (
    AdjustmentCategory,
    AntiPattern,
    ConcernRecord,
    CriterionAssessment,
    ModificationRecord,
    PMAnalystEnvelope,
    PMCompletionRecord,
    PMEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    RecommendationType,
    ThesisQualityEvaluation,
    Verdict,
    VerdictSummary,
    completion_record_schema,
    envelope_schema,
)
from alphamind.commands.pm_envelope import (
    SourceProvenance as PMSourceProvenance,
)
from alphamind.commands.protocols import (
    BrokerDispatch,
)
from alphamind.commands.submission_log import (
    FailedSubmissionEntry,
    SubmissionLogEntry,
)
from alphamind.commands.submission_results import (
    Acknowledgment,
    GreeksLike,
    RejectionPayload,
    SubmissionResult,
)
from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)

__all__ = [
    "Acknowledgment",
    "AddCommand",
    "AdjustCommand",
    "AdjustmentCategory",
    "AntiPattern",
    "AssetType",
    "BracketAdjustment",
    "BracketOrderParameters",
    "BracketOrderType",
    "BreachDetails",
    "BrokerDispatch",
    "CancelCommand",
    "CloseCommand",
    "CloseRationaleType",
    "CommandType",
    "Comparator",
    "ComponentType",
    "ConcernRecord",
    "ContractType",
    "CriterionAssessment",
    "Direction",
    "EngineEnvelope",
    "EngineSourceProvenance",
    "EntryOrder",
    "EntryOrderType",
    "EquityInstrument",
    "EventCondition",
    "EventLeg",
    "FailedSubmissionEntry",
    "GreeksLike",
    "GuardrailTriggerRecord",
    "Instrument",
    "InvalidationLeg",
    "LegType",
    "ModificationRecord",
    "NewEventInvalidation",
    "NewStopLevel",
    "NewTargetLevel",
    "OMSCommand",
    "OpenCommand",
    "OptionInstrument",
    "PMAnalystEnvelope",
    "PMCompletionRecord",
    "PMEnvelope",
    "PMSourceProvenance",
    "PMStrategistEnvelope",
    "PositionActionEvaluation",
    "PositionSize",
    "PriceCondition",
    "PriceLeg",
    "RecommendationType",
    "RejectionPayload",
    "RiskManagementSubtype",
    "SecondaryBreachCheckResult",
    "SecondaryBreachResult",
    "StrategyInstrument",
    "StrategyLeg",
    "StrategyType",
    "SubmissionLogEntry",
    "SubmissionResult",
    "Target",
    "TargetType",
    "Thesis",
    "ThesisComponent",
    "ThesisQualityEvaluation",
    "TimeCondition",
    "TimeLeg",
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "Verdict",
    "VerdictSummary",
    "completion_record_schema",
    "engine_envelope_schema",
    "envelope_schema",
    "oms_command_schema",
]
