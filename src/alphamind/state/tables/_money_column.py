"""SQLAlchemy column type for Decimal-exact monetary storage (ALP-462).

SQLite's ``NUMERIC`` column affinity falls back to ``REAL`` (IEEE 754 binary
float) whenever the value carries a fractional part — which silently
re-introduces the binary-rounding drift the ``Money`` / ``Price`` primitives
exist to prevent. ``Decimal('1234567.89')`` round-trips through ``Numeric`` as
``Decimal('1234567.889999999898')``.

``DecimalText`` sidesteps the affinity collapse by storing each value as the
canonical string repr of the ``Decimal`` (e.g., ``"1234567.89"``) and parsing
it back with ``Decimal(text)`` on read. The codec layer (cash-ledger,
fill-records, and future activity-log) is the durability boundary; with this
column type the round-trip is exact regardless of magnitude or scale.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator[Decimal]):
    """Store ``Decimal`` as text, preserving precision exactly."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | str | None, dialect: Dialect) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, Decimal):
            return str(value)
        return str(Decimal(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        del dialect
        if value is None:
            return None
        return Decimal(value)


__all__ = ["DecimalText"]
