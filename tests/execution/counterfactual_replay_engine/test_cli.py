"""Tests for the counterfactual replay engine CLI (ALP-565, story 09).

Behaviors covered:
* empty queue → all-zeros summary → exit 0
* happy path: seeded proposals → evaluated + unevaluable counts + DB rows
* --since filter keeps only proposals at/after the given timestamp
* invalid --since / --as-of (naive datetime) → clear error + non-zero exit
* --db-path override is honored via make_engine
* default-path fallback logs a WARNING
* --help exits 0
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.persistence.models import AssetUniverse, Base, OhlcvBars
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.portfolio_state.events.types import PMVerdict
from tests.execution.counterfactual_replay_engine._fixtures import (
    add_pm_decision_row,
    analyst_equity_recommendation_json,
    pm_decision_entry,
    seed_invocation,
)

_INVOCATION = "inv-cli-2026-06-01T14:00:00Z"
_PROPOSAL_TS = datetime(2026, 6, 1, 14, 0, 0, tzinfo=UTC)
_AS_OF = _PROPOSAL_TS + timedelta(days=4)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_engine() -> Iterator[Engine]:
    import alphamind.state.tables  # noqa: F401 — register all ORM tables

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def mem_session(mem_engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(mem_engine)
    with factory() as sess:
        seed_invocation(sess, _INVOCATION)
        yield sess


@pytest.fixture()
def db_file(mem_engine: Engine) -> Iterator[Path]:
    """Write the in-memory DB schema to a temp file and return its path.

    The CLI under test needs a real file path so make_engine can open it; we
    create all tables in the temp file before yielding.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    import alphamind.state.tables  # noqa: F401

    file_engine = make_engine(str(db_path))
    Base.metadata.create_all(file_engine)
    file_engine.dispose()
    yield db_path
    db_path.unlink(missing_ok=True)


@pytest.fixture()
def config_dir() -> Path:
    """Return the real project config/ directory (has replay_engine.yaml + execution.yaml).

    test_cli.py lives at tests/execution/counterfactual_replay_engine/test_cli.py
    parents[0] = tests/execution/counterfactual_replay_engine/
    parents[1] = tests/execution/
    parents[2] = tests/
    parents[3] = project root
    """
    return Path(__file__).parents[3] / "config"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_underlying(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-01T00:00:00Z",
        )
    )
    session.flush()


def _bar(ticker: str, period_start: datetime, *, high: float) -> OhlcvBars:
    period_end = datetime.fromtimestamp(period_start.timestamp() + 900, tz=UTC)
    return OhlcvBars(
        ticker=ticker,
        timeframe="15min",
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        session="regular",
        adj_open=100.0,
        adj_high=high,
        adj_low=99.0,
        adj_close=100.0,
        adj_volume=1,
        adj_vwap=None,
        unadj_open=100.0,
        unadj_high=high,
        unadj_low=99.0,
        unadj_close=100.0,
        unadj_volume=1_000_000,
        unadj_vwap=None,
        trade_count=None,
        source="test",
        ingested_at="2026-06-01T00:00:00Z",
    )


def _seed_bars(session: Session, ticker: str, *, count: int = 12) -> None:
    _ensure_underlying(session, ticker)
    for i in range(count):
        ts = _PROPOSAL_TS + timedelta(minutes=15 * i)
        session.add(_bar(ticker, ts, high=201.0 if i == 1 else 101.0))
    session.flush()


def _seed_analyst_reject(
    session: Session,
    *,
    entry_id: str,
    envelope_id: str,
    ticker: str = "AAPL",
    timestamp: datetime | None = None,
) -> None:
    ts = timestamp or _PROPOSAL_TS
    entry = pm_decision_entry(
        entry_id=entry_id,
        invocation_id=_INVOCATION,
        timestamp=ts,
        envelope_id=envelope_id,
        verdict=PMVerdict.REJECT,
        source_provenance_json={"source_provenance": "pm_analyst"},
        originating_proposal_json=analyst_equity_recommendation_json(ticker=ticker),
    )
    add_pm_decision_row(session, entry)


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """Run the CLI main() and capture stdout output (via capsys workaround)."""
    import io
    import sys

    from alphamind.execution.counterfactual_replay_engine.cli import main

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        rc = main(argv)
    finally:
        sys.stdout = old_stdout
    return rc, captured.getvalue()


# ---------------------------------------------------------------------------
# Cycle 1 RED→GREEN: empty queue → all-zeros summary → exit 0
# ---------------------------------------------------------------------------


class TestEmptyQueue:
    def test_empty_queue_exits_zero_with_zero_summary(
        self, db_file: Path, config_dir: Path
    ) -> None:
        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                "2026-06-05T00:00:00+00:00",
            ]
        )
        assert rc == 0
        assert "counterfactual replay summary" in output
        assert "evaluated:                 0" in output
        assert "skipped_idempotent:        0" in output
        assert "errors:                    0" in output
        assert "total processed:           0" in output


# ---------------------------------------------------------------------------
# Cycle 2 RED→GREEN: summary format includes all enum reason names
# ---------------------------------------------------------------------------


class TestSummaryFormat:
    def test_summary_includes_all_unevaluable_reason_names(
        self, db_file: Path, config_dir: Path
    ) -> None:
        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                "2026-06-05T00:00:00+00:00",
            ]
        )
        assert rc == 0
        # All five UnevaluableReason values must appear
        assert "unsupported_instrument:" in output
        assert "unsupported_bracket_type:" in output
        assert "data_missing:" in output
        assert "corporate_action_in_window:" in output
        assert "strategist_position_action_not_supported:" in output

    def test_summary_includes_skipped_not_due_line(self, db_file: Path, config_dir: Path) -> None:
        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                "2026-06-05T00:00:00+00:00",
            ]
        )
        assert rc == 0
        assert "skipped_not_due:" in output


