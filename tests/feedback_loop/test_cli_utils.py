"""Shared feedback-loop CLI helpers (ALP-915 / story 06i, finding B).

``_cli_utils`` is the one home for the tz-aware ISO-8601 parse, the engine→session→dispose
lifecycle (sync + async), and the JSON emit the three feedback-loop CLIs share.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

import alphamind.state.tables  # noqa: F401 — register all tables on Base.metadata
from alphamind.feedback_loop import _cli_utils
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cli_utils.db"
    engine = make_engine(str(path))
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


class TestParseAwareDatetime:
    def test_parses_aware_iso8601(self) -> None:
        parsed = _cli_utils.parse_aware_datetime("2026-01-01T00:00:00+00:00", "--start")
        assert parsed == datetime(2026, 1, 1, tzinfo=UTC)

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _cli_utils.parse_aware_datetime("2026-01-01T00:00:00", "--start")

    def test_rejects_unparseable(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            _cli_utils.parse_aware_datetime("not-a-date", "--start")

    def test_message_names_the_argument(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="--end"):
            _cli_utils.parse_aware_datetime("oops", "--end")


class TestOpenSyncSession:
    def test_yields_usable_session(self, db_path: Path) -> None:
        with _cli_utils.open_sync_session(str(db_path)) as session:
            value = session.execute(text("SELECT 1")).scalar_one()
        assert value == 1


class TestOpenAsyncSession:
    async def test_yields_usable_session(self, db_path: Path) -> None:
        async with _cli_utils.open_async_session(str(db_path)) as session:
            result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


class TestEmitJson:
    def test_prints_json_payload(self, capsys: pytest.CaptureFixture[str]) -> None:
        _cli_utils.emit_json({"validation_id": "v-1"})
        out = capsys.readouterr().out
        assert json.loads(out) == {"validation_id": "v-1"}
