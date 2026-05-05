"""Engine-stub OMS package — story 06c (ALP-328).

Public surface for the ``submit_envelope`` MCP wrapper. The wrapper itself is
a transitional engine-stub; see :mod:`alphamind.execution.oms.submit_envelope_mcp`.
"""

from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    RejectionPayload,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
    build_initial_submit_envelope_state,
    build_submit_envelope_mcp_server,
    get_submission_log,
)

__all__ = [
    "Acknowledgment",
    "RejectionPayload",
    "SubmissionLogEntry",
    "SubmissionResult",
    "SubmitEnvelopeState",
    "build_initial_submit_envelope_state",
    "build_submit_envelope_mcp_server",
    "get_submission_log",
]
