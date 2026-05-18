"""Tests for ``alphamind._kernel.calibration`` — the shared calibration vocab.

``CalibrationState`` and ``CALIBRATION_STATE_VALUES`` are imported by every
distillation computation and by the persistence-layer schema CHECK constraints.
Before ALP-457 they lived in ``distillation/calibration.py``, which imported
from ``persistence.models`` (lazily, to dodge the cycle). Hoisting the
vocabulary into ``_kernel`` removes the need for that dodge.
"""

from __future__ import annotations


def test_calibration_state_members_and_values() -> None:
    from alphamind._kernel.calibration import CalibrationState

    assert tuple(member.value for member in CalibrationState) == (
        "calibrated",
        "accumulating",
        "unavailable",
    )


def test_calibration_state_module_is_kernel_calibration() -> None:
    from alphamind._kernel.calibration import CalibrationState

    assert CalibrationState.__module__ == "alphamind._kernel.calibration"


def test_calibration_state_values_tuple_matches_enum() -> None:
    from alphamind._kernel.calibration import (
        CALIBRATION_STATE_VALUES,
        CalibrationState,
    )

    assert tuple(member.value for member in CalibrationState) == CALIBRATION_STATE_VALUES
    assert CALIBRATION_STATE_VALUES == ("calibrated", "accumulating", "unavailable")


def test_kernel_calibration_has_zero_first_party_imports() -> None:
    """``_kernel.calibration`` must have no ``alphamind.*`` imports."""
    import ast
    from pathlib import Path

    import alphamind._kernel.calibration as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not (
                node.module == "alphamind" or node.module.startswith("alphamind.")
            ), f"_kernel.calibration must not import from alphamind.*; found: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "alphamind" or alias.name.startswith("alphamind.")), (
                    f"_kernel.calibration must not import alphamind.*; found: {alias.name}"
                )


def test_kernel_calibration_all_lists_expected_names() -> None:
    from alphamind._kernel import calibration

    assert set(calibration.__all__) == {
        "CALIBRATION_STATE_VALUES",
        "CalibrationState",
    }
