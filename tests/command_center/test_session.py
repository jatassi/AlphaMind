"""Tests for ``command_center.session`` (story 02 / ALP-666).

Covers the frozen :class:`ProcessSession` Pydantic model + the
:func:`new_session` builder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from alphamind.command_center.session import ProcessSession, new_session


class TestProcessSession:
    def test_builds_with_required_fields(self) -> None:
        session = ProcessSession(
            process_lifetime_id="plt-x",
            started_at=datetime.now(UTC),
        )
        assert session.process_lifetime_id == "plt-x"

    def test_is_frozen(self) -> None:
        session = ProcessSession(
            process_lifetime_id="plt-x",
            started_at=datetime.now(UTC),
        )
        # Pydantic v2 frozen models raise ValidationError on attribute set.
        with pytest.raises(ValidationError):
            session.process_lifetime_id = "plt-y"

    def test_rejects_unknown_fields(self) -> None:
        # F14: extra='forbid' catches a caller's typo
        # (``process_lifetimes_id`` rather than ``process_lifetime_id``)
        # at construction rather than silently dropping it.
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProcessSession(
                process_lifetime_id="plt-x",
                started_at=datetime.now(UTC),
                unexpected_field="oops",  # type: ignore[call-arg]
            )


class TestNewSession:
    def test_stamps_current_utc_time(self) -> None:
        before = datetime.now(UTC)
        session = new_session(process_lifetime_id="plt-x")
        after = datetime.now(UTC)
        assert before <= session.started_at <= after
        assert session.started_at.tzinfo is UTC
