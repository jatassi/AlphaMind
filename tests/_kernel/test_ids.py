"""Tests for ``alphamind._kernel.ids`` — NewType aliases for identifiers.

Each alias is a distinct type at type-check time but a plain ``str`` at runtime.
Constructor functions for IDs with known regex patterns (envelope IDs, command
IDs) validate once at the boundary so downstream code can trust the typed
value.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_order_id_is_str_at_runtime() -> None:
    from alphamind._kernel.ids import OrderId

    value = OrderId("abc123")
    assert isinstance(value, str)
    assert value == "abc123"


def test_every_newtype_alias_is_str_at_runtime() -> None:
    """All NewType aliases pass through to ``str`` at runtime.

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
        RecommendationId,
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
        RecommendationId,
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
        "RecommendationId": ids.RecommendationId,
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
        "RecommendationId",
        "ThesisId",
        "Symbol",
        "OccSymbol",
        "envelope_id",
        "command_id",
        "recommendation_id",
        "make_symbol",
        "make_occ_symbol",
    }


def test_make_symbol_accepts_plain_us_equity_ticker() -> None:
    from alphamind._kernel.ids import make_symbol

    for raw in ("A", "AAPL", "GOOGL", "JPM", "NVDA"):
        value = make_symbol(raw)
        assert isinstance(value, str)
        assert value == raw


def test_make_symbol_accepts_multi_share_class_dot_ticker() -> None:
    """``BRK.B`` / ``BF.B``-style multi-share-class tickers are valid."""
    from alphamind._kernel.ids import make_symbol

    for raw in ("BRK.B", "BF.B"):
        assert make_symbol(raw) == raw


def test_make_symbol_rejects_empty_whitespace_and_lowercase() -> None:
    """``make_symbol`` rejects the malformations called out in ALP-640."""
    import pytest

    from alphamind._kernel.ids import make_symbol

    for raw in (
        "",
        " ",
        "with spaces",
        "aapl",
        "AAPL ",
        " AAPL",
        ".AAPL",
        "AAPL.",
    ):
        with pytest.raises(ValueError, match="make_symbol"):
            make_symbol(raw)


def test_make_symbol_rejects_overlong_input() -> None:
    """A garbage string longer than any plausible ticker is rejected.

    The cap is set so ``BRK.B``-style multi-share-class tickers pass while
    obvious malformations ("garbage-with-spaces"-length non-tickers) fail
    at the boundary, surfacing the bug rather than propagating downstream.
    """
    import pytest

    from alphamind._kernel.ids import make_symbol

    for raw in ("ABCDEFGHIJK", "A" * 11):
        with pytest.raises(ValueError, match="make_symbol"):
            make_symbol(raw)


def test_make_occ_symbol_accepts_canonical_format() -> None:
    """Compressed form: ``AAPL250620C00200000`` (no space padding).

    Both compressed and the OPRA-canonical space-padded 21-char form
    (``"NVDA  260619C00800000"``) are accepted — the production builder
    ``build_occ_symbol`` emits the padded form, but ``OccSymbol`` consumers
    may receive either from external boundaries (broker payloads, strategist
    proposals), so the constructor accepts both.
    """
    from alphamind._kernel.ids import make_occ_symbol

    for raw in (
        "AAPL250620C00200000",
        "SPY250117P00450000",
        "A250620C00050000",
        "GOOGL260116P00150000",
    ):
        value = make_occ_symbol(raw)
        assert isinstance(value, str)
        assert value == raw


def test_make_occ_symbol_accepts_opra_space_padded_form() -> None:
    """OPRA fixed-21-char form: root is left-padded with spaces to six chars.

    This is what ``alphamind.execution.broker_adapter.order_options.build_occ_symbol``
    emits and what Alpaca returns on fill events.
    """
    from alphamind._kernel.ids import make_occ_symbol

    for raw in (
        "NVDA  260619C00800000",  # 4-letter root + 2 spaces
        "SPY   250117P00450000",  # 3-letter root + 3 spaces
        "A     250620C00050000",  # 1-letter root + 5 spaces
        "GOOGL 260116P00150000",  # 5-letter root + 1 space
        "AAAAAA250620C00200000",  # 6-letter root + 0 spaces (canonical 21-char no padding)
    ):
        assert make_occ_symbol(raw) == raw


def test_make_occ_symbol_rejects_invalid_input() -> None:
    import pytest

    from alphamind._kernel.ids import make_occ_symbol

    for raw in (
        "",
        "garbage",
        "AAPL250620X00200000",
        "aapl250620C00200000",
        "AAPL25062C00200000",
        "AAPL250620C0020000",
        "AAPL250620C002000000",
        "AAPL.250620C00200000",
        "TOOLONG250620C00200000",
        " AAPL250620C00200000",
    ):
        with pytest.raises(ValueError, match="make_occ_symbol"):
            make_occ_symbol(raw)


def test_recommendation_id_accepts_analyst_pattern() -> None:
    """Analyst-originated recommendation IDs match ``^REC-[0-9]+$``."""
    from alphamind._kernel.ids import recommendation_id

    for raw in ("REC-1", "REC-42", "REC-999"):
        assert recommendation_id(raw) == raw


def test_recommendation_id_accepts_strategist_pattern() -> None:
    """Strategist-originated recommendation IDs match ``^SA(-ORD)?-[0-9]+$``.

    Both analyst and strategist recommendation IDs share the
    :class:`RecommendationId` type; the constructor accepts either valid shape.
    """
    from alphamind._kernel.ids import recommendation_id

    for raw in ("SA-1", "SA-ORD-7", "SA-99"):
        assert recommendation_id(raw) == raw


def test_recommendation_id_rejects_invalid_input() -> None:
    import pytest

    from alphamind._kernel.ids import recommendation_id

    for raw in ("", "REC-", "SA-", "ENV-REC-1", "random", "REC-abc"):
        with pytest.raises(ValueError, match="recommendation_id"):
            recommendation_id(raw)


def test_synthetic_id_confusion_fails_mypy() -> None:
    """``mypy`` flags passing one ID type to a function expecting another.

    This is the load-bearing assertion of the story: NewType aliases must
    be distinct at type-check time, so an accidental cross-assignment of
    ``EnvelopeId`` to a parameter expecting ``PositionId`` is caught by the
    type checker.

    The snippet is written to ``src/alphamind/_kernel/`` (so the project's
    mypy config can resolve ``alphamind.*`` imports) and removed regardless
    of test outcome. Modeled after the synthetic-violation pattern in
    ``tests/test_import_linter.py``. The project's ``[tool.mypy]`` config
    already sets ``strict = true``.
    """
    import subprocess

    target = PROJECT_ROOT / "src" / "alphamind" / "_kernel" / "_synthetic_violation.py"
    snippet = (
        '"""Synthetic ID-confusion module used by tests/_kernel/test_ids.py."""\n\n'
        "from alphamind._kernel.ids import EnvelopeId, PositionId\n\n\n"
        "def _takes_position(p: PositionId) -> None: ...\n\n\n"
        "_e: EnvelopeId = EnvelopeId('ENV-REC-1')\n"
        "_takes_position(_e)\n"
    )
    try:
        target.write_text(snippet, encoding="utf-8")
        result = subprocess.run(
            ["uv", "run", "mypy", str(target)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    finally:
        target.unlink(missing_ok=True)

    assert result.returncode != 0, (
        "mypy accepted passing an EnvelopeId to a function expecting "
        "PositionId — the NewType aliases are not distinct at type-check time.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "PositionId" in combined and "EnvelopeId" in combined, (
        "mypy failed but did not name the PositionId / EnvelopeId mismatch.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
