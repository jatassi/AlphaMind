"""Unit tests for ``alphamind.config.guardrails_helpers``.

The helper consolidates four formerly-duplicated loaders that each parsed
``config/guardrails.yaml`` and selected the ``cumulative_drawdown_pct`` rule's
``progressive_tiers``. The tests cover:

* The loaded tuple is non-empty and every entry is a ``ProgressiveTier``.
* The terminal tier carries ``full_halt=True`` (canonical contract from the
  shipped guardrails config).
* Trigger percentages are strictly monotonically increasing (mirroring the
  ``GuardrailsConfig.progressive_tiers_only_on_cumulative_drawdown`` validator).
* ``@cache`` semantics — repeated calls return the same tuple object.
"""

from __future__ import annotations

from itertools import pairwise

from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
)
from alphamind.config.models.guardrails import ProgressiveTier


def test_loader_returns_non_empty_tuple_of_progressive_tiers() -> None:
    tiers = load_cumulative_drawdown_progressive_tiers()
    assert isinstance(tiers, tuple)
    assert len(tiers) >= 1
    assert all(isinstance(t, ProgressiveTier) for t in tiers)


def test_loader_terminal_tier_is_full_halt() -> None:
    """The cumulative-drawdown rule's last tier is the terminal full-halt tier."""
    tiers = load_cumulative_drawdown_progressive_tiers()
    assert tiers[-1].full_halt is True


def test_loader_trigger_pct_strictly_increasing() -> None:
    """Trigger percentages must be strictly increasing — locked by the
    ``GuardrailsConfig`` validator; this test guards against drift in the
    loaded sequence."""
    tiers = load_cumulative_drawdown_progressive_tiers()
    triggers = [t.trigger_pct for t in tiers]
    assert all(b > a for a, b in pairwise(triggers))


def test_loader_cached_returns_same_object() -> None:
    """``@functools.cache`` means repeated calls return the same tuple object."""
    first = load_cumulative_drawdown_progressive_tiers()
    second = load_cumulative_drawdown_progressive_tiers()
    assert first is second
