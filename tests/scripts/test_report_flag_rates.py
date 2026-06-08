"""Tests for ``scripts/report_flag_rates.py`` (story 16 / ALP-96).

The report reads every ``DISTILLATION_ANOMALY_FLAG`` activity-log entry over a
trailing window, groups by ``(threshold_class, threshold_key)``, and reports the
empirical firing rate, calibration-state split, universe coverage, and a
universe-wide-silence signal the operator feeds into the Class A review
procedure (``threshold-calibration.md`` section Update process, steps 2-3).

Coverage:
- Empty-window run lists every class and known key with zero counts and
  universe-wide-silence markers (invocation_count > 0).
- A populated window reports flag counts and the calibration-state split.
- Rate basis: ticker-bearing keys per-ticker-per-day; market-wide keys
  (all ``detail.ticker`` ``None``) per-invocation; zero denominators → 0.0.
- Config-gated classes display the resolved config value; structural classes
  display the literal ``structural (no config knob)``.
- ``--threshold-class`` emits one class; ``--ticker`` restricts ticker-bearing
  counts and leaves market-wide keys unchanged.
- JSON output mirrors the text structure (class → key → metrics record).
- ``main()`` returns 0 regardless of silence findings.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alphamind._kernel.calibration import CalibrationState
from alphamind.distillation.flag_event_types import (
    all_threshold_classes,
    flag_keys_for_class,
    resolve_flag_taxonomy,
)
from alphamind.persistence.models import Base
from alphamind.portfolio_state.events.activity_log import (
    DistillationAnomalyFlagDetail,
    EventSource,
    EventType,
    build_activity_log_entry,
)
from alphamind.scripts._common import load_distillation_config, load_universe_scope
from alphamind.scripts.report_flag_rates import (
    FlagRateReport,
    ThresholdKeyMetrics,
    compute_flag_rate_report,
    format_report_json,
    format_report_text,
    main,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.invocation_context.records import invocation_record_to_row
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_PROC_ID = "proc-1"
_WINDOW_DAYS = 28


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with state-persistence tables registered."""
    import alphamind.state.tables  # noqa: F401 — registers rows on Base.

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        sess.add(_make_process_lifetime_row())
        sess.commit()
        yield sess


@pytest.fixture()
def config() -> Any:
    return load_distillation_config()


@pytest.fixture()
def universe_size() -> int:
    return len(load_universe_scope())


