"""Tests for ``RegTExcessAggregates`` typed record (ALP-429, story 06b).

Lightweight value object summing per-fill ``regt_excess_over_pm`` over three
calendar-day-anchored windows. Exercised end-to-end by the SQL repository
method and the assembler integration test; the tests here cover construction
shape only.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from alphamind.execution.regt_margin_attribution import RegTExcessAggregates


def test_regt_excess_aggregates_constructs_with_three_floats() -> None:
    aggregates = RegTExcessAggregates(
        trailing_30d_usd=10.0,
        trailing_90d_usd=25.0,
        lifetime_usd=100.0,
    )
    assert aggregates.trailing_30d_usd == 10.0
    assert aggregates.trailing_90d_usd == 25.0
    assert aggregates.lifetime_usd == 100.0


def test_regt_excess_aggregates_is_frozen() -> None:
    aggregates: Any = RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )
    # ``Any`` annotation lets mypy through; the runtime ``slots=True`` /
    # ``frozen=True`` descriptor on the actual class still raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        aggregates.trailing_30d_usd = 5.0


def test_regt_excess_aggregates_uses_slots() -> None:
    aggregates = RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )
    # Slotted dataclasses do not have __dict__.
    assert not hasattr(aggregates, "__dict__")
