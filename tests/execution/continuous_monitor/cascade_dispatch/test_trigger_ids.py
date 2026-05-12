"""Tests for the per-session monotonic trigger-id generator (ALP-438)."""

from __future__ import annotations

import pytest

from alphamind.execution.continuous_monitor.cascade_dispatch import (
    TriggerIdGenerator,
)


def test_generator_starts_at_one_by_default() -> None:
    """A fresh generator yields 1 as its first id."""
    gen = TriggerIdGenerator(session_id="mon-session-a")
    assert gen.next() == 1


def test_generator_is_strictly_increasing() -> None:
    """Sequential ``next()`` calls yield strictly increasing ids with no gaps."""
    gen = TriggerIdGenerator(session_id="mon-session-a")
    ids = [gen.next() for _ in range(50)]
    assert ids == list(range(1, 51))


def test_generator_with_custom_start_uses_it() -> None:
    """``start`` controls the first id; values are gapless from there."""
    gen = TriggerIdGenerator(session_id="mon-session-a", start=10)
    assert [gen.next() for _ in range(3)] == [10, 11, 12]


def test_independent_generators_do_not_share_state() -> None:
    """A fresh generator for a new session restarts at 1 — sessions are independent."""
    first = TriggerIdGenerator(session_id="mon-session-a")
    first.next()
    first.next()
    second = TriggerIdGenerator(session_id="mon-session-b")
    assert second.next() == 1


def test_rejects_empty_session_id() -> None:
    """An empty session_id is a structural error."""
    with pytest.raises(ValueError, match="session_id"):
        TriggerIdGenerator(session_id="")


def test_rejects_start_below_one() -> None:
    """``start`` must be a positive integer; zero or negative is rejected."""
    with pytest.raises(ValueError, match="start"):
        TriggerIdGenerator(session_id="mon-session-a", start=0)
