"""Tests for ``MonitorSession`` typed record + ``new_session`` factory (story 01)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.execution.continuous_monitor.session import MonitorSession, new_session

_SESSION_ID_RE = re.compile(r"^mon-\d{8}T\d{6}Z-[0-9a-f]{8}$")


class TestMonitorSession:
    def test_frozen_record(self) -> None:
        session = new_session(mode="paper")
        with pytest.raises(ValidationError):
            session.mode = "live"

    def test_new_session_session_id_matches_required_pattern(self) -> None:
        session = new_session(mode="paper")
        assert _SESSION_ID_RE.match(session.session_id), (
            f"session_id {session.session_id!r} does not match required pattern"
        )

    def test_new_session_stamps_tz_aware_utc(self) -> None:
        before = datetime.now(UTC)
        session = new_session(mode="paper")
        after = datetime.now(UTC)
        assert session.started_at.tzinfo is not None
        assert session.started_at.utcoffset() == before.utcoffset()
        assert before <= session.started_at <= after

    def test_new_session_round_trips_mode(self) -> None:
        live_session = new_session(mode="live")
        paper_session = new_session(mode="paper")
        assert live_session.mode == "live"
        assert paper_session.mode == "paper"

    def test_two_new_sessions_produce_distinct_ids(self) -> None:
        s1 = new_session(mode="paper")
        s2 = new_session(mode="paper")
        assert s1.session_id != s2.session_id

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValidationError):
            MonitorSession(
                session_id="mon-20260511T120000Z-deadbeef",
                started_at=datetime.now(UTC),
                mode="oops",  # type: ignore[arg-type]
            )

    def test_session_id_timestamp_matches_started_at(self) -> None:
        """The ID's embedded timestamp tracks ``started_at`` so the row is greppable."""
        session = new_session(mode="paper")
        # ID format: mon-YYYYMMDDTHHMMSSZ-XXXXXXXX
        ts_part = session.session_id.split("-")[1]  # "YYYYMMDDTHHMMSSZ"
        assert ts_part == session.started_at.strftime("%Y%m%dT%H%M%SZ")
