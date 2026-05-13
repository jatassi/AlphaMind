"""Money and Price primitives — Decimal-backed NewType aliases.

The audit's L4 finding identified 115 ``float``-for-money sites across 133K
LOC of trading code. This module is the type primitive every migration story
will reach for; the rule downstream is parse-at-the-boundary
(``money(broker_str)``), keep Decimal internally, never let a float touch a
monetary path (python-architecture §D2, §D3).

The Alpaca SDK returns monetary fields as ``str``; ``money(broker_str)``
converts at the boundary. Internal arithmetic uses standard Decimal operators
(``+``, ``-``, ``*``) which preserve precision — see
``test_money_arithmetic_preserves_precision`` for the canonical
``0.1 + 0.2 == 0.3`` guarantee.

Story 04 (ALP-460) creates the types in isolation; story 05b (ALP-462)
migrates boundary consumers to use them.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import NewType

__all__ = [
    "Money",
    "Price",
    "money",
    "price",
    "signed_money",
]


Money = NewType("Money", Decimal)
Price = NewType("Price", Decimal)


def money(value: str | int | Decimal) -> Money:
    """Construct a non-negative :class:`Money` value, parsing at the boundary.

    Accepts the broker-string shape (``"100.50"``), integer shape (``100``),
    or an already-converted :class:`Decimal`. Raises :class:`ValueError` for
    inputs Decimal cannot parse and for negative values; use
    :func:`signed_money` for amounts that may be negative (e.g., realized
    losses, debit balances).
    """
    decimal_value = _to_decimal(value, label="money")
    if decimal_value < 0:
        msg = f"money value must be non-negative; got {decimal_value}"
        raise ValueError(msg)
    return Money(decimal_value)


def price(value: str | int | Decimal) -> Price:
    """Construct a strictly positive :class:`Price` value.

    Prices are always > 0 by definition (a zero or negative price is a
    pricing error upstream, not a valid value to thread through downstream
    arithmetic). Raises :class:`ValueError` for parse errors and for
    values <= 0.
    """
    decimal_value = _to_decimal(value, label="price")
    if decimal_value <= 0:
        msg = f"price value must be strictly positive; got {decimal_value}"
        raise ValueError(msg)
    return Price(decimal_value)


def signed_money(value: str | int | Decimal) -> Money:
    """Construct a :class:`Money` value permitting negative amounts.

    Used for monetary fields that legitimately carry a sign (realized P&L,
    debit balances, cash-flow deltas). Raises :class:`ValueError` only for
    parse errors.
    """
    return Money(_to_decimal(value, label="signed_money"))


def _to_decimal(value: str | int | Decimal, *, label: str) -> Decimal:
    """Normalize the constructor input to :class:`Decimal`.

    Wraps :class:`decimal.InvalidOperation` (the underlying parse failure)
    in a :class:`ValueError` carrying the constructor label so the error
    message points at the boundary that rejected the value.
    """
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        msg = f"{label} value cannot be parsed as Decimal: {value!r}"
        raise ValueError(msg) from exc
