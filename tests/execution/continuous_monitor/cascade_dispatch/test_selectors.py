"""Tests for the per-rule selector dispatch table (ALP-438)."""

from __future__ import annotations

from alphamind.execution.continuous_monitor.cascade_dispatch import (
    RULE_SELECTOR_DISPATCH,
    selector_for,
)
from alphamind.risk_guardrails.breach_behavior import (
    select_for_drawdown_breach,
    select_for_position_max_loss,
    select_for_single_short_max_size_breach,
)


def test_dispatch_table_covers_every_immediate_engine_rule() -> None:
    """Every rule classified as IMMEDIATE in breach-behavior has a selector.

    Keys mirror ``config/guardrails.yaml`` rule ids whose ``breach_response``
    is ``immediate_engine`` and which can reach the dispatcher via
    ``handle_immediate_breach``. ``margin_call`` flows through the separate
    ``handle_margin_call`` entry point and is intentionally absent.
    """
    expected = {
        "daily_drawdown_pct",
        "cumulative_drawdown_pct",
        "position_max_loss_equity_pct",
        "position_max_loss_options_pct",
        "single_short_max_pct",
    }
    assert set(RULE_SELECTOR_DISPATCH.keys()) == expected


def test_dispatch_table_maps_drawdown_rules_to_drawdown_selector() -> None:
    """Both daily and cumulative drawdown route to ``select_for_drawdown_breach``."""
    assert RULE_SELECTOR_DISPATCH["daily_drawdown_pct"] is select_for_drawdown_breach
    assert RULE_SELECTOR_DISPATCH["cumulative_drawdown_pct"] is select_for_drawdown_breach


def test_dispatch_table_maps_position_max_loss_to_per_position_selector() -> None:
    """Both equity and options per-position max-loss route to ``select_for_position_max_loss``."""
    assert RULE_SELECTOR_DISPATCH["position_max_loss_equity_pct"] is select_for_position_max_loss
    assert RULE_SELECTOR_DISPATCH["position_max_loss_options_pct"] is select_for_position_max_loss


def test_dispatch_table_maps_single_short_to_single_short_selector() -> None:
    """The single-short max-size rule routes to ``select_for_single_short_max_size_breach``."""
    assert RULE_SELECTOR_DISPATCH["single_short_max_pct"] is select_for_single_short_max_size_breach


def test_selector_for_returns_callable_for_known_rule() -> None:
    """``selector_for`` returns the dispatched selector for a known immediate rule."""
    assert selector_for("position_max_loss_equity_pct") is select_for_position_max_loss


def test_selector_for_returns_none_for_deferred_rule() -> None:
    """A deferred-classification rule (e.g., ``sector_concentration``) yields ``None``."""
    assert selector_for("sector_concentration") is None


def test_selector_for_returns_none_for_unknown_rule() -> None:
    """An unknown rule id yields ``None`` (caller handles structural error)."""
    assert selector_for("totally_made_up_rule") is None
