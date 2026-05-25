"""Backward-compat re-export tests (ALP-463).

The activity-log module split into per-event-group submodules but every
existing consumer imports from the legacy ``activity_log`` module path. The
shim module must keep working until consumers migrate to the per-group
modules (post-merge).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import price


class TestBackwardCompatReexport:
    """The legacy ``events.activity_log`` import surface re-exports every public symbol."""

    def test_event_type_enum_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import EventType

        assert EventType.POSITION_OPENED.value == "POSITION_OPENED"

    def test_event_group_enum_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import EventGroup

        assert EventGroup.POSITION_LIFECYCLE.value == "POSITION_LIFECYCLE"

    def test_position_opened_detail_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import PositionOpenedDetail

        assert PositionOpenedDetail.__name__ == "PositionOpenedDetail"

    def test_order_filled_detail_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import OrderFilledDetail

        assert OrderFilledDetail.__name__ == "OrderFilledDetail"

    def test_distillation_config_change_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import DistillationConfigChange

        assert DistillationConfigChange.__name__ == "DistillationConfigChange"

    def test_dispatch_dict_importable(self) -> None:
        from alphamind.portfolio_state.events.activity_log import (
            EVENT_TYPE_TO_DETAIL_CLASS,
            EVENT_TYPE_TO_GROUP,
            EventType,
            PositionOpenedDetail,
        )

        assert EVENT_TYPE_TO_DETAIL_CLASS[EventType.POSITION_OPENED] is PositionOpenedDetail
        assert EVENT_TYPE_TO_GROUP[EventType.POSITION_OPENED].value == "POSITION_LIFECYCLE"

    def test_activity_log_entry_importable_and_constructible(self) -> None:

        from alphamind.portfolio_state.events.activity_log import (
            ActivityLogEntry,
            EventGroup,
            EventSource,
            EventType,
            PositionOpenedDetail,
            PositionOpenMechanism,
        )

        detail = PositionOpenedDetail(
            ticker=Symbol("AAPL"),
            direction="LONG",
            fill_price=price("150.0"),
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        )
        entry = ActivityLogEntry(
            entry_id="eid-001",
            invocation_id="inv-001",
            timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
            event_type=EventType.POSITION_OPENED,
            event_group=EventGroup.POSITION_LIFECYCLE,
            position_id=None,
            order_id=None,
            thesis_id=None,
            source=EventSource.FILL_PROCESSOR,
            detail=detail,
        )
        assert entry.event_type is EventType.POSITION_OPENED


class TestDispatchDictDerivation:
    """The dispatch dicts derive from per-submodule registries (no hand-maintained dicts)."""

    def test_event_type_to_detail_class_position_opened(self) -> None:
        from alphamind.portfolio_state.events import (
            EVENT_TYPE_TO_DETAIL_CLASS,
            EventType,
            PositionOpenedDetail,
        )

        assert EVENT_TYPE_TO_DETAIL_CLASS[EventType.POSITION_OPENED] is PositionOpenedDetail

    def test_event_type_to_group_position_opened(self) -> None:
        from alphamind.portfolio_state.events import (
            EVENT_TYPE_TO_GROUP,
            EventGroup,
            EventType,
        )

        assert EVENT_TYPE_TO_GROUP[EventType.POSITION_OPENED] is EventGroup.POSITION_LIFECYCLE
