"""Structural Protocol declaring the design's "base position interface".

Per ``docs/design/05-execution-layer/position-model.md`` § Base position, every
instrument type (equity / options / strategy) shares a common field set:
position_id, thesis_id, bracket_id, status, direction, entry_timestamp. Story
05b formalises this contract as a runtime-checkable Protocol so consumers
reasoning generically about a position can annotate against the Protocol rather
than the concrete ``PositionRecord`` or ``PositionView`` types — insulating
them from instrument-specific schema evolution and from the 05a record/view
split.

Pydantic models implement Protocols structurally; no inheritance is required.
``runtime_checkable`` enables ``isinstance(x, BasePositionProtocol)`` for
sanity checks at integration boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from alphamind.portfolio_state.records.positions import (
    Direction,
    PositionStatus,
)


@runtime_checkable
class BasePositionProtocol(Protocol):
    """The design's "base position interface" — fields every position has,
    regardless of instrument type (equity / options / strategy).

    Consumers that reason about positions generically (sector exposure
    aggregation, P/L summation, lifecycle tracking) should annotate against
    this Protocol rather than the concrete ``PositionRecord``. This insulates
    the consumer from instrument-specific schema evolution and from the
    ``PositionRecord`` vs ``PositionView`` split (story 05a).

    Per ``docs/design/05-execution-layer/position-model.md`` § Base position.
    """

    @property
    def position_id(self) -> str: ...

    @property
    def thesis_id(self) -> str | None: ...

    @property
    def bracket_id(self) -> str | None: ...

    @property
    def status(self) -> PositionStatus: ...

    @property
    def direction(self) -> Direction: ...

    @property
    def entry_timestamp(self) -> datetime | None: ...
