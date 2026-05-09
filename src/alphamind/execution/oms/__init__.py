"""Engine-stub OMS package — story 06c (ALP-328).

Public surface for the ``submit_envelope`` MCP wrapper. The wrapper itself is
a transitional engine-stub; see :mod:`alphamind.execution.oms.submit_envelope_mcp`.

Also exports the canonical OMS command-ID derivation utility — story 01b
(ALP-371); see :mod:`alphamind.execution.oms.command_ids`.
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
    "EngineCommandIdComponents",
    "FailedSubmissionEntry",
    "PMCommandIdComponents",
    "RejectionPayload",
    "SubmissionLogEntry",
    "SubmissionResult",
    "SubmitEnvelopeState",
    "build_initial_submit_envelope_state",
    "build_submit_envelope_mcp_server",
    "compute_attempt_seq",
    "derive_engine_command_id",
    "derive_pm_command_id",
    "get_failed_submission_log",
    "get_submission_log",
    "is_engine_originated",
    "is_pm_originated",
    "parse_engine_command_id",
    "parse_pm_command_id",
]
