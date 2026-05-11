"""Class-group composition for the Reg T margin attribution module (story 02).

Mirrors ``regt-margin-attribution.md § Portfolio-margin reference model``: a
class group is one underlying ticker plus every position written on it. The
PM-equivalent stress (story 03b) iterates class groups; the Reg T per-leg
formula (story 03a) iterates positions independently.

Pure module: no I/O, no clock reads, no global mutation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from alphamind.portfolio_state.records.positions import (
    PositionRecord,
    resolve_ticker,
)


@dataclass(frozen=True, slots=True)
class ClassGroup:
    """One underlying symbol plus every position written on it.

    Mirrors ``regt-margin-attribution.md § Portfolio-margin reference model``:
    a class group's PM-equivalent margin is the worst-case P/L magnitude
    across the 10-point shock grid applied to all instruments in the group.
    """

    underlying_symbol: str
    positions: tuple[PositionRecord, ...]

    def __post_init__(self) -> None:
        if not self.positions:
            msg = "ClassGroup.positions must be non-empty"
            raise ValueError(msg)
        normalised = self.underlying_symbol.upper()
        if normalised != self.underlying_symbol:
            object.__setattr__(self, "underlying_symbol", normalised)


def _underlying_of(position: PositionRecord) -> str:
    """Return the upper-cased underlying symbol for a position.

    Reuses ``resolve_ticker`` for the per-variant dispatch and rejects the
    empty-strategy-legs case (``resolve_ticker`` returns ``None``) since
    multi-leg strategies are required to carry at least one leg by the
    ``position-model.md`` invariant.
    """
    ticker = resolve_ticker(position.details)
    if ticker is None:
        msg = (
            f"Cannot determine underlying for position {position.position_id!r}: "
            "strategy has no legs"
        )
        raise ValueError(msg)
    return ticker.upper()


def compose_class_groups(
    positions: tuple[PositionRecord, ...],
) -> tuple[ClassGroup, ...]:
    """Partition open positions into class groups keyed on underlying symbol.

    Pure function. Returns a tuple sorted by ``underlying_symbol`` ascending
    for deterministic downstream iteration. An empty input returns an empty
    tuple.
    """
    by_symbol: dict[str, list[PositionRecord]] = defaultdict(list)
    for position in positions:
        by_symbol[_underlying_of(position)].append(position)
    return tuple(
        ClassGroup(underlying_symbol=symbol, positions=tuple(by_symbol[symbol]))
        for symbol in sorted(by_symbol)
    )
