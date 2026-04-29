"""Import smoke test for the regime-adaptation package skeleton (story 02).

Asserts the package imports cleanly and re-exports every typed-record symbol
documented in the story's acceptance criteria.
"""

from __future__ import annotations

import alphamind.risk_guardrails.regime_adaptation as regime_adaptation
import alphamind.risk_guardrails.regime_adaptation.types as types_module


def test_top_level_package_importable() -> None:
    assert regime_adaptation is not None


def test_types_submodule_importable() -> None:
    assert types_module is not None


def test_top_level_reexports_every_typed_record() -> None:
    """Every typed record from `types.py` is reachable from the top-level package."""
    expected_symbols = (
        "CompositeAlertState",
        "EventCalendar",
        "EventCalendarEntry",
        "LOOSENING_INVOCATIONS",
        "NextTransitionDecision",
        "OverlayActivationDecision",
        "RegimeAdaptationAuditEntry",
        "RegimeAdaptationAuditEventKind",
        "RegimeAdaptationOutput",
        "RegimeAdaptationState",
        "RegimeTransitionBreach",
        "RuleMetadata",
        "StaleCalendarReport",
        "VixBoundaryThresholds",
        "overlays_to_strings",
    )
    for name in expected_symbols:
        assert hasattr(regime_adaptation, name), (
            f"top-level package missing re-exported symbol {name!r}"
        )


def test_top_level_reexports_regime_mapping_helpers() -> None:
    """``map_distillation_to_guardrail_regime`` and ``from_regime_classification``
    are reachable from the top-level package (story 03)."""
    expected_symbols = (
        "from_regime_classification",
        "map_distillation_to_guardrail_regime",
    )
    for name in expected_symbols:
        assert hasattr(regime_adaptation, name), (
            f"top-level package missing re-exported symbol {name!r}"
        )
