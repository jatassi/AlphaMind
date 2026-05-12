"""Identity tests for the risk-guardrail enums after ALP-457 hoist into ``_kernel``.

ALP-344 moved the four enums (``RegimeLabel``, ``RegimeTransitionState``,
``DrawdownTier``, ``RiskZone``) out of ``portfolio_state.records.capital`` into
``risk_guardrails/`` submodules. ALP-457 moved them again into
``alphamind._kernel.regime`` so they sit beneath every layer in the import
graph and break the 10-module cycle that had ``capital.py`` at its center.

These tests pin two invariants:

1. The four enums live at ``alphamind._kernel.regime`` (canonical home).
2. The legacy ``risk_guardrails`` import paths still resolve, returning the
   same class objects (re-export via Python's import machinery, not
   duplicated definitions). Downstream consumers may still walk through the
   ``risk_guardrails.breach_behavior`` / ``regime_adaptation`` /
   ``guardrail_evaluation`` packages; both paths must yield the same enum.
"""

from __future__ import annotations


def test_regime_label_canonical_home_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RegimeLabel as FromKernel
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as FromGuardrails

    assert FromKernel is FromGuardrails


def test_regime_transition_state_canonical_home_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RegimeTransitionState as FromKernel
    from alphamind.risk_guardrails.regime_adaptation.types import (
        RegimeTransitionState as FromGuardrails,
    )

    assert FromKernel is FromGuardrails


def test_drawdown_tier_canonical_home_is_kernel_regime() -> None:
    from alphamind._kernel.regime import DrawdownTier as FromKernel
    from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier as FromGuardrails

    assert FromKernel is FromGuardrails


def test_risk_zone_canonical_home_is_kernel_regime() -> None:
    from alphamind._kernel.regime import RiskZone as FromKernel
    from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone as FromGuardrails

    assert FromKernel is FromGuardrails


def test_no_circular_imports_among_relocated_homes() -> None:
    """Importing all relocated homes in sequence must succeed without cycles."""
    import importlib

    importlib.import_module("alphamind._kernel.regime")
    importlib.import_module("alphamind.risk_guardrails.regime_adaptation.types")
    importlib.import_module("alphamind.risk_guardrails.breach_behavior.types")
    importlib.import_module("alphamind.risk_guardrails.guardrail_evaluation.types")
