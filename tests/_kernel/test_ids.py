"""Tests for ``alphamind._kernel.ids`` — NewType aliases for identifiers.

Each alias is a distinct type at type-check time but a plain ``str`` at runtime.
Constructor functions for IDs with known regex patterns (envelope IDs, command
IDs) validate once at the boundary so downstream code can trust the typed
value.
"""

from __future__ import annotations


def test_order_id_is_str_at_runtime() -> None:
    from alphamind._kernel.ids import OrderId

    value = OrderId("abc123")
    assert isinstance(value, str)
    assert value == "abc123"


def test_every_newtype_alias_is_str_at_runtime() -> None:
    """All 11 NewType aliases pass through to ``str`` at runtime.

    NewType has no runtime cost — the alias is the identity function on the
    supertype. Verifying every alias here pins the runtime contract so a
    refactor that wraps any of them in a class would be caught.
    """
    from alphamind._kernel.ids import (
        AlpacaOrderId,
        BracketId,
        ClientOrderId,
        CommandId,
        EnvelopeId,
        InvocationId,
        OccSymbol,
        OrderId,
        PositionId,
        Symbol,
        ThesisId,
    )

    aliases = (
        AlpacaOrderId,
        BracketId,
        ClientOrderId,
        CommandId,
        EnvelopeId,
        InvocationId,
        OccSymbol,
        OrderId,
        PositionId,
        Symbol,
        ThesisId,
    )
    for alias in aliases:
        value = alias("x")
        assert isinstance(value, str)
        assert value == "x"


def test_newtype_aliases_are_distinct_identities() -> None:
    """Each NewType is a distinct callable identity.

    ``NewType("X", str)`` creates a function-like object whose ``__name__``
    is ``"X"`` and whose ``__supertype__`` is ``str``. Two aliases with the
    same supertype are still distinct objects at runtime (the distinct
    identity is what mypy --strict pins as a type-check-time difference).

    A pure type-check-time distinctness assertion is impossible at runtime;
    this test pins the runtime invariant on which mypy's enforcement rests.
    See ``mypy --strict`` for the type-checker-level guarantee. The aliases
    are widened to ``Any`` so we can introspect the documented
    ``__name__`` / ``__supertype__`` runtime metadata that mypy elides
    (NewType is a type at type-check time, a function at runtime).
    """
    from typing import Any

    from alphamind._kernel import ids

    aliases: dict[str, Any] = {
        "OrderId": ids.OrderId,
        "PositionId": ids.PositionId,
        "BracketId": ids.BracketId,
        "CommandId": ids.CommandId,
        "AlpacaOrderId": ids.AlpacaOrderId,
        "ClientOrderId": ids.ClientOrderId,
        "EnvelopeId": ids.EnvelopeId,
        "InvocationId": ids.InvocationId,
        "ThesisId": ids.ThesisId,
        "Symbol": ids.Symbol,
        "OccSymbol": ids.OccSymbol,
    }

    identities = {id(alias) for alias in aliases.values()}
    assert len(identities) == len(aliases)

    for name, alias in aliases.items():
        assert alias.__name__ == name
        assert alias.__supertype__ is str


def test_envelope_id_accepts_pm_pattern() -> None:
    """PM-originated envelope IDs match ``^ENV-(REC|SA|SA-ORD)-[0-9]+$``."""
    from alphamind._kernel.ids import envelope_id

    value = envelope_id("ENV-REC-42")
    assert isinstance(value, str)
    assert value == "ENV-REC-42"


def test_envelope_id_accepts_pm_sa_pattern() -> None:
    from alphamind._kernel.ids import envelope_id

    for raw in ("ENV-SA-1", "ENV-SA-ORD-7", "ENV-REC-999"):
        assert envelope_id(raw) == raw


def test_envelope_id_accepts_engine_pattern() -> None:
    """Engine-originated envelope IDs match ``^MON\\.[^.]+\\.[0-9]+$``.

    Both origins share the :class:`EnvelopeId` type; the constructor accepts
    either valid shape.
    """
    from alphamind._kernel.ids import envelope_id

    assert envelope_id("MON.session-abc.7") == "MON.session-abc.7"
    assert envelope_id("MON.s1.1") == "MON.s1.1"


def test_envelope_id_rejects_invalid_input() -> None:
    """Strings that match neither pattern raise :class:`ValueError`."""
    import pytest

    from alphamind._kernel.ids import envelope_id

    for raw in ("", "ENV-FOO-1", "ENV-REC-", "MON..7", "MON.s.x", "random"):
        with pytest.raises(ValueError, match="envelope_id"):
            envelope_id(raw)


def test_command_id_accepts_pm_pattern() -> None:
    """PM-originated command IDs: ``inv-{inv}.{envelope}.{ord}.{seq}``."""
    from alphamind._kernel.ids import command_id

    raw = "inv-abc.ENV-REC-1.0.0"
    assert command_id(raw) == raw

    for raw in (
        "inv-2026-01-01.ENV-SA-7.0.0",
        "inv-x.ENV-SA-ORD-99.3.5",
    ):
        assert command_id(raw) == raw


def test_command_id_accepts_engine_pattern() -> None:
    """Engine-originated command IDs: ``MON.{session}.{trigger}.{ord}``."""
    from alphamind._kernel.ids import command_id

    for raw in ("MON.s.1.0", "MON.session-abc.42.3"):
        assert command_id(raw) == raw


def test_command_id_rejects_invalid_input() -> None:
    import pytest

    from alphamind._kernel.ids import command_id

    for raw in ("", "ENV-REC-1", "MON.s.x.0", "inv-.ENV-REC-1.0.0", "random"):
        with pytest.raises(ValueError, match="command_id"):
            command_id(raw)


def test_kernel_ids_has_zero_first_party_imports() -> None:
    """``_kernel.ids`` must have no ``alphamind.*`` imports — it is a leaf.

    The ``kernel-leaf`` import-linter contract from story 03 enforces this at
    the package level; this test pins the file-level invariant so a careless
    refactor surfaces immediately under pytest.
    """
    import ast
    from pathlib import Path

    import alphamind._kernel.ids as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not (
                node.module == "alphamind" or node.module.startswith("alphamind.")
            ), f"_kernel.ids must not import from alphamind.*; found: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "alphamind" or alias.name.startswith("alphamind.")), (
                    f"_kernel.ids must not import alphamind.*; found: {alias.name}"
                )


def test_kernel_ids_all_lists_every_public_name() -> None:
    from alphamind._kernel import ids

    assert set(ids.__all__) == {
        "OrderId",
        "PositionId",
        "BracketId",
        "CommandId",
        "AlpacaOrderId",
        "ClientOrderId",
        "EnvelopeId",
        "InvocationId",
        "ThesisId",
        "Symbol",
        "OccSymbol",
        "envelope_id",
        "command_id",
    }
