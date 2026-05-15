"""Tests for ``alphamind._kernel.mode`` — the consolidated ``PipelineMode`` enum.

Three parallel ``Mode``-translation helpers historically lived in
``pipeline/decision.py`` (``_ANALYST_MODE_FOR_PIPELINE`` /
``_STRATEGIST_MODE_FOR_PIPELINE``), ``scheduler/orchestrator.py``
(``_mode_to_decision_literal``), and ``scheduler/invocation.py``
(``_mode_to_active_mode_literal``). ALP-472 consolidated them onto
``PipelineMode`` with typed ``to_*`` methods. The two regression-anchor
asserts below pin equivalence to the old dict / function returns so a
refactor that changes the return value surface trips the test.
"""

from __future__ import annotations


def test_pipeline_mode_members_and_values() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert tuple(member.value for member in PipelineMode) == (
        "normal",
        "halt",
        "defensive_posture",
    )


def test_pipeline_mode_module_is_kernel_mode() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.__module__ == "alphamind._kernel.mode"


def test_kernel_mode_has_zero_first_party_imports() -> None:
    """``_kernel.mode`` must have no ``alphamind.*`` imports — it is a leaf."""
    import ast
    from pathlib import Path

    import alphamind._kernel.mode as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not (
                node.module == "alphamind" or node.module.startswith("alphamind.")
            ), f"_kernel.mode must not import from alphamind.*; found: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "alphamind" or alias.name.startswith("alphamind.")), (
                    f"_kernel.mode must not import alphamind.*; found: {alias.name}"
                )


def test_to_analyst_pipeline_mode_halt_matches_old_dict() -> None:
    """``PipelineMode.HALT.to_analyst_pipeline_mode()`` matches the old dict.

    The pre-refactor pipeline used a dict literal
    ``{"normal": "normal", "halt": "watchlist"}``. This regression anchor pins
    the translator method to the same value.
    """
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.HALT.to_analyst_pipeline_mode() == "watchlist"


def test_to_analyst_pipeline_mode_normal() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.NORMAL.to_analyst_pipeline_mode() == "normal"


def test_to_strategist_pipeline_mode_halt_matches_old_dict() -> None:
    """``PipelineMode.HALT.to_strategist_pipeline_mode()`` matches the old dict."""
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.HALT.to_strategist_pipeline_mode() == "defensive_posture"


def test_to_strategist_pipeline_mode_normal() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.NORMAL.to_strategist_pipeline_mode() == "normal"


def test_to_decision_literal_normal() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.NORMAL.to_decision_literal() == "normal"


def test_to_decision_literal_halt() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.HALT.to_decision_literal() == "halt"


def test_to_active_mode_literal_normal() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.NORMAL.to_active_mode_literal() == "normal"


def test_to_active_mode_literal_halt() -> None:
    """``PipelineMode.HALT`` maps to ``"halted"`` for the row's ``active_mode`` column.

    Mirrors the pre-refactor :func:`_mode_to_active_mode_literal` contract:
    the row's ``active_mode`` accepts ``"normal" | "defensive_posture" |
    "halted"``; ``Mode.halt`` (value ``"halt"``) is translated to ``"halted"``
    so direct ``.value`` is wrong.
    """
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.HALT.to_active_mode_literal() == "halted"


def test_to_active_mode_literal_defensive_posture() -> None:
    from alphamind._kernel.mode import PipelineMode

    assert PipelineMode.DEFENSIVE_POSTURE.to_active_mode_literal() == "defensive_posture"


def test_from_config_mode_normal() -> None:
    """``PipelineMode.from_config_mode(Mode.normal)`` returns ``PipelineMode.NORMAL``.

    The config-layer ``Mode`` (``normal | halt``) is the orchestrator's input.
    ``PipelineMode`` is the canonical surface that subsumes it; this adapter
    lets callers stay on the typed PipelineMode without re-translating.
    """
    from alphamind._kernel.mode import PipelineMode
    from alphamind.config.models.modes import Mode

    assert PipelineMode.from_config_mode(Mode.normal) is PipelineMode.NORMAL


def test_from_config_mode_halt() -> None:
    from alphamind._kernel.mode import PipelineMode
    from alphamind.config.models.modes import Mode

    assert PipelineMode.from_config_mode(Mode.halt) is PipelineMode.HALT


def test_from_config_mode_unknown_raises_valueerror() -> None:
    """A future ``Mode`` member that ``PipelineMode`` does not know raises.

    Mirrors the prior :func:`_mode_to_decision_literal` /
    :func:`_mode_to_active_mode_literal` contract: a new ``Mode`` enum member
    raises ``ValueError`` rather than silently mis-translating into a default.
    """
    import pytest

    from alphamind._kernel.mode import PipelineMode

    class FakeMode:
        value = "attentive"

    with pytest.raises(ValueError, match="unexpected"):
        PipelineMode.from_config_mode(FakeMode)
