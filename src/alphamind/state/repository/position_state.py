"""As-of position-state provider for the strategist replay path (ALP-563, §1).

The strategist position-action replay (story 07) measures the counterfactual
outcome of a ``close`` / ``reduce`` / ``adjust-bracket`` / ``add`` proposal
against the position's state *at the proposal's invocation timestamp*. That
state is reconstructed here from the existing position / fill / bracket records
(parent decision (I): no new state-capture infrastructure).

:func:`load_position_state_at` loads the :class:`PositionRecord` (and its
:class:`BracketRecord`s) for a position, then folds the fills with
``fill_timestamp <= as_of`` into a frozen :class:`PositionStateSnapshot`: a
signed ``net_quantity_as_of`` and an entry-weighted ``average_cost_basis``.
Reduce / close fills net the quantity down but never contaminate the cost
basis. When no add / reduce occurred before ``as_of`` these equal the position's
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


# Fractional-share / fractional-contract fills make exact Decimal equality on
# the reduce-magnitude tally brittle; treat sub-milli quantities as zero.
_QUANTITY_EPSILON = Decimal("0.001")


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

    ``net_quantity_as_of`` is signed (positive long, negative short) and folds
    reduce / close fills back out; ``average_cost_basis`` is the quantity-weighted
    entry price (per share for equity, per contract for options) across the
    entry / add fills at or before ``as_of`` — reduce / close exit prices never
    enter the average. ``open_brackets`` is the position's bracket set the
    ADJUST-BRACKET / ADD walks read; ``opened_at`` is the first entry fill's
    timestamp.
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
    ``net_quantity_as_of`` (reduces netted out) and an entry-weighted
    ``average_cost_basis`` (entry / add fills only).

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
    """Fold fills at or before *as_of* into (net quantity, entry basis, first ts).

    ``execution_history`` carries every fill — entry, add, reduce, and close —
    each as a positive ``fill_quantity`` with no stored side (the write path
    derives the side from the order at write time but the persisted
    :class:`~alphamind.portfolio_state.records.positions.PositionFill` keeps only
    the magnitude). A reduce/close fill therefore looks identical to an add in
    isolation, so this fold reconstructs each fill's side before folding:

    * **net_quantity** is the running quantity in the position's direction —
      entry/add fills grow it, reduce/close fills shrink it. The caller applies
      the position-direction sign.
    * **entry basis** is ``Σ(fill_price * qty) / Σ qty`` over entry/add fills
      only; reduce/close exit prices never contaminate the cost basis.

    Side reconstruction uses the position's current net as a terminal anchor.
    The total reduce magnitude over the whole life is
    ``(sum|fill| - |current_net|) / 2`` (entries minus reduces equals the current
    net; entries plus reduces equals the gross). Reduces are assigned to the
    *latest* fills — the open → add → reduce/close lifecycle the strategist
    replay measures against — so a partial reduce before ``as_of`` nets out
    correctly and leaves the entry basis untouched.

    Returns ``(0.0, None, None)`` when no fill qualifies — the caller treats a
    ``None`` first-timestamp as "position not open at as_of".
    """
    reduce_timestamps = _reduce_fill_timestamps(record)

    net_quantity = Decimal(0)
    entry_quantity = Decimal(0)
    weighted_entry_cost = Decimal(0)
    opened_at: datetime | None = None
    for index, fill in enumerate(record.execution_history):
        if fill.fill_timestamp > as_of:
            continue
        qty = Decimal(str(fill.fill_quantity))
        if index in reduce_timestamps:
            net_quantity -= qty
            continue
        if opened_at is None or fill.fill_timestamp < opened_at:
            opened_at = fill.fill_timestamp
        net_quantity += qty
        entry_quantity += qty
        weighted_entry_cost += Decimal(fill.fill_price) * qty

    if opened_at is None or entry_quantity == 0:
        return 0.0, None, None
    average_cost_basis = price(weighted_entry_cost / entry_quantity)
    return float(net_quantity), average_cost_basis, opened_at


def _reduce_fill_timestamps(record: PositionRecord) -> frozenset[int]:
    """Return the indices of the reduce/close fills in ``execution_history``.

    Entries minus reduces equal the position's current net quantity, so the
    total reduce magnitude over the position's whole life is
    ``(gross - |current_net|) / 2``. With no per-fill side stored, that reduce
    magnitude is attributed to the *latest* fills (the open → add → reduce/close
    lifecycle): walking the history newest-first, each fill is a reduce until the
    attributed reduce magnitude is exhausted. The remaining (earlier) fills are
    entries/adds.
    """
    history = record.execution_history
    if not history:
        return frozenset()
    gross = sum((Decimal(str(f.fill_quantity)) for f in history), Decimal(0))
    current_net = Decimal(str(_current_net_quantity(record)))
    reduce_remaining = (gross - abs(current_net)) / 2
    if reduce_remaining <= _QUANTITY_EPSILON:
        return frozenset()

    reduce_indices: set[int] = set()
    for index in range(len(history) - 1, -1, -1):
        if reduce_remaining <= _QUANTITY_EPSILON:
            break
        reduce_indices.add(index)
        reduce_remaining -= Decimal(str(history[index].fill_quantity))
    return frozenset(reduce_indices)


def _current_net_quantity(record: PositionRecord) -> float:
    """The position's current net quantity (shares for equity, contracts for options)."""
    details = record.details
    if isinstance(details, EquityPositionDetails):
        return details.share_count
    assert isinstance(details, OptionsPositionDetails)  # strategy payloads are excluded upstream
    return details.contract_count


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
