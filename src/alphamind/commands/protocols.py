"""Injection-seam Protocols at the commands/ kernel boundary.

Two callable Protocols let the engine-stub
(:mod:`alphamind.decision.portfolio_manager.submit_envelope`) consume
execution-side broker routing and decision-side envelope validation
without importing those modules directly:

* :class:`BrokerDispatch` — the callable that routes a canonical
  :class:`alphamind.commands.command_models.OMSCommand` through the
  broker adapter. The concrete implementation lives in
  :mod:`alphamind.execution.oms.broker_dispatch`; the composition root
  (the pipeline runner) constructs it and passes it into the PM harness.

* :class:`ValidationCallable` — the callable that runs the Layer-2/3
  cross-field-invariant validator on a parsed :class:`PMEnvelope`. The
  concrete implementation lives in
  :mod:`alphamind.decision.portfolio_manager.validation`; the engine-stub
  receives it via constructor injection.

Both Protocols are :func:`typing.runtime_checkable` so test fakes pass
:func:`isinstance` checks without inheriting; the engine-stub itself
type-checks against the Protocol shape, not the concrete class.

ALP-458 introduced these Protocols to break the decision↔execution import
cycle: with dependency injection at the composition root, neither side
needs to import the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from alphamind.commands.command_models import OMSCommand
    from alphamind.commands.pm_envelope import PMEnvelope
    from alphamind.commands.validation_results import ValidationResult

__all__ = [
    "BrokerDispatch",
    "ValidationCallable",
]


@runtime_checkable
class BrokerDispatch(Protocol):
    """Callable interface routing one canonical OMS command to the broker.

    The engine-stub invokes this callable per accepted command; the concrete
    implementation
    (:func:`alphamind.execution.oms.broker_dispatch.dispatch_command_to_broker`)
    threads per-asset-type kwargs through the broker adapter and returns a
    :class:`alphamind.execution.broker_adapter.SubmissionOutcome` wrapping
    a :class:`alphamind.execution.oms.broker_dispatch.BrokerDispatchResult`.

    The shape is intentionally generic — the engine-stub forwards all
    per-command kwargs via ``**context`` so the dispatcher can grow new
    parameters (story 03e threaded ten of them) without forcing changes to
    every test fake.

    Composition root contract (the pipeline runner): construct the concrete
    dispatcher partially-applied over the per-invocation ``client`` /
    ``queries`` / ``execution_config`` so the engine-stub only supplies the
    per-command kwargs at call time.
    """

    async def __call__(
        self,
        command: OMSCommand,
        *,
        client_order_id: str,
        **context: Any,
    ) -> Any: ...


@runtime_checkable
class ValidationCallable(Protocol):
    """Callable interface running Layer-2/3 invariants on a PMEnvelope.

    The concrete implementation is
    :func:`alphamind.decision.portfolio_manager.validation.validate_pm_envelope`.
    Used by execution-side consumers (currently the Phase 2 write path's
    rejection persistence) that hold a :class:`PMEnvelope` and need its
    invariant errors without importing the decision-layer validation module.

    The function's full signature on the concrete side accepts a
    :class:`RetrievalStore` and pre-processor bundle; the Protocol leaves
    those as ``**context`` kwargs so callers thread per-invocation state
    without binding to the concrete implementation's parameter shape.
    """

    def __call__(
        self,
        envelope: PMEnvelope,
        **context: Any,
    ) -> ValidationResult: ...
