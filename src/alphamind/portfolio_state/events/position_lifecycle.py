"""Position-lifecycle event details — POSITION_OPENED / CLOSED / ADDED / REDUCED.

Each detail payload is a frozen, slotted dataclass with monetary fields typed
as ``Money`` / ``Price`` from ``_kernel.money``. Invariants the prior
Pydantic validators expressed are preserved as ``__post_init__`` guards.

``_REGISTRY`` is the dispatch source — the aggregated dicts in
``events/__init__.py`` derive ``EVENT_TYPE_TO_DETAIL_CLASS`` and
``EVENT_TYPE_TO_GROUP`` from the union of every submodule's ``_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass

from alphamind._kernel.money import Money, Price
from alphamind.portfolio_state.events.types import (
    EventGroup,
    EventType,
    PositionExitMethod,
    PositionOpenMechanism,
)


@dataclass(frozen=True, slots=True)
class PositionOpenedDetail:
    """Detail payload for POSITION_OPENED events."""

    ticker: str
    direction: str
    fill_price: Price
    quantity: float
    thesis_id: str | None
    bracket_id: str | None
    mechanism: PositionOpenMechanism
    parent_position_id: str | None

    def __post_init__(self) -> None:
        if (
            self.mechanism == PositionOpenMechanism.ORDER_FILL
            and self.parent_position_id is not None
        ):
            msg = "parent_position_id must be None when mechanism is ORDER_FILL"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class PositionClosedDetail:
    """Detail payload for POSITION_CLOSED events.

    ``exit_price`` carries ``Money`` rather than ``Price`` semantics because
    strategy-position closures emit ``exit_price=0`` as a placeholder while
    Phase 1 reconciliation is pending (see
    ``bracket_stops.task._estimated_exit_price_for``). The strategist's
    rendering treats ``0`` as "fill-time price unavailable".

    ``realized_pnl_usd`` may be negative (a losing trade), so it uses
    signed ``Money`` semantics — see ``_kernel.money.signed_money``.
    """

    exit_method: PositionExitMethod
    exit_price: Money
    realized_pnl_usd: Money
    thesis_resolution_category: str


@dataclass(frozen=True, slots=True)
class PositionAddedDetail:
    """Detail payload for POSITION_ADDED events.

    ``new_average_cost_basis`` is the post-addition average cost per share —
    always positive, modeled as :class:`Price`.
    """

    additional_quantity: float
    new_average_cost_basis: Price
    addition_thesis_component_id: str


@dataclass(frozen=True, slots=True)
class PositionReducedDetail:
    """Detail payload for POSITION_REDUCED events.

    ``partial_realized_pnl_usd`` may be negative on a losing partial close.
    """

    reduced_quantity: float
    partial_realized_pnl_usd: Money
    close_rationale_classification: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.POSITION_OPENED, PositionOpenedDetail, EventGroup.POSITION_LIFECYCLE),
    (EventType.POSITION_CLOSED, PositionClosedDetail, EventGroup.POSITION_LIFECYCLE),
    (EventType.POSITION_ADDED, PositionAddedDetail, EventGroup.POSITION_LIFECYCLE),
    (EventType.POSITION_REDUCED, PositionReducedDetail, EventGroup.POSITION_LIFECYCLE),
]


__all__ = [
    "PositionAddedDetail",
    "PositionClosedDetail",
    "PositionOpenedDetail",
    "PositionReducedDetail",
]
