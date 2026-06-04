"""Pure classifier: broker ``ActivitySnapshot`` stream → typed lifecycle events.

The functional core of the poll. It filters the flat activity stream down to
the four option-lifecycle types, projects each into a typed
:class:`~alphamind.execution.account_activities.records.LifecycleEvent`, and
pairs every assignment / exercise (``OPASN`` / ``OPEXC``) with the ``OPTRD``
that prices its equity leg (ADR-0002). No I/O — the broker fetch and the DB
writes live in the shell (:mod:`alphamind.execution.account_activities.poll`).

Pairing rule: an ``OPTRD`` carries the *equity* symbol (the underlying); an
``OPASN`` / ``OPEXC`` carries the *option* OCC symbol whose embedded underlying
root matches. The two share a ``transaction_time``. The classifier matches each
assignment / exercise to the lone ``OPTRD`` on the same underlying root.
"""

from __future__ import annotations

import re

from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.execution.broker_adapter.queries import ActivitySnapshot

# OCC symbol: ROOT (1-6 alpha) + YYMMDD + C|P + 8-digit strike-milli.
_OCC_ROOT_RE = re.compile(r"^(?P<root>[A-Z]+)\d{6}[CP]\d{8}$")

_PAIRED_TYPES = frozenset({LifecycleActivityType.OPASN, LifecycleActivityType.OPEXC})

# All lifecycle activity-type string values, hoisted to module level so the
# classify loop does not rebuild the set per snapshot (mirrors ``_PAIRED_TYPES``).
_LIFECYCLE_TYPE_VALUES = frozenset(t.value for t in LifecycleActivityType)


def _underlying_root(occ_symbol: str) -> str | None:
    """Extract the underlying root from a bare OCC contract symbol.

    Returns ``None`` when *occ_symbol* is not OCC-shaped (the broker should
    never hand us a malformed lifecycle symbol, but the caller treats a miss as
    "cannot pair" rather than raising mid-stream).
    """
    match = _OCC_ROOT_RE.match(occ_symbol)
    return match.group("root") if match is not None else None


def _to_trade_leg(snap: ActivitySnapshot) -> TradeLeg | None:
    """Project an ``OPTRD`` ``ActivitySnapshot`` into a :class:`TradeLeg`.

    Returns ``None`` when the snapshot lacks the fields that price the equity
    leg (symbol / qty / price / side) — an underspecified ``OPTRD`` the caller
    surfaces rather than books against.
    """
    if snap.symbol is None or snap.qty is None or snap.price is None or snap.side is None:
        return None
    return TradeLeg(
        activity_id=snap.id,
        equity_symbol=snap.symbol,
        qty=snap.qty,
        strike_price=snap.price,
        side=snap.side,
        net_amount=snap.net_amount,
    )


def classify_lifecycle_activities(
    snapshots: tuple[ActivitySnapshot, ...],
) -> tuple[LifecycleEvent, ...]:
    """Classify a broker activity stream into typed, paired lifecycle events.

    Drops non-lifecycle activities and any standalone ``OPTRD`` (it surfaces
    only as the pair of an assignment / exercise). Each assignment / exercise is
    paired with the ``OPTRD`` whose underlying root matches its option's root.
    """
    trade_legs_by_root: dict[str, TradeLeg] = {}
    for snap in snapshots:
        if snap.activity_type != LifecycleActivityType.OPTRD.value or snap.symbol is None:
            continue
        leg = _to_trade_leg(snap)
        if leg is not None:
            trade_legs_by_root[leg.equity_symbol] = leg

    events: list[LifecycleEvent] = []
    for snap in snapshots:
        if snap.activity_type not in _LIFECYCLE_TYPE_VALUES:
            continue
        activity_type = LifecycleActivityType(snap.activity_type)
        if activity_type is LifecycleActivityType.OPTRD:
            continue
        if snap.symbol is None or snap.transaction_time is None:
            continue
        paired: TradeLeg | None = None
        if activity_type in _PAIRED_TYPES:
            root = _underlying_root(snap.symbol)
            paired = trade_legs_by_root.get(root) if root is not None else None
        events.append(
            LifecycleEvent(
                activity_id=snap.id,
                activity_type=activity_type,
                occ_symbol=snap.symbol,
                qty=snap.qty if snap.qty is not None else 0.0,
                transaction_time=snap.transaction_time,
                paired_trade=paired,
            )
        )
    return tuple(events)


__all__ = ["classify_lifecycle_activities"]
