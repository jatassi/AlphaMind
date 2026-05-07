"""Identity tests for the risk-guardrail enum relocation (ALP-344).

These tests pin two invariants:

1. Each of the four relocated enums (``RegimeLabel``, ``RegimeTransitionState``,
   ``DrawdownTier``, ``RiskZone``) lives at its new canonical home under
   ``risk_guardrails/`` AND remains importable from
   ``portfolio_state.records.capital`` (backward-compat re-export).
2. The two imports return the *same class object* — the re-export from
   ``capital.py`` is via Python's import machinery, not a copy.

If either invariant breaks, downstream consumers may end up with two distinct
``DrawdownTier`` classes and silent equality bugs.
"""

from __future__ import annotations


def test_regime_label_canonical_home_is_regime_adaptation_types() -> None:
    from alphamind.portfolio_state.records.capital import RegimeLabel as FromCapital
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as FromCanonical

    assert FromCapital is FromCanonical


def test_regime_transition_state_canonical_home_is_regime_adaptation_types() -> None:
    from alphamind.portfolio_state.records.capital import RegimeTransitionState as FromCapital
    from alphamind.risk_guardrails.regime_adaptation.types import (
        RegimeTransitionState as FromCanonical,
    )

    assert FromCapital is FromCanonical


def test_drawdown_tier_canonical_home_is_breach_behavior_types() -> None:
    from alphamind.portfolio_state.records.capital import DrawdownTier as FromCapital
    from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier as FromCanonical

    assert FromCapital is FromCanonical


def test_risk_zone_canonical_home_is_guardrail_evaluation_types() -> None:
    from alphamind.portfolio_state.records.capital import RiskZone as FromCapital
    from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone as FromCanonical

    assert FromCapital is FromCanonical


def test_drawdown_tiers_module_does_not_import_from_capital() -> None:
    """The story's stated inversion-fix target: drawdown_tiers.py must import
    DrawdownTier from its new canonical home, not from capital.py."""
    import inspect

    from alphamind.risk_guardrails.breach_behavior import drawdown_tiers

    source = inspect.getsource(drawdown_tiers)
    assert "from alphamind.portfolio_state.records.capital import DrawdownTier" not in source, (
        "drawdown_tiers.py must not import DrawdownTier from portfolio_state.records.capital; "
        "import from alphamind.risk_guardrails.breach_behavior.types instead"
    )


def test_no_circular_imports_among_relocated_homes() -> None:
    """Importing all four modules in sequence must succeed without cycles."""
    import importlib

    importlib.import_module("alphamind.portfolio_state.records.capital")
    importlib.import_module("alphamind.risk_guardrails.regime_adaptation.types")
    importlib.import_module("alphamind.risk_guardrails.breach_behavior.types")
    importlib.import_module("alphamind.risk_guardrails.guardrail_evaluation.types")
