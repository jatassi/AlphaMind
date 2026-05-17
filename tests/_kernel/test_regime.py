"""Tests for ``alphamind._kernel.regime`` — the shared regime/zone/tier enums.

This module holds the four ``StrEnum`` definitions every guardrail/distillation
consumer imports. Two invariants matter:

1. The enum members and their string values are exactly as currently in use
   downstream (regime-adaptation tables, drawdown tier overlays, zone-classifier
   output).
2. ``__module__`` resolves to ``alphamind._kernel.regime`` — proving each enum
   was defined here (not re-exported from a previous home that the consumer
   might still pick up via a stale path).
"""

from __future__ import annotations


def test_regime_label_members_and_values() -> None:
    from alphamind._kernel.regime import RegimeLabel

    assert tuple(member.value for member in RegimeLabel) == (
        "LOW_VOL",
        "NORMAL",
        "ELEVATED",
        "CRISIS",
    )


def test_regime_label_module_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RegimeLabel

    assert RegimeLabel.__module__ == "alphamind._kernel.regime"


def test_regime_transition_state_members_and_values() -> None:
    from alphamind._kernel.regime import RegimeTransitionState

    assert tuple(member.value for member in RegimeTransitionState) == (
        "STABLE",
        "TIGHTENING",
        "LOOSENING",
    )


def test_regime_transition_state_module_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RegimeTransitionState

    assert RegimeTransitionState.__module__ == "alphamind._kernel.regime"


def test_risk_zone_members_and_values() -> None:
    from alphamind._kernel.regime import RiskZone

    assert tuple(member.value for member in RiskZone) == (
        "NORMAL",
        "WARNING",
        "CRITICAL",
        "BLOCKED",
    )


def test_risk_zone_module_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RiskZone

    assert RiskZone.__module__ == "alphamind._kernel.regime"


def test_drawdown_tier_members_and_values() -> None:
    from alphamind._kernel.regime import DrawdownTier

    assert tuple(member.value for member in DrawdownTier) == (
        "CONSTRAINED",
        "HEAVILY_CONSTRAINED",
        "FULL_HALT",
    )


def test_drawdown_tier_module_is_kernel_regime() -> None:
    from alphamind._kernel.regime import DrawdownTier

    assert DrawdownTier.__module__ == "alphamind._kernel.regime"


def test_kernel_regime_has_zero_first_party_imports() -> None:
    """``_kernel.regime`` must have no ``alphamind.*`` imports — it is a leaf."""
    import ast
    from pathlib import Path

    import alphamind._kernel.regime as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or not (
                node.module == "alphamind" or node.module.startswith("alphamind.")
            ), f"_kernel.regime must not import from alphamind.*; found: {node.module}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "alphamind" or alias.name.startswith("alphamind.")), (
                    f"_kernel.regime must not import alphamind.*; found: {alias.name}"
                )


def test_kernel_regime_all_lists_documented_surface() -> None:
    from alphamind._kernel import regime

    assert set(regime.__all__) == {
        "DrawdownTier",
        "RegimeLabel",
        "RegimeTransitionState",
        "RiskZone",
        "classify_consumption_zone",
    }
