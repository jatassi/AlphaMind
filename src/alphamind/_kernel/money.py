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
    "DECIMAL_ZERO",
    "Money",
    "Price",
    "decimal_json_default",
    "money",
    "price",
    "signed_money",
]


Money = NewType("Money", Decimal)
Price = NewType("Price", Decimal)

DECIMAL_ZERO = Decimal(0)


def decimal_json_default(obj: object) -> object:
    """``json.dumps`` ``default=`` hook that emits ``Decimal`` as its string form.

    Use as ``json.dumps(payload, default=decimal_json_default)`` whenever a
    codec needs to round-trip ``Decimal`` values through JSON without
    binary-float drift. The decode path reconstructs via
    :func:`money` / :func:`price` / :func:`signed_money`.
    """
    if isinstance(obj, Decimal):
        return str(obj)
    msg = f"object of type {type(obj).__name__} is not JSON-serializable"
    raise TypeError(msg)


def money(value: str | int | float | Decimal) -> Money:
    """Construct a non-negative :class:`Money` value, parsing at the boundary.

    Accepts the broker-string shape (``"100.50"``), integer shape (``100``),
    float shape (``100.5`` — converted via ``str`` to avoid binary-float
    drift), or an already-converted :class:`Decimal`. Raises
    :class:`ValueError` for inputs Decimal cannot parse and for negative
    values; use :func:`signed_money` for amounts that may be negative (e.g.,
    realized losses, debit balances).
    """
    decimal_value = _to_decimal(value, label="money")
    if decimal_value < 0:
        msg = f"money value must be non-negative; got {decimal_value}"
        raise ValueError(msg)
    return Money(decimal_value)


def price(value: str | int | float | Decimal) -> Price:
    """Construct a strictly positive :class:`Price` value.

    Prices are always > 0 by definition (a zero or negative price is a
    pricing error upstream, not a valid value to thread through downstream
    arithmetic). Accepts float input (converted via ``str`` to avoid
    binary-float drift). Raises :class:`ValueError` for parse errors and
    for values <= 0.
    """
    decimal_value = _to_decimal(value, label="price")
    if decimal_value <= 0:
        msg = f"price value must be strictly positive; got {decimal_value}"
        raise ValueError(msg)
    return Price(decimal_value)


def signed_money(value: str | int | float | Decimal) -> Money:
    """Construct a :class:`Money` value permitting negative amounts.

    Used for monetary fields that legitimately carry a sign (realized P&L,
    debit balances, cash-flow deltas). Accepts float input (converted via
    ``str`` to avoid binary-float drift). Raises :class:`ValueError` only
    for parse errors.
    """
    return Money(_to_decimal(value, label="signed_money"))


def _to_decimal(value: str | int | float | Decimal, *, label: str) -> Decimal:
    """Normalize the constructor input to :class:`Decimal`.

    Floats are converted via ``str`` to preserve the literal the developer
    typed (``str(0.1) == "0.1"``), avoiding the binary-float drift that
    ``Decimal(0.1)`` produces (``Decimal('0.1000000000000000055511...')``).
    Wraps :class:`decimal.InvalidOperation` (the underlying parse failure)
    in a :class:`ValueError` carrying the constructor label so the error
    message points at the boundary that rejected the value.

    ALP-489 — rejects ``NaN`` and ``Infinity`` at the boundary so internal
    Money/Price values are guaranteed finite (Decimal happily round-trips
    ``"Infinity"``/``"NaN"``; the previous float-typed records relied on a
    per-field ``math.isfinite`` check, which the Decimal migration retires).
    """
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, float):
        # Convert via str to avoid binary-float drift
        # (Decimal(0.1) == Decimal('0.1000000000000000055511151231257827021181583404541015625')).
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            msg = f"{label} value cannot be parsed as Decimal: {value!r}"
            raise ValueError(msg) from exc
    else:
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, ValueError, TypeError) as exc:
            msg = f"{label} value cannot be parsed as Decimal: {value!r}"
            raise ValueError(msg) from exc
    if not decimal_value.is_finite():
        msg = f"{label} value must be finite; got {decimal_value}"
        raise ValueError(msg)
    return decimal_value
