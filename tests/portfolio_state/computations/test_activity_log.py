"""Tests for activity-log filtering pure functions (story 05e)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alphamind._kernel.ids import OrderId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import price
from alphamind.config.models.distillation import (
    AnomalyDetection,
    DistillationConfig,
    LeadLag,
    LeadLagPair,
    NarrativeLag,
    PersistenceWindows,
    PredictionMarket,
    RegimeClassification,
    RegimeTransition,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_UTC = UTC


def _pm_detail() -> PMDecisionDetail:
    return PMDecisionDetail(
        envelope_id="env-1",
        source_provenance_json={},
        evaluation_json={},
        modifications_json=[],
        resulting_command_ids=(),
        verdict=PMVerdict.APPROVE,
    )


def _pos_detail() -> PositionOpenedDetail:
    return PositionOpenedDetail(
        ticker=Symbol("AAPL"),
        direction="LONG",
        fill_price=price("150.0"),
        quantity=100.0,
        thesis_id=None,
        bracket_id=None,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )


def _pm_entry(
    entry_id: str = "e-pm",
    invocation_id: str = "inv-1",
    timestamp: datetime | None = None,
    position_id: str | None = "pos-1",
    order_id: str | None = None,
    thesis_id: str | None = None,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
) -> ActivityLogEntry:
    ts = timestamp or datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=ts,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=_pm_detail(),
    )


def _pos_entry(
    entry_id: str = "e-pos",
    invocation_id: str = "inv-1",
    timestamp: datetime | None = None,
    position_id: str | None = "pos-1",
    order_id: str | None = "ord-1",
    thesis_id: str | None = "thesis-1",
    source: EventSource = EventSource.FILL_PROCESSOR,
) -> ActivityLogEntry:
    ts = timestamp or datetime(2024, 1, 1, 12, 1, 0, tzinfo=_UTC)
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=ts,
        event_type=EventType.POSITION_OPENED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=_pos_detail(),
    )


# ---------------------------------------------------------------------------
# filter_by_invocation_id
# ---------------------------------------------------------------------------


def test_filter_by_invocation_id_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_invocation_id

    e1 = _pm_entry(entry_id="e1", invocation_id="inv-A")
    e2 = _pos_entry(entry_id="e2", invocation_id="inv-B")
    e3 = _pm_entry(entry_id="e3", invocation_id="inv-A")
    entries = (e1, e2, e3)

    result = filter_by_invocation_id(entries, "inv-A")

    assert result == (e1, e3)


def test_filter_by_invocation_id_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_invocation_id

    assert filter_by_invocation_id((), "inv-A") == ()


def test_filter_by_invocation_id_no_match() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_invocation_id

    e1 = _pm_entry(invocation_id="inv-A")
    assert filter_by_invocation_id((e1,), "inv-Z") == ()


def test_filter_by_invocation_id_input_unchanged() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_invocation_id

    e1 = _pm_entry(entry_id="e1", invocation_id="inv-A")
    e2 = _pos_entry(entry_id="e2", invocation_id="inv-B")
    original = (e1, e2)
    filter_by_invocation_id(original, "inv-A")
    assert original == (e1, e2)


# ---------------------------------------------------------------------------
# filter_by_event_type
# ---------------------------------------------------------------------------


def test_filter_by_event_type_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_type

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    e3 = _pm_entry(entry_id="e3")
    entries = (e1, e2, e3)

    result = filter_by_event_type(entries, EventType.PM_DECISION)

    assert result == (e1, e3)


def test_filter_by_event_type_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_type

    assert filter_by_event_type((), EventType.PM_DECISION) == ()


def test_filter_by_event_type_no_match() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_type

    e1 = _pm_entry()
    assert filter_by_event_type((e1,), EventType.POSITION_OPENED) == ()


# ---------------------------------------------------------------------------
# filter_by_event_types
# ---------------------------------------------------------------------------


def test_filter_by_event_types_union() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_types

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    entries = (e1, e2)

    result = filter_by_event_types(
        entries,
        frozenset({EventType.PM_DECISION, EventType.POSITION_OPENED}),
    )

    assert result == (e1, e2)


def test_filter_by_event_types_single_type() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_types

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    entries = (e1, e2)

    result = filter_by_event_types(entries, frozenset({EventType.PM_DECISION}))

    assert result == (e1,)


def test_filter_by_event_types_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_types

    assert filter_by_event_types((), frozenset({EventType.PM_DECISION})) == ()


def test_filter_by_event_types_empty_frozenset() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_types

    e1 = _pm_entry()
    assert filter_by_event_types((e1,), frozenset()) == ()


# ---------------------------------------------------------------------------
# filter_by_event_group
# ---------------------------------------------------------------------------


def test_filter_by_event_group_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_group

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    e3 = _pm_entry(entry_id="e3")
    entries = (e1, e2, e3)

    result = filter_by_event_group(entries, EventGroup.PM_DECISION)

    assert result == (e1, e3)


def test_filter_by_event_group_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_group

    assert filter_by_event_group((), EventGroup.PM_DECISION) == ()


def test_filter_by_event_group_no_match() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_event_group

    e1 = _pm_entry()
    assert filter_by_event_group((e1,), EventGroup.BRACKET) == ()


# ---------------------------------------------------------------------------
# filter_by_source
# ---------------------------------------------------------------------------


def test_filter_by_source_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_source

    e1 = _pm_entry(entry_id="e1", source=EventSource.COMMAND_EXECUTOR)
    e2 = _pos_entry(entry_id="e2", source=EventSource.FILL_PROCESSOR)
    entries = (e1, e2)

    result = filter_by_source(entries, EventSource.FILL_PROCESSOR)

    assert result == (e2,)


def test_filter_by_source_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_source

    assert filter_by_source((), EventSource.FILL_PROCESSOR) == ()


# ---------------------------------------------------------------------------
# filter_by_position_id
# ---------------------------------------------------------------------------


def test_filter_by_position_id_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_position_id

    e1 = _pm_entry(entry_id="e1", position_id=PositionId("pos-A"))
    e2 = _pos_entry(entry_id="e2", position_id=PositionId("pos-B"))
    e3 = _pm_entry(entry_id="e3", position_id=PositionId("pos-A"))
    entries = (e1, e2, e3)

    result = filter_by_position_id(entries, "pos-A")

    assert result == (e1, e3)


def test_filter_by_position_id_excludes_none() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_position_id

    e_with = _pm_entry(entry_id="e1", position_id=PositionId("pos-A"))
    e_none = _pm_entry(entry_id="e2", position_id=None)
    entries = (e_with, e_none)

    result = filter_by_position_id(entries, "pos-A")

    assert result == (e_with,)
    # e_none excluded because position_id is None
    assert e_none not in result


def test_filter_by_position_id_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_position_id

    assert filter_by_position_id((), "pos-A") == ()


# ---------------------------------------------------------------------------
# filter_by_order_id
# ---------------------------------------------------------------------------


def test_filter_by_order_id_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_order_id

    e1 = _pos_entry(entry_id="e1", order_id=OrderId("ord-1"))
    e2 = _pos_entry(entry_id="e2", order_id=OrderId("ord-2"))
    e3 = _pm_entry(entry_id="e3", order_id=None)
    entries = (e1, e2, e3)

    result = filter_by_order_id(entries, "ord-1")

    assert result == (e1,)


def test_filter_by_order_id_excludes_none() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_order_id

    e_none = _pm_entry(entry_id="e1", order_id=None)
    assert filter_by_order_id((e_none,), "ord-1") == ()


def test_filter_by_order_id_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_order_id

    assert filter_by_order_id((), "ord-1") == ()


# ---------------------------------------------------------------------------
# filter_by_thesis_id
# ---------------------------------------------------------------------------


def test_filter_by_thesis_id_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_thesis_id

    e1 = _pos_entry(entry_id="e1", thesis_id=ThesisId("thesis-X"))
    e2 = _pos_entry(entry_id="e2", thesis_id=ThesisId("thesis-Y"))
    e3 = _pm_entry(entry_id="e3", thesis_id=None)
    entries = (e1, e2, e3)

    result = filter_by_thesis_id(entries, "thesis-X")

    assert result == (e1,)


def test_filter_by_thesis_id_excludes_none() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_thesis_id

    e_none = _pm_entry(thesis_id=None)
    assert filter_by_thesis_id((e_none,), "thesis-X") == ()


def test_filter_by_thesis_id_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_thesis_id

    assert filter_by_thesis_id((), "thesis-X") == ()


# ---------------------------------------------------------------------------
# filter_by_timestamp_range
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=_UTC)
_T1 = datetime(2024, 1, 1, 11, 0, 0, tzinfo=_UTC)
_T2 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
_T3 = datetime(2024, 1, 1, 13, 0, 0, tzinfo=_UTC)


def test_filter_by_timestamp_range_inclusive_start_exclusive_end() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    e0 = _pm_entry(entry_id="e0", timestamp=_T0)
    e1 = _pm_entry(entry_id="e1", timestamp=_T1)  # start — included
    e2 = _pm_entry(entry_id="e2", timestamp=_T2)  # end — excluded
    e3 = _pm_entry(entry_id="e3", timestamp=_T3)
    entries = (e0, e1, e2, e3)

    result = filter_by_timestamp_range(entries, start=_T1, end=_T2)

    assert result == (e1,)


def test_filter_by_timestamp_range_unbounded_start() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    e0 = _pm_entry(entry_id="e0", timestamp=_T0)
    e1 = _pm_entry(entry_id="e1", timestamp=_T1)
    e2 = _pm_entry(entry_id="e2", timestamp=_T2)
    entries = (e0, e1, e2)

    result = filter_by_timestamp_range(entries, start=None, end=_T2)

    assert result == (e0, e1)


def test_filter_by_timestamp_range_unbounded_end() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    e0 = _pm_entry(entry_id="e0", timestamp=_T0)
    e1 = _pm_entry(entry_id="e1", timestamp=_T1)
    e2 = _pm_entry(entry_id="e2", timestamp=_T2)
    entries = (e0, e1, e2)

    result = filter_by_timestamp_range(entries, start=_T1, end=None)

    assert result == (e1, e2)


def test_filter_by_timestamp_range_both_none() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    e0 = _pm_entry(entry_id="e0", timestamp=_T0)
    e1 = _pm_entry(entry_id="e1", timestamp=_T1)
    entries = (e0, e1)

    result = filter_by_timestamp_range(entries, start=None, end=None)

    assert result == (e0, e1)


def test_filter_by_timestamp_range_reversed_range_returns_empty() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    e1 = _pm_entry(entry_id="e1", timestamp=_T1)
    e2 = _pm_entry(entry_id="e2", timestamp=_T2)
    entries = (e1, e2)

    result = filter_by_timestamp_range(entries, start=_T2, end=_T1)

    assert result == ()


def test_filter_by_timestamp_range_mixed_tz_raises() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    naive = aware.replace(tzinfo=None)

    with pytest.raises(ValueError):
        filter_by_timestamp_range((), start=naive, end=aware)

    with pytest.raises(ValueError):
        filter_by_timestamp_range((), start=aware, end=naive)


def test_filter_by_timestamp_range_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_timestamp_range

    assert filter_by_timestamp_range((), start=_T0, end=_T3) == ()


# ---------------------------------------------------------------------------
# intra_invocation_changelog
# ---------------------------------------------------------------------------


def test_intra_invocation_changelog_equivalence() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        filter_by_invocation_id,
        intra_invocation_changelog,
    )

    e1 = _pm_entry(entry_id="e1", invocation_id="inv-A")
    e2 = _pos_entry(entry_id="e2", invocation_id="inv-B")
    entries = (e1, e2)

    assert intra_invocation_changelog(entries, "inv-A") == filter_by_invocation_id(entries, "inv-A")


# ---------------------------------------------------------------------------
# pm_decision_log
# ---------------------------------------------------------------------------


def test_pm_decision_log_returns_pm_entries_only() -> None:
    from alphamind.portfolio_state.computations.activity_log import pm_decision_log

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    e3 = _pm_entry(entry_id="e3")
    entries = (e1, e2, e3)

    result = pm_decision_log(entries)

    assert result == (e1, e3)
    for e in result:
        assert e.event_type == EventType.PM_DECISION


def test_pm_decision_log_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import pm_decision_log

    assert pm_decision_log(()) == ()


# ---------------------------------------------------------------------------
# position_modification_trail
# ---------------------------------------------------------------------------


def test_position_modification_trail_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    e1 = _pm_entry(entry_id="e1", position_id=PositionId("pos-A"))
    e2 = _pos_entry(entry_id="e2", position_id=PositionId("pos-B"))
    e3 = _pm_entry(entry_id="e3", position_id=PositionId("pos-A"))
    entries = (e1, e2, e3)

    result = position_modification_trail(entries, ("pos-A", "pos-B"))

    assert result["pos-A"] == (e1, e3)
    assert result["pos-B"] == (e2,)


def test_position_modification_trail_unknown_id_absent() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    e1 = _pm_entry(position_id=PositionId("pos-A"))
    entries = (e1,)

    result = position_modification_trail(entries, ("pos-A", "pos-UNKNOWN"))

    assert "pos-A" in result
    assert "pos-UNKNOWN" not in result


def test_position_modification_trail_empty_position_ids() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    e1 = _pm_entry(position_id=PositionId("pos-A"))
    assert position_modification_trail((e1,), ()) == {}


def test_position_modification_trail_empty_entries() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    result = position_modification_trail((), ("pos-A",))
    # No entries match, so pos-A key absent
    assert result == {}


# ---------------------------------------------------------------------------
# recent_pm_decisions_for_position
# ---------------------------------------------------------------------------


def test_recent_pm_decisions_for_position_happy_path() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    base = datetime(2024, 1, 1, 10, 0, 0, tzinfo=_UTC)
    e1 = _pm_entry(entry_id="e1", position_id=PositionId("pos-A"), timestamp=base)
    e2 = _pm_entry(
        entry_id="e2", position_id=PositionId("pos-A"), timestamp=base + timedelta(hours=1)
    )
    e3 = _pm_entry(
        entry_id="e3", position_id=PositionId("pos-A"), timestamp=base + timedelta(hours=2)
    )
    e4 = _pos_entry(
        entry_id="e4", position_id=PositionId("pos-A"), timestamp=base + timedelta(hours=3)
    )
    entries = (e1, e2, e3, e4)

    result = recent_pm_decisions_for_position(entries, "pos-A", limit=2)

    # Should be last 2 PM_DECISION entries for pos-A: e2, e3
    assert result == (e2, e3)


def test_recent_pm_decisions_for_position_limit_exceeds_matches() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    e1 = _pm_entry(entry_id="e1", position_id=PositionId("pos-A"))
    entries = (e1,)

    result = recent_pm_decisions_for_position(entries, "pos-A", limit=10)

    assert result == (e1,)


def test_recent_pm_decisions_for_position_no_matches() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    e1 = _pos_entry(entry_id="e1", position_id=PositionId("pos-A"))
    assert recent_pm_decisions_for_position((e1,), "pos-A", limit=2) == ()


def test_recent_pm_decisions_for_position_zero_limit_raises() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    with pytest.raises(ValueError):
        recent_pm_decisions_for_position((), "pos-A", limit=0)


def test_recent_pm_decisions_for_position_negative_limit_raises() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    with pytest.raises(ValueError):
        recent_pm_decisions_for_position((), "pos-A", limit=-5)


# ---------------------------------------------------------------------------
# partition_by_event_group
# ---------------------------------------------------------------------------


def test_partition_by_event_group_groups_correctly() -> None:
    from alphamind.portfolio_state.computations.activity_log import partition_by_event_group

    e1 = _pm_entry(entry_id="e1")
    e2 = _pos_entry(entry_id="e2")
    e3 = _pm_entry(entry_id="e3")
    entries = (e1, e2, e3)

    result = partition_by_event_group(entries)

    assert result[EventGroup.PM_DECISION] == (e1, e3)
    assert result[EventGroup.POSITION_LIFECYCLE] == (e2,)


def test_partition_by_event_group_absent_groups_have_no_key() -> None:
    from alphamind.portfolio_state.computations.activity_log import partition_by_event_group

    e1 = _pm_entry(entry_id="e1")
    entries = (e1,)

    result = partition_by_event_group(entries)

    assert EventGroup.BRACKET not in result
    assert EventGroup.THESIS not in result
    assert EventGroup.POSITION_LIFECYCLE not in result


def test_partition_by_event_group_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import partition_by_event_group

    assert partition_by_event_group(()) == {}


# ---------------------------------------------------------------------------
# chronological_sort
# ---------------------------------------------------------------------------


def test_chronological_sort_orders_ascending() -> None:
    from alphamind.portfolio_state.computations.activity_log import chronological_sort

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    e1 = _pm_entry(entry_id="e1", timestamp=base + timedelta(hours=2))
    e2 = _pos_entry(entry_id="e2", timestamp=base)
    e3 = _pm_entry(entry_id="e3", timestamp=base + timedelta(hours=1))
    entries = (e1, e2, e3)

    result = chronological_sort(entries)

    assert result == (e2, e3, e1)


def test_chronological_sort_ties_broken_by_entry_id() -> None:
    from alphamind.portfolio_state.computations.activity_log import chronological_sort

    ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    e_b = _pm_entry(entry_id="e_b", timestamp=ts)
    e_a = _pos_entry(entry_id="e_a", timestamp=ts)
    entries = (e_b, e_a)

    result = chronological_sort(entries)

    # e_a < e_b lexicographically
    assert result == (e_a, e_b)


def test_chronological_sort_empty_input() -> None:
    from alphamind.portfolio_state.computations.activity_log import chronological_sort

    assert chronological_sort(()) == ()


def test_chronological_sort_input_unchanged() -> None:
    from alphamind.portfolio_state.computations.activity_log import chronological_sort

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    e1 = _pm_entry(entry_id="e1", timestamp=base + timedelta(hours=1))
    e2 = _pos_entry(entry_id="e2", timestamp=base)
    original = (e1, e2)
    chronological_sort(original)
    assert original == (e1, e2)


# ---------------------------------------------------------------------------
# Determinism — all functions produce identical output on repeated calls
# ---------------------------------------------------------------------------


def test_all_functions_deterministic() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        chronological_sort,
        filter_by_event_group,
        filter_by_event_type,
        filter_by_event_types,
        filter_by_invocation_id,
        filter_by_order_id,
        filter_by_position_id,
        filter_by_source,
        filter_by_thesis_id,
        filter_by_timestamp_range,
        intra_invocation_changelog,
        partition_by_event_group,
        pm_decision_log,
        position_modification_trail,
        recent_pm_decisions_for_position,
    )

    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=_UTC)
    e1 = _pm_entry(entry_id="e1", timestamp=base)
    e2 = _pos_entry(entry_id="e2", timestamp=base + timedelta(hours=1))
    entries = (e1, e2)

    assert filter_by_invocation_id(entries, "inv-1") == filter_by_invocation_id(entries, "inv-1")
    assert filter_by_event_type(entries, EventType.PM_DECISION) == filter_by_event_type(
        entries, EventType.PM_DECISION
    )
    assert filter_by_event_types(
        entries, frozenset({EventType.PM_DECISION})
    ) == filter_by_event_types(entries, frozenset({EventType.PM_DECISION}))
    assert filter_by_event_group(entries, EventGroup.PM_DECISION) == filter_by_event_group(
        entries, EventGroup.PM_DECISION
    )
    assert filter_by_source(entries, EventSource.FILL_PROCESSOR) == filter_by_source(
        entries, EventSource.FILL_PROCESSOR
    )
    assert filter_by_position_id(entries, "pos-1") == filter_by_position_id(entries, "pos-1")
    assert filter_by_order_id(entries, "ord-1") == filter_by_order_id(entries, "ord-1")
    assert filter_by_thesis_id(entries, "thesis-1") == filter_by_thesis_id(entries, "thesis-1")
    assert filter_by_timestamp_range(entries, start=None, end=None) == filter_by_timestamp_range(
        entries, start=None, end=None
    )
    assert intra_invocation_changelog(entries, "inv-1") == intra_invocation_changelog(
        entries, "inv-1"
    )
    assert pm_decision_log(entries) == pm_decision_log(entries)
    assert position_modification_trail(entries, ("pos-1",)) == position_modification_trail(
        entries, ("pos-1",)
    )
    assert recent_pm_decisions_for_position(
        entries, "pos-1", 1
    ) == recent_pm_decisions_for_position(entries, "pos-1", 1)
    assert partition_by_event_group(entries) == partition_by_event_group(entries)
    assert chronological_sort(entries) == chronological_sort(entries)


# ---------------------------------------------------------------------------
# Distillation config-change helpers (story 14a contract)
# ---------------------------------------------------------------------------


def _build_distillation_config(**overrides: object) -> DistillationConfig:
    """Return a fully-populated :class:`DistillationConfig` for tests.

    Per-section overrides are applied via Pydantic's ``model_copy(update=...)``
    on the relevant section.
    """
    config = DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=2.0,
            price_move_atr_multiple=2.5,
            options_low_oi_volume_multiple=5.0,
            block_trade_min_shares=10_000,
            block_trade_min_notional_usd=1_000_000,
            dark_pool_one_sided_window_minutes=60,
            earnings_revision_cluster_count=3,
            earnings_revision_cluster_days=5,
            macro_surprise_percentile=90,
            funding_stress_component_alert_count=2,
            funding_stress_component_percentile=80,
            market_liquidity_alert_percentile=10,
            news_price_divergence_window_hours=12,
            news_price_divergence_min_articles=5,
        ),
        regime_classification=RegimeClassification(
            regime_low_vol_vix_max=15.0,
            regime_normal_vix_min=15.0,
            regime_normal_vix_max=20.0,
            regime_elevated_vix_min=20.0,
            regime_elevated_vix_max=28.0,
            regime_crisis_vix_min=28.0,
            regime_term_structure_backwardation_threshold=0.0,
            regime_vvix_high_percentile=80,
            regime_vvix_low_percentile=20,
        ),
        regime_transition=RegimeTransition(
            regime_transition_confirmed_invocations=3,
            regime_transition_indicator_agreement_min=3,
            regime_skip_emergency_trigger=True,
        ),
        lead_lag=LeadLag(
            pairs=(
                LeadLagPair(key="credit_to_equity", lead="HYG", lag="SPY"),
                LeadLagPair(key="semis_to_tech", lead="SOXX", lag="QQQ"),
                LeadLagPair(key="financials_to_market", lead="XLF", lag="SPY"),
                LeadLagPair(key="commodity_to_energy_equity", lead="USO", lag="XLE"),
            ),
            lead_lag_funding_to_credit_max_days=4,
            lead_lag_credit_to_equity_max_days=4,
            lead_lag_semis_to_tech_max_days=3,
            lead_lag_financials_to_market_max_days=4,
            lead_lag_commodity_to_energy_equity_max_days=4,
            lead_lag_overdue_lead_sigma=2.0,
        ),
        narrative_lag=NarrativeLag(
            narrative_lag_correlation_shift_sigma=2.0,
            correlation_breakdown_sigma=2.0,
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.05,
            narrative_lag_media_silence_hours=24,
        ),
        persistence_windows=PersistenceWindows(
            volume_baseline_days=20,
            atr_baseline_days=14,
            spread_baseline_days=20,
            correlation_short_days=20,
            correlation_long_days=60,
            sentiment_baseline_days=30,
            sentiment_min_observations=5,
            gap_fill_baseline_days=60,
            gap_fill_min_events=3,
            extended_hours_confirmation_days=30,
            extended_hours_min_events=3,
            prediction_market_history_days=30,
            funding_stress_baseline_days=60,
            market_liquidity_baseline_days=60,
        ),
        prediction_market=PredictionMarket(
            prediction_market_delta_pp_threshold=10.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        ),
    )
    return config.model_copy(update=overrides) if overrides else config


def _swap_section(
    config: DistillationConfig, section: str, **updates: object
) -> DistillationConfig:
    """Return a copy of ``config`` with one section updated via ``model_copy``."""
    section_obj = getattr(config, section).model_copy(update=updates)
    return config.model_copy(update={section: section_obj})


class TestComputeDistillationConfigHash:
    """``compute_distillation_config_hash`` is deterministic SHA-256 hex."""

    def test_returns_64_hex_chars(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_hash,
        )

        digest = compute_distillation_config_hash(_build_distillation_config())
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_byte_identical_inputs_produce_identical_digest(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_hash,
        )

        a = _build_distillation_config()
        b = _build_distillation_config()
        assert compute_distillation_config_hash(a) == compute_distillation_config_hash(b)

    def test_single_field_perturbation_changes_digest(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_hash,
        )

        a = _build_distillation_config()
        b = _swap_section(a, "anomaly_detection", volume_anomaly_sigma=3.0)
        assert compute_distillation_config_hash(a) != compute_distillation_config_hash(b)


class TestComputeDistillationConfigDiff:
    """``compute_distillation_config_diff`` walks the Pydantic tree per-key."""

    def test_identical_configs_produce_empty_tuple(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )

        a = _build_distillation_config()
        b = _build_distillation_config()
        assert compute_distillation_config_diff(a, b) == ()

    def test_single_scalar_change_produces_single_entry(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )

        a = _build_distillation_config()
        b = _swap_section(a, "anomaly_detection", volume_anomaly_sigma=3.5)
        diff = compute_distillation_config_diff(a, b)
        assert len(diff) == 1
        assert diff[0].key_path == "anomaly_detection.volume_anomaly_sigma"
        assert diff[0].old_value == 2.0
        assert diff[0].new_value == 3.5

    def test_nested_change_produces_dotted_key_path(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )

        a = _build_distillation_config()
        b = _swap_section(
            a,
            "regime_classification",
            regime_low_vol_vix_max=14.0,
            regime_normal_vix_min=14.0,
        )
        diff = compute_distillation_config_diff(a, b)
        paths = [c.key_path for c in diff]
        assert "regime_classification.regime_low_vol_vix_max" in paths
        assert "regime_classification.regime_normal_vix_min" in paths

    def test_multi_field_change_returns_sorted_entries(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )

        a = _build_distillation_config()
        b_anom = _swap_section(a, "anomaly_detection", volume_anomaly_sigma=3.5)
        # Apply a second change on a different section
        narrative_obj = b_anom.narrative_lag.model_copy(
            update={"narrative_lag_media_silence_hours": 36}
        )
        b = b_anom.model_copy(update={"narrative_lag": narrative_obj})

        diff = compute_distillation_config_diff(a, b)
        paths = [c.key_path for c in diff]
        assert paths == sorted(paths)
        assert "anomaly_detection.volume_anomaly_sigma" in paths
        assert "narrative_lag.narrative_lag_media_silence_hours" in paths

    def test_prior_none_returns_empty_tuple(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )

        new = _build_distillation_config()
        assert compute_distillation_config_diff(None, new) == ()

    def test_returns_tuple_of_distillation_config_change(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            compute_distillation_config_diff,
        )
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChange,
        )

        a = _build_distillation_config()
        b = _swap_section(a, "anomaly_detection", volume_anomaly_sigma=3.5)
        diff = compute_distillation_config_diff(a, b)
        assert isinstance(diff, tuple)
        assert all(isinstance(c, DistillationConfigChange) for c in diff)


_FIXED_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestBuildDistillationConfigChangeEntry:
    """``build_distillation_config_change_entry`` composes hash + diff into an entry."""

    def test_first_reload_returns_baseline_entry(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            build_distillation_config_change_entry,
            compute_distillation_config_hash,
        )
        from alphamind.portfolio_state.records.activity_log import (
            DistillationConfigChangeDetail,
            EventGroup,
            EventSource,
            EventType,
        )

        new = _build_distillation_config()
        entry = build_distillation_config_change_entry(
            prior=None,
            new=new,
            invocation_id="inv-001",
            timestamp=_FIXED_TS,
            git_sha="abc1234",
            entry_id="eid-001",
        )
        assert entry is not None
        assert entry.event_type == EventType.DISTILLATION_CONFIG_CHANGE
        assert entry.event_group == EventGroup.CONFIGURATION
        assert entry.source == EventSource.CONFIG_RELOAD
        assert entry.position_id is None
        assert entry.order_id is None
        assert entry.thesis_id is None
        assert entry.invocation_id == "inv-001"
        assert entry.entry_id == "eid-001"
        assert entry.timestamp == _FIXED_TS

        assert isinstance(entry.detail, DistillationConfigChangeDetail)
        assert entry.detail.prior_hash is None
        assert entry.detail.new_hash == compute_distillation_config_hash(new)
        assert entry.detail.changes == ()
        assert entry.detail.git_sha == "abc1234"
        assert entry.detail.config_file == "config/distillation.yaml"

    def test_no_change_suppressed(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            build_distillation_config_change_entry,
        )

        config = _build_distillation_config()
        result = build_distillation_config_change_entry(
            prior=config,
            new=config,
            invocation_id="inv-002",
            timestamp=_FIXED_TS,
            git_sha="abc1234",
            entry_id="eid-002",
        )
        assert result is None

    def test_no_change_with_distinct_but_equal_configs_suppressed(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            build_distillation_config_change_entry,
        )

        prior = _build_distillation_config()
        new = _build_distillation_config()
        assert prior is not new
        result = build_distillation_config_change_entry(
            prior=prior,
            new=new,
            invocation_id="inv-002",
            timestamp=_FIXED_TS,
            git_sha="abc1234",
            entry_id="eid-002",
        )
        assert result is None

    def test_change_returns_populated_entry(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            build_distillation_config_change_entry,
            compute_distillation_config_hash,
        )

        prior = _build_distillation_config()
        new = _swap_section(prior, "anomaly_detection", volume_anomaly_sigma=3.5)
        entry = build_distillation_config_change_entry(
            prior=prior,
            new=new,
            invocation_id="inv-003",
            timestamp=_FIXED_TS,
            git_sha="cafe5678",
            entry_id="eid-003",
        )
        assert entry is not None
        assert entry.detail.prior_hash == compute_distillation_config_hash(prior)
        assert entry.detail.new_hash == compute_distillation_config_hash(new)
        assert entry.detail.git_sha == "cafe5678"
        assert len(entry.detail.changes) == 1
        change = entry.detail.changes[0]
        assert change.key_path == "anomaly_detection.volume_anomaly_sigma"
        assert change.old_value == 2.0
        assert change.new_value == 3.5

    def test_timestamp_independent_of_wall_clock(self) -> None:
        from alphamind.portfolio_state.computations.activity_log import (
            build_distillation_config_change_entry,
        )

        prior = _build_distillation_config()
        new = _swap_section(prior, "anomaly_detection", volume_anomaly_sigma=3.5)
        custom_ts = datetime(1999, 12, 31, 23, 59, 59, tzinfo=UTC)
        entry = build_distillation_config_change_entry(
            prior=prior,
            new=new,
            invocation_id="inv-004",
            timestamp=custom_ts,
            git_sha="abc1234",
            entry_id="eid-004",
        )
        assert entry is not None
        assert entry.timestamp == custom_ts