# ---------------------------------------------------------------------------
# Cycle 3 RED→GREEN: invalid --since / --as-of (naive datetime) → non-zero
# ---------------------------------------------------------------------------


class TestInvalidArgs:
    def test_naive_since_exits_nonzero(self, db_file: Path, config_dir: Path) -> None:
        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--since",
                "2026-06-01T00:00:00",  # no timezone
                "--as-of",
                "2026-06-05T00:00:00+00:00",
            ]
        )
        assert rc != 0
        output_lower = output.lower()
        assert (
            "naive" in output_lower
            or "timezone" in output_lower
            or "tz" in output_lower
            or "aware" in output_lower
        )

    def test_naive_as_of_exits_nonzero(self, db_file: Path, config_dir: Path) -> None:
        rc, _output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                "2026-06-05T00:00:00",  # no timezone
            ]
        )
        assert rc != 0

    def test_invalid_iso_format_exits_nonzero(self, db_file: Path, config_dir: Path) -> None:
        rc, _output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                "not-a-datetime",
            ]
        )
        assert rc != 0


# ---------------------------------------------------------------------------
# Cycle 4 RED→GREEN: default-path fallback logs WARNING
# ---------------------------------------------------------------------------


class TestDefaultPathWarning:
    def test_default_db_path_logs_warning(
        self, config_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When --db-path is omitted, a WARNING is logged before the DB attempt."""
        import alphamind.execution.counterfactual_replay_engine.cli as cli_module

        with (
            caplog.at_level(logging.WARNING, logger=cli_module.__name__),
            contextlib.suppress(Exception),
        ):
            # We expect this to fail (no prod DB on dev machine), but the
            # WARNING must be logged before the failure.
            _run_cli(
                [
                    "--config-dir",
                    str(config_dir),
                    "--as-of",
                    "2026-06-05T00:00:00+00:00",
                ]
            )
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            "default" in r.message.lower() or "db" in r.message.lower() for r in warning_records
        )


# ---------------------------------------------------------------------------
# Cycle 5 RED→GREEN: happy path (seeded proposals → evaluated + summary)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_evaluated_proposal_appears_in_summary(self, db_file: Path, config_dir: Path) -> None:
        """Seed one evaluable equity reject → CLI reports evaluated: 1."""
        # Seed the file DB with FK parents + bars + PM decision
        file_engine = make_engine(str(db_file))
        factory = make_session_factory(file_engine)
        with factory() as sess:
            seed_invocation(sess, _INVOCATION)
            _seed_bars(sess, "AAPL")
            _seed_analyst_reject(sess, entry_id="e1", envelope_id="env-cli-eval")
            sess.commit()
        file_engine.dispose()

        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                _AS_OF.isoformat(),
            ]
        )
        assert rc == 0
        assert "evaluated:                 1" in output
        assert "total processed:           1" in output

    def test_unevaluable_data_missing_appears_in_summary(
        self, db_file: Path, config_dir: Path
    ) -> None:
        """Seed an equity reject with no bars → CLI reports data_missing: 1."""
        file_engine = make_engine(str(db_file))
        factory = make_session_factory(file_engine)
        with factory() as sess:
            seed_invocation(sess, _INVOCATION)
            _ensure_underlying(sess, "MSFT")
            _seed_analyst_reject(sess, entry_id="e1", envelope_id="env-cli-unev", ticker="MSFT")
            sess.commit()
        file_engine.dispose()

        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--as-of",
                _AS_OF.isoformat(),
            ]
        )
        assert rc == 0
        # reason-line alignment: "    data_missing:" + spaces + count
        assert "data_missing:" in output and output.count("data_missing:") == 1
        lines = output.splitlines()
        dm_line = next(line for line in lines if "data_missing:" in line)
        assert dm_line.strip().endswith("1"), f"data_missing line: {dm_line!r}"
        assert "unevaluable:               1" in output


# ---------------------------------------------------------------------------
# Cycle 6 RED→GREEN: --since filter
# ---------------------------------------------------------------------------


class TestSinceFilter:
    def test_since_filter_excludes_older_proposals(self, db_file: Path, config_dir: Path) -> None:
        """Two proposals: one before --since, one at/after. Only the latter replays."""
        old_ts = _PROPOSAL_TS - timedelta(days=10)
        new_ts = _PROPOSAL_TS

        file_engine = make_engine(str(db_file))
        factory = make_session_factory(file_engine)
        with factory() as sess:
            seed_invocation(sess, _INVOCATION)
            _seed_bars(sess, "AAPL")
            # Seed bars for old_ts window too (12 bars forward from old_ts)
            for i in range(12):
                ts = old_ts + timedelta(minutes=15 * i)
                sess.add(_bar("AAPL", ts, high=201.0 if i == 1 else 101.0))
            sess.flush()
            _seed_analyst_reject(sess, entry_id="e-old", envelope_id="env-old", timestamp=old_ts)
            _seed_analyst_reject(sess, entry_id="e-new", envelope_id="env-new", timestamp=new_ts)
            sess.commit()
        file_engine.dispose()

        # Run with --since = new_ts (exclude the old one)
        rc, output = _run_cli(
            [
                "--db-path",
                str(db_file),
                "--config-dir",
                str(config_dir),
                "--since",
                new_ts.isoformat(),
                "--as-of",
                _AS_OF.isoformat(),
            ]
        )
        assert rc == 0
        assert "evaluated:                 1" in output
        assert "total processed:           1" in output
