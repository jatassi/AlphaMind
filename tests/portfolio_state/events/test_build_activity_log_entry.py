"""Pure ``build_activity_log_entry`` builder (ALP-923).

The builder is the functional core for activity-log entry construction: it
derives ``event_group`` from the ``EVENT_TYPE_TO_GROUP`` registry and defaults
``entry_id`` to the ``{invocation_id}-{event_type.value}-{uuid4.hex}`` token,
overridable for deterministic-idempotent and custom-token callers. It lives in
``portfolio_state/events/__init__.py`` and must stay sqlalchemy-free so the
distillation layer reaches it without a new import-linter carve-out.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import price
from alphamind.portfolio_state.events import (
    EVENT_TYPE_TO_GROUP,
    EventSource,
    EventType,
    PositionOpenedDetail,
    PositionOpenMechanism,
    build_activity_log_entry,
)

_T0 = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


def _detail() -> PositionOpenedDetail:
    return PositionOpenedDetail(
        ticker=Symbol("AAPL"),
        direction="long",
        fill_price=price("150.0"),
        quantity=10.0,
        thesis_id=None,
        bracket_id=None,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )


def test_derives_event_group_from_registry_and_default_entry_id() -> None:
    entry = build_activity_log_entry(
        invocation_id="inv-1",
        event_type=EventType.POSITION_OPENED,
        position_id="pos-1",
        order_id=None,
        thesis_id=None,
        timestamp=_T0,
        detail=_detail(),
        source=EventSource.FILL_PROCESSOR,
    )

    # Group is derived from the registry, never hand-passed.
    assert entry.event_group == EVENT_TYPE_TO_GROUP[EventType.POSITION_OPENED]
    # Default entry_id shape: {invocation_id}-{event_type.value}-{uuid4.hex}.
    prefix = f"inv-1-{EventType.POSITION_OPENED.value}-"
    assert entry.entry_id.startswith(prefix)
    suffix = entry.entry_id.removeprefix(prefix)
    assert len(suffix) == 32  # uuid4().hex
    assert all(c in "0123456789abcdef" for c in suffix)
    # The remaining fields are threaded straight through.
    assert entry.invocation_id == "inv-1"
    assert entry.event_type is EventType.POSITION_OPENED
    assert entry.position_id == "pos-1"
    assert entry.order_id is None
    assert entry.thesis_id is None
    assert entry.timestamp == _T0
    assert entry.source is EventSource.FILL_PROCESSOR


def test_explicit_entry_id_overrides_the_default() -> None:
    """A caller-supplied ``entry_id`` is used verbatim (deterministic-id path)."""
    entry = build_activity_log_entry(
        invocation_id="inv-1",
        event_type=EventType.POSITION_OPENED,
        position_id="pos-1",
        order_id=None,
        thesis_id=None,
        timestamp=_T0,
        detail=_detail(),
        source=EventSource.FILL_PROCESSOR,
        entry_id="custom-deterministic-id",
    )

    assert entry.entry_id == "custom-deterministic-id"
