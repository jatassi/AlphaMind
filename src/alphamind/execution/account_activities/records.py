"""Typed records for the account-activities option-lifecycle poll (ALP-846 / W1b).

The functional core of this package operates on these frozen records, never on
the broker's loosely-typed ``ActivitySnapshot``. The classifier
(:mod:`alphamind.execution.account_activities.classify`) parses the broker
stream into :class:`LifecycleEvent` values once at the boundary; the handlers
and booking math then trust the type (parse, don't validate).

The four lifecycle activity types this story handles (ADR-0002):

* ``OPEXP`` — option expiration. An out-of-the-money option expires worthless;
  realized PnL is ``-premium`` and the option position closes with no husk.
* ``OPEXC`` / ``OPASN`` — exercise / assignment. The option converts to an
  equity leg priced at the strike, fully described by the **paired** ``OPTRD``
  activity (ADR-0002): the resulting equity position opens at the strike with
  correct cost basis and an Intent stub linked to the option's originating
  thesis.
* ``OPTRD`` — the option-trade leg that prices an assignment / exercise. Never
  a standalone lifecycle event; it is consumed as the pair of an ``OPASN`` /
  ``OPEXC`` on the same underlying.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from alphamind._kernel.money import Money, Price
from alphamind.portfolio_state.records.positions import PositionRecord


class LifecycleActivityType(StrEnum):
    """The option-lifecycle account-activity types this story handles.

    A strict subset of
    :class:`~alphamind.state.records_broker_event_log.BrokerEventType` — the
    classifier rejects any other ``activity_type`` so a non-lifecycle activity
    can never be mistaken for one.
    """

    OPEXP = "OPEXP"
    OPEXC = "OPEXC"
    OPASN = "OPASN"
    OPTRD = "OPTRD"


class TradeLeg(BaseModel):
    """The ``OPTRD`` leg that prices an assignment / exercise (ADR-0002).

    Projected from the paired ``OPTRD`` ``ActivitySnapshot``. ``symbol`` is the
    resulting equity ticker, ``price`` the per-share strike, ``qty`` the share
    count, ``side`` the broker's buy/sell. These fields fully describe the
    equity leg — if a real ``OPTRD`` ever omitted them, the booking would be
    underspecified and the handler must surface rather than guess.
    """

    model_config = ConfigDict(frozen=True)

    activity_id: str
    equity_symbol: str
    qty: float
    strike_price: Price
    side: str
    net_amount: Money | None


class LifecycleEvent(BaseModel):
    """One typed option-lifecycle event, optionally carrying its priced pair.

    ``occ_symbol`` is the bare OCC contract symbol Alpaca keys the activity by
    (e.g. ``"AAPL250918C00150000"``) — the same form
    :func:`alphamind.execution.corporate_actions.reconciliation._alpaca_occ_symbol`
    builds from a local options position, so the handler can match the event to
    the open option position. ``paired_trade`` is the ``OPTRD`` leg for an
    assignment / exercise; ``None`` for an expiry (and for an ``OPTRD`` that
    failed to pair, which the classifier drops).
    """

    model_config = ConfigDict(frozen=True)

    activity_id: str
    activity_type: LifecycleActivityType
    occ_symbol: str
    qty: float
    transaction_time: dt.datetime
    paired_trade: TradeLeg | None


@dataclass(frozen=True, slots=True)
class BookingResult:
    """Pure output of the booking math for one lifecycle event.

    ``realized_pnl_usd`` is the signed realized PnL to book in the
    ``thesis_pnl_ledger`` (negative for an OTM expiry loss). ``closed_option``
    is the option ``PositionRecord`` transitioned to ``CLOSED`` (no ``OPEN/0``
    husk). ``opened_equity`` is the resulting equity position for an assignment
    / exercise, opened at the strike with the correct cost basis and the
    option's thesis link; ``None`` for an expiry.
    """

    realized_pnl_usd: Money
    closed_option: PositionRecord
    opened_equity: PositionRecord | None


__all__ = [
    "BookingResult",
    "LifecycleActivityType",
    "LifecycleEvent",
    "TradeLeg",
]
