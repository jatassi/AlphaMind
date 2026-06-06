"""As-of position-state provider for the strategist replay path (ALP-563, §1).

The strategist position-action replay (story 07) measures the counterfactual
outcome of a ``close`` / ``reduce`` / ``adjust-bracket`` / ``add`` proposal
against the position's state *at the proposal's invocation timestamp*. That
state is reconstructed here from the existing position / fill / bracket records
(parent decision (I): no new state-capture infrastructure).

:func:`load_position_state_at` loads the :class:`PositionRecord` (and its
:class:`BracketRecord`s) for a position, then folds the fills with
``fill_timestamp <= as_of`` into a frozen :class:`PositionStateSnapshot`: a
signed ``net_quantity_as_of`` and a quantity-weighted ``average_cost_basis``.
When no add / reduce occurred before ``as_of`` these equal the position's
current ``details`` values. A multi-leg strategy position is out of scope and
raises :class:`PositionStateNotFoundError` (the eligibility check, story 04,
should already have excluded it); so does a missing position or one with no fill
at or before ``as_of``.

This helper lives in ``state.repository`` (the read layer that owns the
position / bracket queries); the engine's ``strategist_replay`` imports the
snapshot type from here. ``execution`` and ``state`` are non-independent
siblings under the composition-root layering, so the cross-package import is
allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.ids import PositionId
from alphamind._kernel.money import Price, price
from alphamind.portfolio_state.records.orders import BracketRecord
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionRecord,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import rows_to_records_isolated
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record

__all__ = [
    "PositionStateNotFoundError",
    "PositionStateSnapshot",
    "load_position_state_at",
]


class PositionStateNotFoundError(Exception):
    """The position has no replayable state at the requested ``as_of``.

    Raised when the position does not exist, when no fill exists at or before
    ``as_of`` (the position had not opened yet), or when the position is a
    multi-leg strategy (out of scope for v2 — the eligibility check should
    already have excluded it).
    """


@dataclass(frozen=True, slots=True)
class PositionStateSnapshot:
    """The position's reconstructed state at the strategist-proposal timestamp.

    ``net_quantity_as_of`` is signed (positive long, negative short) and
    ``average_cost_basis`` is the quantity-weighted entry price (per share for
    equity, per contract for options) across the fills at or before ``as_of``.
    ``open_brackets`` is the position's bracket set the ADJUST-BRACKET / ADD
    walks read; ``opened_at`` is the first such fill's timestamp.
    """

    position_id: PositionId
    details: EquityPositionDetails | OptionsPositionDetails
    direction: Direction
    net_quantity_as_of: float
    average_cost_basis: Price
    open_brackets: tuple[BracketRecord, ...]
    opened_at: datetime


def load_position_state_at(
    session: Session,
    *,
    position_id: PositionId,
    as_of: datetime,
) -> PositionStateSnapshot:
    """Reconstruct *position_id*'s state as of *as_of* (design Step §1).

    Loads the :class:`PositionRecord` and its open :class:`BracketRecord`s, then
    folds the fills with ``fill_timestamp <= as_of`` into a signed
    ``net_quantity_as_of`` and a quantity-weighted ``average_cost_basis``.

    Raises :class:`PositionStateNotFoundError` when the position is missing, is a
    multi-leg strategy (out of scope), or has no fill at or before *as_of*.
    """
    row = session.execute(
        select(PositionRow).where(PositionRow.position_id == str(position_id))
    ).scalar_one_or_none()
    if row is None:
        msg = f"no position found for position_id={position_id!r}"
        raise PositionStateNotFoundError(msg)

    record = row_to_record(row)
    details = record.details
    if not isinstance(details, EquityPositionDetails | OptionsPositionDetails):
        msg = (
            f"position_id={position_id!r} is a multi-leg strategy position; "
            "strategist replay is single-leg only (out of scope for v2)"
        )
        raise PositionStateNotFoundError(msg)
    if record.direction is None:  # pragma: no cover — tied to strategy payload above
        msg = f"position_id={position_id!r} has no direction"
        raise PositionStateNotFoundError(msg)

    net_quantity, average_cost_basis, opened_at = _fold_fills(record, as_of=as_of)
    if opened_at is None or average_cost_basis is None:
        msg = (
            f"position_id={position_id!r} has no fill at or before as_of={as_of.isoformat()}; "
            "the position had not opened yet"
        )
        raise PositionStateNotFoundError(msg)

    direction_sign = 1.0 if record.direction is Direction.LONG else -1.0
    open_brackets = _load_open_brackets(session, position_id=position_id)

    return PositionStateSnapshot(
        position_id=record.position_id,
        details=details,
        direction=record.direction,
        net_quantity_as_of=direction_sign * net_quantity,
        average_cost_basis=average_cost_basis,
        open_brackets=open_brackets,
        opened_at=opened_at,
    )


def _fold_fills(
    record: PositionRecord,
    *,
    as_of: datetime,
) -> tuple[float, Price | None, datetime | None]:
    """Fold fills at or before *as_of* into (gross quantity, weighted basis, first ts).

    ``gross_quantity`` is the unsigned sum of ``fill_quantity``; the caller
    applies the position-direction sign. ``weighted_basis`` is
    ``Σ(fill_price * fill_quantity) / Σ fill_quantity`` over the same fills.
    Returns ``(0.0, None, None)`` when no fill qualifies — the caller treats a
    ``None`` first-timestamp as "position not open at as_of".
    """
    gross_quantity = Decimal(0)
    weighted_cost = Decimal(0)
    opened_at: datetime | None = None
    for fill in record.execution_history:
        if fill.fill_timestamp > as_of:
            continue
        if opened_at is None or fill.fill_timestamp < opened_at:
            opened_at = fill.fill_timestamp
        qty = Decimal(str(fill.fill_quantity))
        gross_quantity += qty
        weighted_cost += Decimal(fill.fill_price) * qty

    if opened_at is None or gross_quantity == 0:
        return 0.0, None, None
    average_cost_basis = price(weighted_cost / gross_quantity)
    return float(gross_quantity), average_cost_basis, opened_at


def _load_open_brackets(
    session: Session,
    *,
    position_id: PositionId,
) -> tuple[BracketRecord, ...]:
    """Return the position's bracket records, reconstructed with per-bracket isolation."""
    bracket_rows = list(
        session.execute(
            select(BracketRow)
            .where(BracketRow.position_id == str(position_id))
            .order_by(BracketRow.bracket_id.asc())
        ).scalars()
    )
    if not bracket_rows:
        return ()
    bracket_ids = [b.bracket_id for b in bracket_rows]
    leg_rows = session.execute(
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id.in_(bracket_ids))
        .order_by(BracketLegRow.bracket_id.asc(), BracketLegRow.leg_index.asc())
    ).scalars()
    legs_by_bracket: dict[str, list[BracketLegRow]] = {bid: [] for bid in bracket_ids}
    for leg in leg_rows:
        legs_by_bracket[leg.bracket_id].append(leg)
    return rows_to_records_isolated(bracket_rows, legs_by_bracket)
