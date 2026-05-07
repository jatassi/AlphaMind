"""Tests for ThesisHealthSnapshot — story ALP-351."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.records.theses import (
    SupportingSignal,
    SupportingSignalStatus,
    ThesisStatus,
)
from alphamind.portfolio_state.views.thesis_health import (
    ComponentHealthEntry,
    ThesisHealthSnapshot,
)

_NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def _make_entry(component_id: str = "comp-1") -> ComponentHealthEntry:
    return ComponentHealthEntry(
        component_id=component_id,
        supporting_signals=(
            SupportingSignal(name="capex_signal", status=SupportingSignalStatus.PRESENT),
        ),
    )


def test_thesis_health_snapshot_construction() -> None:
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-001",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(_make_entry(),),
    )
    assert snap.thesis_id == "THESIS-1"
    assert snap.invocation_id == "inv-001"
    assert snap.health_status == ThesisStatus.ON_TRACK
    assert snap.prior_health_status is None
    assert len(snap.component_health) == 1


def test_thesis_health_snapshot_is_frozen() -> None:
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-001",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(),
    )
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        snap.health_status = ThesisStatus.AT_RISK


def test_health_for_component_hit() -> None:
    entry_a = _make_entry("comp-a")
    entry_b = _make_entry("comp-b")
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-001",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(entry_a, entry_b),
    )
    found = snap.health_for_component("comp-b")
    assert found is entry_b


def test_health_for_component_miss() -> None:
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-001",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(_make_entry("comp-a"),),
    )
    assert snap.health_for_component("comp-z") is None


def test_empty_component_health_accepted_for_initial_invocation() -> None:
    """Initial-invocation case: no component health yet."""
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-001",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.ON_TRACK,
        prior_health_status=None,
        component_health=(),
    )
    assert snap.component_health == ()
    assert snap.health_for_component("anything") is None


def test_thesis_id_min_length() -> None:
    with pytest.raises(ValidationError):
        ThesisHealthSnapshot(
            thesis_id="",
            invocation_id="inv-001",
            snapshot_timestamp=_NOW,
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=None,
            component_health=(),
        )


def test_invocation_id_min_length() -> None:
    with pytest.raises(ValidationError):
        ThesisHealthSnapshot(
            thesis_id="THESIS-1",
            invocation_id="",
            snapshot_timestamp=_NOW,
            health_status=ThesisStatus.ON_TRACK,
            prior_health_status=None,
            component_health=(),
        )


def test_component_health_entry_is_frozen() -> None:
    entry = _make_entry()
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        entry.component_id = "changed"


def test_component_health_entry_min_id() -> None:
    with pytest.raises(ValidationError):
        ComponentHealthEntry(
            component_id="",
            supporting_signals=(),
        )


def test_prior_health_status_distinct_from_current() -> None:
    """Prior and current can both be present (carry-forward across invocations)."""
    snap = ThesisHealthSnapshot(
        thesis_id="THESIS-1",
        invocation_id="inv-002",
        snapshot_timestamp=_NOW,
        health_status=ThesisStatus.AT_RISK,
        prior_health_status=ThesisStatus.ON_TRACK,
        component_health=(),
    )
    assert snap.prior_health_status == ThesisStatus.ON_TRACK
    assert snap.health_status == ThesisStatus.AT_RISK
