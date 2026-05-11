"""Tests for ``PipelineSession`` typed record + ``new_session`` factory (story 01)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.scheduler.session import PipelineSession, new_session


class TestPipelineSession:
    def test_frozen_record(self) -> None:
        session = new_session(process_lifetime_id="plt-pipeline-x", mode="paper")
        with pytest.raises(ValidationError):
            session.mode = "live"

    def test_new_session_stamps_tz_aware_utc(self) -> None:
        before = datetime.now(UTC)
        session = new_session(process_lifetime_id="plt-pipeline-x", mode="paper")
        after = datetime.now(UTC)

        assert session.started_at.tzinfo is not None
        assert session.started_at.utcoffset() == before.utcoffset()
        assert before <= session.started_at <= after

    def test_new_session_round_trips_process_lifetime_id_and_mode(self) -> None:
        session = new_session(process_lifetime_id="plt-pipeline-abc", mode="live")
        assert session.process_lifetime_id == "plt-pipeline-abc"
        assert session.mode == "live"

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValidationError):
            PipelineSession(
                process_lifetime_id="plt-pipeline-x",
                started_at=datetime.now(UTC),
                mode="oops",  # type: ignore[arg-type]
            )