def _make_process_lifetime_row() -> ProcessLifetimeRow:
    return ProcessLifetimeRow(
        process_lifetime_id=_PROC_ID,
        process_role="pipeline",
        process_start_at="2026-04-01T00:00:00Z",
        process_pid=12345,
        hostname="host",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=0,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux",
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _add_invocation(
    session: Session,
    *,
    invocation_id: str,
    start_at: datetime,
) -> None:
    from alphamind.state.invocation_context.records import InvocationRecord

    record = InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROC_ID,
        start_at=_iso(start_at),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="continuous_monitor",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/{invocation_id}/config.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=f"/tmp/{invocation_id}/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    session.add(invocation_record_to_row(record))


def _add_anomaly_flag(
    session: Session,
    *,
    invocation_id: str,
    timestamp: datetime,
    entry_id: str,
    threshold_class: str,
    threshold_key: str,
    ticker: str | None,
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
) -> None:
    detail = DistillationAnomalyFlagDetail(
        threshold_class=threshold_class,
        threshold_key=threshold_key,
        magnitude=3.0,
        severity="investigate_if_persists",
        ticker=ticker,
        calibration_state=calibration_state,
        block_id="blk-1",
    )
    entry = build_activity_log_entry(
        invocation_id=invocation_id,
        event_type=EventType.DISTILLATION_ANOMALY_FLAG,
        position_id=None,
        order_id=None,
        thesis_id=None,
        timestamp=timestamp,
        detail=detail,
        source=EventSource.DISTILLATION_ORCHESTRATOR,
        entry_id=entry_id,
    )
    session.add(activity_log_entry_to_row(entry))


def _seed_invocations(session: Session, count: int) -> list[str]:
    ids = [f"inv-{i}" for i in range(count)]
    for i, invocation_id in enumerate(ids):
        _add_invocation(session, invocation_id=invocation_id, start_at=_NOW - timedelta(days=i + 1))
    session.commit()
    return ids


def _find_key(report: FlagRateReport, cls: str, key: str) -> ThresholdKeyMetrics:
    klass = next(c for c in report.classes if c.threshold_class == cls)
    return next(k for k in klass.keys if k.threshold_key == key)


def _expected_keys(cls: str) -> set[str]:
    return {resolve_flag_taxonomy(prefix).threshold_key for prefix in flag_keys_for_class(cls)}


# ---------------------------------------------------------------------------
# Empty window — tracer bullet
# ---------------------------------------------------------------------------


class TestEmptyWindow:
    def test_lists_every_class_and_known_key_with_silence(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 3)

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        assert isinstance(report, FlagRateReport)
        assert report.invocation_count == 3
        assert {c.threshold_class for c in report.classes} == set(all_threshold_classes())
        for klass in report.classes:
            assert {k.threshold_key for k in klass.keys} == _expected_keys(klass.threshold_class)
            for key in klass.keys:
                assert key.flag_count == 0
                assert key.rate == 0.0
                assert key.silence is True

    def test_zero_invocations_are_not_silence(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        assert report.invocation_count == 0
        for klass in report.classes:
            for key in klass.keys:
                assert key.silence is False


# ---------------------------------------------------------------------------
# Populated window — counts, coverage, calibration split, silence
# ---------------------------------------------------------------------------


class TestPopulatedWindow:
    def test_flag_count_coverage_and_silence(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 3)
        for i, ticker in enumerate(("AAPL", "MSFT", "AAPL")):
            _add_anomaly_flag(
                session,
                invocation_id="inv-0",
                timestamp=_NOW - timedelta(days=1, seconds=i),
                entry_id=f"vol-{i}",
                threshold_class="anomaly_detection",
                threshold_key="volume_anomaly_sigma",
                ticker=ticker,
            )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        volume = _find_key(report, "anomaly_detection", "volume_anomaly_sigma")
        assert volume.flag_count == 3
        assert volume.coverage == 2  # AAPL, MSFT
        assert volume.is_market_wide is False
        assert volume.silence is False

        # A sibling key with no flags stays silent.
        price = _find_key(report, "anomaly_detection", "price_move_atr_multiple")
        assert price.flag_count == 0
        assert price.silence is True

    def test_calibration_split_counts_each_state(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 2)
        states = (
            CalibrationState.CALIBRATED,
            CalibrationState.CALIBRATED,
            CalibrationState.ACCUMULATING,
            CalibrationState.UNAVAILABLE,
        )
        for i, state in enumerate(states):
            _add_anomaly_flag(
                session,
                invocation_id="inv-0",
                timestamp=_NOW - timedelta(days=1, seconds=i),
                entry_id=f"vol-{i}",
                threshold_class="anomaly_detection",
                threshold_key="volume_anomaly_sigma",
                ticker="AAPL",
                calibration_state=state,
            )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        split = _find_key(report, "anomaly_detection", "volume_anomaly_sigma").calibration_split
        assert (split.calibrated, split.accumulating, split.unavailable) == (2, 1, 1)


# ---------------------------------------------------------------------------
# Rate basis — per ticker/day vs per invocation
# ---------------------------------------------------------------------------


class TestRateBasis:
    def test_ticker_bearing_rate_is_per_ticker_per_day(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 3)
        for i in range(4):
            _add_anomaly_flag(
                session,
                invocation_id="inv-0",
                timestamp=_NOW - timedelta(days=1, seconds=i),
                entry_id=f"vol-{i}",
                threshold_class="anomaly_detection",
                threshold_key="volume_anomaly_sigma",
                ticker=f"T{i}",
            )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        volume = _find_key(report, "anomaly_detection", "volume_anomaly_sigma")
        assert volume.is_market_wide is False
        assert volume.rate == pytest.approx(4 / (universe_size * _WINDOW_DAYS))

    def test_market_wide_rate_is_per_invocation(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 4)
        for i in range(2):
            _add_anomaly_flag(
                session,
                invocation_id="inv-0",
                timestamp=_NOW - timedelta(days=1, seconds=i),
                entry_id=f"macro-{i}",
                threshold_class="anomaly_detection",
                threshold_key="macro_surprise_percentile",
                ticker=None,
            )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        macro = _find_key(report, "anomaly_detection", "macro_surprise_percentile")
        assert macro.is_market_wide is True
        assert macro.coverage is None
        assert macro.rate == pytest.approx(2 / 4)


# ---------------------------------------------------------------------------
# Config-gated vs structural
# ---------------------------------------------------------------------------


class TestConfiguredValue:
    def test_config_gated_key_shows_resolved_config_value(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 1)
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        volume = _find_key(report, "anomaly_detection", "volume_anomaly_sigma")
        assert volume.configured_value == config.anomaly_detection.volume_anomaly_sigma

    def test_structural_key_has_no_config_value(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 1)
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )

        structural = _find_key(report, "options_flow", "pair_trade_signature")
        assert structural.configured_value is None

        text = format_report_text(report)
        assert "structural (no config knob)" in text


# ---------------------------------------------------------------------------
# Filters — --threshold-class and --ticker
# ---------------------------------------------------------------------------


class TestFilters:
    def test_threshold_class_filter_emits_one_section(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 1)
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
            threshold_class="anomaly_detection",
        )

        assert [c.threshold_class for c in report.classes] == ["anomaly_detection"]
        text = format_report_text(report)
        assert "=== ANOMALY_DETECTION" in text
        assert "=== OPTIONS_FLOW" not in text

    def test_ticker_filter_restricts_ticker_bearing_leaves_market_wide(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 3)
        for i, ticker in enumerate(("AAPL", "MSFT", "GOOG")):
            _add_anomaly_flag(
                session,
                invocation_id="inv-0",
                timestamp=_NOW - timedelta(days=1, seconds=i),
                entry_id=f"vol-{i}",
                threshold_class="anomaly_detection",
                threshold_key="volume_anomaly_sigma",
                ticker=ticker,
            )
        for i in range(2):
            _add_anomaly_flag(
                session,
                invocation_id="inv-1",
                timestamp=_NOW - timedelta(days=2, seconds=i),
                entry_id=f"macro-{i}",
                threshold_class="anomaly_detection",
                threshold_key="macro_surprise_percentile",
                ticker=None,
            )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
            ticker="AAPL",
        )

        assert report.ticker_filter == "AAPL"
        volume = _find_key(report, "anomaly_detection", "volume_anomaly_sigma")
        assert volume.flag_count == 1  # only AAPL
        assert volume.coverage == 1
        # Market-wide key is untouched by the ticker filter.
        macro = _find_key(report, "anomaly_detection", "macro_surprise_percentile")
        assert macro.is_market_wide is True
        assert macro.flag_count == 2


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_mirrors_class_key_metrics_structure(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        _seed_invocations(session, 2)
        _add_anomaly_flag(
            session,
            invocation_id="inv-0",
            timestamp=_NOW - timedelta(days=1),
            entry_id="vol-0",
            threshold_class="anomaly_detection",
            threshold_key="volume_anomaly_sigma",
            ticker="AAPL",
        )
        session.commit()

        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )
        payload = json.loads(format_report_json(report))

        assert set(payload) == set(all_threshold_classes())
        record = payload["anomaly_detection"]["volume_anomaly_sigma"]
        assert set(record) == {
            "flag_count",
            "rate",
            "calibration_split",
            "coverage",
            "configured_value",
            "silence",
        }
        assert record["flag_count"] == 1
        assert record["coverage"] == 1
        assert set(record["calibration_split"]) == {"calibrated", "accumulating", "unavailable"}
        # Market-wide key carries null coverage; structural key carries null config value.
        macro = payload["anomaly_detection"]["macro_surprise_percentile"]
        assert macro["silence"] is True
        assert payload["options_flow"]["pair_trade_signature"]["configured_value"] is None

    def test_json_keys_are_canonically_sorted(
        self, session: Session, config: Any, universe_size: int
    ) -> None:
        """``sort_keys`` canonicalizes ordering — record keys appear alphabetically.

        The metrics record is built in semantic order (``calibrated`` before
        ``accumulating``); ``sort_keys=True`` re-orders it alphabetically in the
        serialized text, so this fails if ``sort_keys`` is dropped.
        """
        _seed_invocations(session, 1)
        report = compute_flag_rate_report(
            session=session,
            config=config,
            universe_size=universe_size,
            now=_NOW,
            window_days=_WINDOW_DAYS,
        )
        rendered = format_report_json(report)
        assert rendered.index('"accumulating"') < rendered.index('"calibrated"')


# ---------------------------------------------------------------------------
# CLI exit code
# ---------------------------------------------------------------------------


class TestCli:
    def test_main_returns_zero_on_empty_window(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from alphamind.scripts import report_flag_rates as mod

        _seed_invocations(session, 2)

        def _fake_engine(_path: str | None) -> Any:
            return session.bind

        monkeypatch.setattr(mod, "make_engine", _fake_engine)
        assert main(["--window-days", str(_WINDOW_DAYS)]) == 0

    def test_main_emits_json(
        self,
        session: Session,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from alphamind.scripts import report_flag_rates as mod

        _seed_invocations(session, 2)

        def _fake_engine(_path: str | None) -> Any:
            return session.bind

        monkeypatch.setattr(mod, "make_engine", _fake_engine)
        rc = main(["--output", "json"])
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert set(payload) == set(all_threshold_classes())
