"""Direction-word rendering shared across decision-layer input bundles.

The portfolio-manager and strategist input bundles each render an
``Underlying:`` line carrying a direction word. :func:`direction_display`
is the single source of that word so the two harnesses cannot drift.
"""

from __future__ import annotations

from alphamind.portfolio_state.records.positions import (
    Direction,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.portfolio_state.views.positions import PositionView

_DIRECTION_DISPLAY: dict[Direction, str] = {
    Direction.LONG: "long",
    Direction.SHORT: "short",
}


def direction_display(pos: PositionView) -> str:
    """Return the direction word for the underlying line.

    An equity / single-leg options position is long or short. A multi-leg
    strategy has no position-level direction — ``position_direction()`` returns
    ``None`` — so the strategy-type label stands in its place, the same
    treatment story 01b applies to ``AnalystHeldPosition``.
    """
    direction = position_direction(pos.record)
    if direction is not None:
        return _DIRECTION_DISPLAY[direction]
    details = pos.details
    assert isinstance(details, StrategyPositionDetails)
    return details.strategy_type_label
