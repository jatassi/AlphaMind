"""Engine-stub ``submit_envelope`` MCP wrapper package — ALP-464 decomposition of ALP-328.

This package is **transitional**. It is the engine-stub the PM tool-use loop
calls during decision-layer invocations until the real OMS submission engine
lands in `ALP-120 <https://linear.app/alphamind-jatassi/issue/ALP-120>`_ and
the persistence layer in `ALP-119 <https://linear.app/alphamind-jatassi/issue/ALP-119>`_.
When those work trees ship, this package is replaced in a coordinated edit.

The wrapper accepts one PM envelope per tool call. It coerces the input dict
to :class:`PMEnvelope` (Layer-1 schema), runs :func:`validate_pm_envelope`
(Layer-2/3, story 06b), then re-runs :func:`validate_guardrail` per embedded
command against cumulative state held in a mutable
:class:`SubmitEnvelopeState` cell. PASS commands advance the cell via
``state.with_accepted_proposal(delta)``; FAIL commands produce a
``rejection_payload`` shaped per ``breach-behavior.md`` § Hard rejection
semantics. Every call (envelope-level rejection or per-command result list)
is appended to the submission log accessible via :func:`get_submission_log`.

Mirrors the analyst's :mod:`alphamind.risk_guardrails.state_delivery.validation_tool_mcp`
factory pattern: per-invocation server, mutable state cell captured by the tool
closure, JSON content blocks for response.

Relocated from ``execution.oms.submit_envelope_mcp`` to
``decision.portfolio_manager.submit_envelope`` by ALP-458 to break the
decision↔execution import cycle. Decomposed into five submodules
(``types``, ``process``, ``dispatch``, ``persist``, ``server``) by ALP-464.
The wire-format result shapes (:class:`Acknowledgment`,
:class:`RejectionPayload`, :class:`SubmissionResult`, etc.) and the engine-side
envelope contract types live in :mod:`alphamind.commands.*`; this package
imports them downward like every other consumer.
"""

from __future__ import annotations

from alphamind.decision.portfolio_manager.submit_envelope.dispatch import (
    _adjust_command_context as _adjust_command_context,
)
from alphamind.decision.portfolio_manager.submit_envelope.process import (
    _validate_envelope_payload as _validate_envelope_payload,
)
from alphamind.decision.portfolio_manager.submit_envelope.server import (
    _handle_submit_envelope as _handle_submit_envelope,
)
from alphamind.decision.portfolio_manager.submit_envelope.server import (
    build_submit_envelope_mcp_server,
)
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    Acknowledgment,
    FailedSubmissionEntry,
    RejectionPayload,
    SubmissionLogEntry,
    SubmissionResult,
    SubmitEnvelopeState,
    build_initial_submit_envelope_state,
    get_failed_submission_log,
    get_submission_log,
)

__all__ = [
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
]
