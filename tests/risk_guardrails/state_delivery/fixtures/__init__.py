"""Fixture builders for the state-delivery renderer tests.

Centralizes the repeated _make_budget_entry (and a few other small
parameter / view-adjacent builders) that were copy-pasted across the
test modules under state_delivery/. See builders.py for the implementations.

Pattern mirrors tests/risk_guardrails/breach_behavior/fixtures/.
"""

from tests.risk_guardrails.state_delivery.fixtures.builders import (
    _DEFAULT_POSITION_ZONES,
    _make_budget_entry,
    _make_directional,
    _make_param_entry,
    _make_state_delivery_config,
    _make_thesis_quality_aggregates,
)

__all__ = [
    "_DEFAULT_POSITION_ZONES",
    "_make_budget_entry",
    "_make_directional",
    "_make_param_entry",
    "_make_state_delivery_config",
    "_make_thesis_quality_aggregates",
]
