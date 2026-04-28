"""Tests for activity-log filtering pure functions (story 05e)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
        ticker="AAPL",
        direction="LONG",
        fill_price=150.0,
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

    e1 = _pm_entry(entry_id="e1", position_id="pos-A")
    e2 = _pos_entry(entry_id="e2", position_id="pos-B")
    e3 = _pm_entry(entry_id="e3", position_id="pos-A")
    entries = (e1, e2, e3)

    result = filter_by_position_id(entries, "pos-A")

    assert result == (e1, e3)


def test_filter_by_position_id_excludes_none() -> None:
    from alphamind.portfolio_state.computations.activity_log import filter_by_position_id

    e_with = _pm_entry(entry_id="e1", position_id="pos-A")
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

    e1 = _pos_entry(entry_id="e1", order_id="ord-1")
    e2 = _pos_entry(entry_id="e2", order_id="ord-2")
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

    e1 = _pos_entry(entry_id="e1", thesis_id="thesis-X")
    e2 = _pos_entry(entry_id="e2", thesis_id="thesis-Y")
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

    e1 = _pm_entry(entry_id="e1", position_id="pos-A")
    e2 = _pos_entry(entry_id="e2", position_id="pos-B")
    e3 = _pm_entry(entry_id="e3", position_id="pos-A")
    entries = (e1, e2, e3)

    result = position_modification_trail(entries, ("pos-A", "pos-B"))

    assert result["pos-A"] == (e1, e3)
    assert result["pos-B"] == (e2,)


def test_position_modification_trail_unknown_id_absent() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    e1 = _pm_entry(position_id="pos-A")
    entries = (e1,)

    result = position_modification_trail(entries, ("pos-A", "pos-UNKNOWN"))

    assert "pos-A" in result
    assert "pos-UNKNOWN" not in result


def test_position_modification_trail_empty_position_ids() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        position_modification_trail,
    )

    e1 = _pm_entry(position_id="pos-A")
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
    e1 = _pm_entry(entry_id="e1", position_id="pos-A", timestamp=base)
    e2 = _pm_entry(entry_id="e2", position_id="pos-A", timestamp=base + timedelta(hours=1))
    e3 = _pm_entry(entry_id="e3", position_id="pos-A", timestamp=base + timedelta(hours=2))
    e4 = _pos_entry(entry_id="e4", position_id="pos-A", timestamp=base + timedelta(hours=3))
    entries = (e1, e2, e3, e4)

    result = recent_pm_decisions_for_position(entries, "pos-A", limit=2)

    # Should be last 2 PM_DECISION entries for pos-A: e2, e3
    assert result == (e2, e3)


def test_recent_pm_decisions_for_position_limit_exceeds_matches() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    e1 = _pm_entry(entry_id="e1", position_id="pos-A")
    entries = (e1,)

    result = recent_pm_decisions_for_position(entries, "pos-A", limit=10)

    assert result == (e1,)


def test_recent_pm_decisions_for_position_no_matches() -> None:
    from alphamind.portfolio_state.computations.activity_log import (
        recent_pm_decisions_for_position,
    )

    e1 = _pos_entry(entry_id="e1", position_id="pos-A")
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
