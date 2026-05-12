"""Tests for ``GREEKS_REFRESH_FAILED`` activity-log event (story 03a / ALP-436).

Additive enum / detail-payload registration: the existing event-type catalog
must continue to pass; the new entry must be a first-class citizen of
``EVENT_TYPE_TO_GROUP``, ``EVENT_TYPE_TO_DETAIL_CLASS``, ``AnyDetailType``,
and ``ActivityLogEntry``.

The event source for monitor-driven activity-log entries is
``EventSource.GUARDRAIL_LAYER`` — the same value used today by
``EMERGENCY_INVOCATION_REQUESTED`` from the monitor's emergency-trigger
path. Adding a monitor-specific event-source value is the operator's call
(parent issue ALP-123 § Surfacing condition (iii)); story 03a reuses
``GUARDRAIL_LAYER`` to stay within the established vocabulary.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    GreeksRefreshFailedDetail,
)


class TestGreeksRefreshFailedDetail:
    def test_constructs_with_required_fields(self) -> None:
        detail = GreeksRefreshFailedDetail(
            underlying_ticker="AAPL",
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_timeout",
            prior_as_of=datetime(2026, 5, 11, 14, 15, tzinfo=UTC),
        )
        assert detail.underlying_ticker == "AAPL"
        assert detail.occ_symbol == "O:AAPL260619C00200000"
        assert detail.failure_reason == "iv_fetch_timeout"
        assert detail.prior_as_of == datetime(2026, 5, 11, 14, 15, tzinfo=UTC)

    def test_prior_as_of_can_be_none(self) -> None:
        """A position whose greeks have never been successfully refreshed (no
        prior ``as_of_timestamp``) emits the failure with ``prior_as_of=None``."""
        detail = GreeksRefreshFailedDetail(
            underlying_ticker="AAPL",
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_404",
            prior_as_of=None,
        )
        assert detail.prior_as_of is None

    def test_is_frozen(self) -> None:
        detail = GreeksRefreshFailedDetail(
            underlying_ticker="AAPL",
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_timeout",
            prior_as_of=None,
        )
        with pytest.raises(ValidationError):
            detail.failure_reason = "something_else"


class TestGreeksRefreshFailedCatalogRegistration:
    def test_event_type_is_risk_and_guardrail_group(self) -> None:
        assert EVENT_TYPE_TO_GROUP[EventType.GREEKS_REFRESH_FAILED] is EventGroup.RISK_AND_GUARDRAIL

    def test_event_type_maps_to_greeks_refresh_failed_detail(self) -> None:
        assert (
            EVENT_TYPE_TO_DETAIL_CLASS[EventType.GREEKS_REFRESH_FAILED] is GreeksRefreshFailedDetail
        )

    def test_event_type_value_matches_member_name(self) -> None:
        assert EventType.GREEKS_REFRESH_FAILED.value == "GREEKS_REFRESH_FAILED"


class TestActivityLogEntryAcceptsGreeksRefreshFailed:
    def test_entry_with_greeks_refresh_failed_detail_validates(self) -> None:
        entry = ActivityLogEntry(
            entry_id="ent-test-001",
            invocation_id="inv-test-001",
            timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            event_type=EventType.GREEKS_REFRESH_FAILED,
            event_group=EventGroup.RISK_AND_GUARDRAIL,
            position_id="pos-001",
            order_id=None,
            thesis_id=None,
            source=EventSource.GUARDRAIL_LAYER,
            detail=GreeksRefreshFailedDetail(
                underlying_ticker="AAPL",
                occ_symbol="O:AAPL260619C00200000",
                failure_reason="iv_fetch_timeout",
                prior_as_of=datetime(2026, 5, 11, 14, 15, tzinfo=UTC),
            ),
        )
        assert entry.event_type == EventType.GREEKS_REFRESH_FAILED
        assert entry.event_group == EventGroup.RISK_AND_GUARDRAIL

    def test_entry_with_wrong_group_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ActivityLogEntry(
                entry_id="ent-test-002",
                invocation_id="inv-test-002",
                timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
                event_type=EventType.GREEKS_REFRESH_FAILED,
                event_group=EventGroup.POSITION_LIFECYCLE,  # wrong group
                position_id="pos-001",
                order_id=None,
                thesis_id=None,
                source=EventSource.GUARDRAIL_LAYER,
                detail=GreeksRefreshFailedDetail(
                    underlying_ticker="AAPL",
                    occ_symbol="O:AAPL260619C00200000",
                    failure_reason="iv_fetch_timeout",
                    prior_as_of=None,
                ),
            )
