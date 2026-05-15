"""Injection-seam Protocol at the commands/ kernel boundary.

The :class:`BrokerDispatch` callable Protocol lets the engine-stub
(:mod:`alphamind.decision.portfolio_manager.submit_envelope`) consume
execution-side broker routing without importing the execution layer
directly. The Protocol is :func:`typing.runtime_checkable` so test fakes
pass :func:`isinstance` checks without inheriting; the engine-stub
type-checks against the Protocol shape, not the concrete class.

ALP-458 introduced this Protocol to break the decision↔execution import
cycle: with dependency injection at the composition root, neither side
needs to import the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from alphamind._kernel.ids import ClientOrderId

if TYPE_CHECKING:
    from alphamind.commands.command_models import OMSCommand

__all__ = [
    "BrokerDispatch",
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
        client_order_id: ClientOrderId,
        **context: Any,
    ) -> Any: ...
