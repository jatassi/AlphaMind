"""OMS package — command-ID derivation and OMS-side submission paths.

After ALP-458 the wire-format types (canonical OMS command discriminated
union and engine envelope) live in :mod:`alphamind.commands`; this package
hosts only the OMS-internal command-ID utilities and the engine-envelope
submission paths.

The :mod:`~alphamind.execution.oms.command_ids` module (story 01b / ALP-371)
provides the canonical command-ID derivation utility (PM-originated and
engine-originated).

The :mod:`~alphamind.execution.oms.submit_engine_envelope` module (story 04 /
ALP-375) provides the monitor-facing :func:`submit_engine_envelope` write
function the continuous monitor calls between invocations to persist a
protective CLOSE.

The :mod:`~alphamind.execution.oms.broker_dispatch` module (story 03e /
ALP-390) dispatches a canonical :class:`alphamind.commands.command_models
.OMSCommand` to the broker adapter's typed submission helpers — the
concrete implementation of the :class:`alphamind.commands.protocols
.BrokerDispatch` Protocol injected at the composition root.

The ``submit_envelope`` MCP wrapper moved out of this package
to :mod:`alphamind.decision.portfolio_manager.submit_envelope` by ALP-458;
the transitional ``__getattr__`` lazy-loader that previously bridged it
back through this ``__init__`` is gone.
"""

from alphamind.execution.oms.command_ids import (
    EngineCommandIdComponents,
    PMCommandIdComponents,
    compute_attempt_seq,
    derive_engine_command_id,
    derive_open_thesis_id,
    derive_pipeline_protective_command_id,
    derive_pm_base_command_id,
    derive_pm_command_id,
    is_engine_originated,
    is_pm_originated,
    parse_engine_command_id,
    parse_pm_command_id,
)
from alphamind.execution.oms.submit_engine_envelope import (
    SubmitEngineEnvelopeState,
    build_initial_submit_engine_envelope_state,
)

__all__ = [
    "EngineCommandIdComponents",
    "PMCommandIdComponents",
    "SubmitEngineEnvelopeState",
    "build_initial_submit_engine_envelope_state",
    "compute_attempt_seq",
    "derive_engine_command_id",
    "derive_open_thesis_id",
    "derive_pipeline_protective_command_id",
    "derive_pm_base_command_id",
    "derive_pm_command_id",
    "is_engine_originated",
    "is_pm_originated",
    "parse_engine_command_id",
    "parse_pm_command_id",
]
