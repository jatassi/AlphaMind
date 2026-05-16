"""Tests for ``alphamind._kernel.money`` — Decimal-backed Money/Price primitives.

The audit's L4 finding flagged 115 ``float``-for-money sites; this module is
the type primitive every migration story will reach for. Parse at the boundary
(``money(broker_str)``), keep Decimal internally, return Decimal to the
boundary; never let a float touch a monetary path.
"""

from __future__ import annotations


def test_money_constructor_returns_decimal_at_runtime() -> None:
    from decimal import Decimal

    from alphamind._kernel.money import money

    value = money("100.50")
    assert isinstance(value, Decimal)
    assert value == Decimal("100.50")


def test_money_arithmetic_preserves_precision() -> None:
    """Decimal addition is exact for representable values.

    The canonical ``0.1 + 0.2 == 0.3`` test. ``float`` arithmetic gives
    ``0.30000000000000004``; Decimal gives exactly ``0.3``. This invariant
    is the entire reason this module exists — pinning it as a runtime test
    catches a regression that downgrades the constructor to float.
    """
    from alphamind._kernel.money import money

    assert money("0.1") + money("0.2") == money("0.3")


def test_money_rejects_unparseable_input() -> None:
    """Inputs Decimal cannot parse raise :class:`ValueError`."""
    import pytest

    from alphamind._kernel.money import money

    with pytest.raises(ValueError, match="money"):
        money("abc")


def test_money_rejects_negative_value() -> None:
    """``money`` requires non-negative input; debits go through ``signed_money``."""
    import pytest

    from alphamind._kernel.money import money

    with pytest.raises(ValueError, match="non-negative"):
        money("-1")


def test_money_accepts_int_and_decimal_inputs() -> None:
    from decimal import Decimal

    from alphamind._kernel.money import money

    assert money(100) == Decimal(100)
    assert money(Decimal("99.99")) == Decimal("99.99")


def test_money_accepts_float_input_without_binary_drift() -> None:
    """``money`` accepts float input and converts via ``str`` to preserve intent.

    ``Decimal(0.1)`` produces
    ``Decimal('0.1000000000000000055511151231257827021181583404541015625')``
    because ``0.1`` cannot be represented exactly in IEEE 754. Routing
    through ``str`` (``str(0.1) == "0.1"``) preserves the literal the
    developer typed. Pinning this invariant catches a regression that
    drops the ``str()`` step.
    """
    from alphamind._kernel.money import money

    assert money(0.1) + money(0.2) == money("0.3")
    assert money(123.45) == money("123.45")


def test_price_accepts_float_input_without_binary_drift() -> None:
    """``price`` accepts float input via the same str-conversion path."""
    from alphamind._kernel.money import price

    assert price(0.1) + price(0.2) == price("0.3")
    assert price(123.45) == price("123.45")


def test_signed_money_accepts_float_input_without_binary_drift() -> None:
    """``signed_money`` accepts float input (including negative) via str conversion."""
    from alphamind._kernel.money import signed_money

    assert signed_money(0.1) + signed_money(0.2) == signed_money("0.3")
    assert signed_money(-123.45) == signed_money("-123.45")


def test_price_accepts_positive_input() -> None:
    from decimal import Decimal

    from alphamind._kernel.money import price

    value = price("123.45")
    assert isinstance(value, Decimal)
    assert value == Decimal("123.45")


def test_price_rejects_zero() -> None:
    import pytest

    from alphamind._kernel.money import price

    with pytest.raises(ValueError, match="strictly positive"):
        price("0")


def test_price_rejects_negative() -> None:
    import pytest

    from alphamind._kernel.money import price

    with pytest.raises(ValueError, match="strictly positive"):
        price("-1")


def test_price_rejects_unparseable_input() -> None:
    import pytest

    from alphamind._kernel.money import price

    with pytest.raises(ValueError, match="price"):
        price("abc")


def test_signed_money_accepts_negative_value() -> None:
    """``signed_money`` is the constructor for fields that may carry a sign."""
    from decimal import Decimal

    from alphamind._kernel.money import signed_money

    value = signed_money("-100")
    assert isinstance(value, Decimal)
    assert value == Decimal(-100)


def test_signed_money_accepts_positive_and_zero() -> None:
    from decimal import Decimal

    from alphamind._kernel.money import signed_money

    assert signed_money("0") == Decimal(0)
    assert signed_money("100.50") == Decimal("100.50")


def test_signed_money_rejects_unparseable_input() -> None:
    import pytest

    from alphamind._kernel.money import signed_money

    with pytest.raises(ValueError, match="signed_money"):
        signed_money("abc")


def test_money_and_price_newtypes_are_distinct_identities() -> None:
    """``Money`` and ``Price`` are distinct NewTypes over the same supertype.

    Same runtime backing (``Decimal``), distinct identity for the type
    checker. Pinning the identity invariant here means a refactor that
    accidentally aliases ``Price = Money`` is caught by pytest. The
    aliases are widened to ``Any`` so we can introspect the documented
    ``__name__`` / ``__supertype__`` runtime metadata that mypy elides
    (NewType is a type at type-check time, a function at runtime).
    """
    from decimal import Decimal
    from typing import Any

    from alphamind._kernel import money as money_module

    money_alias: Any = money_module.Money
    price_alias: Any = money_module.Price

    assert money_alias is not price_alias
    assert money_alias.__name__ == "Money"
    assert price_alias.__name__ == "Price"
    assert money_alias.__supertype__ is Decimal
    assert price_alias.__supertype__ is Decimal


def test_kernel_money_has_zero_first_party_imports() -> None:
    """``_kernel.money`` must have no ``alphamind.*`` imports — it is a leaf.

    The ``kernel-leaf`` import-linter contract from story 03 enforces this at
    the package level; this test pins the file-level invariant so a careless
    refactor surfaces immediately under pytest.
    """
    import ast
    from pathlib import Path

    import alphamind._kernel.money as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not (
                node.module == "alphamind" or node.module.startswith("alphamind.")
            ), f"_kernel.money must not import from alphamind.*; found: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "alphamind" or alias.name.startswith("alphamind.")), (
                    f"_kernel.money must not import alphamind.*; found: {alias.name}"
                )


def test_kernel_money_all_lists_every_public_name() -> None:
    from alphamind._kernel import money

    assert set(money.__all__) == {
        "DECIMAL_ZERO",
        "Money",
        "Price",
        "decimal_json_default",
        "money",
        "price",
        "signed_money",
    }
