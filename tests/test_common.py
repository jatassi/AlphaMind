"""
Tests for src/alphamind/data_sources/_common.py — story 04.

Each test targets one acceptance criterion.  No real HTTP, no real sleep —
all network and time dependencies are injected or mocked.
"""

from __future__ import annotations

import shutil
import threading
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind._kernel.ids import Symbol
from alphamind.data_sources._common import (
    RateLimiter,
    RetryShape,
    active_universe_tickers,
    load_config,
    resume_since,
    track_run,
    with_retries,
)

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _no_sleep(_seconds: float) -> None:
    """Stand-in for time.sleep used by retry decorators."""


def _make_http_error(*, code: int, msg: str) -> urllib.error.HTTPError:
    """Construct a urllib.error.HTTPError with test-fixture defaults."""
    return urllib.error.HTTPError(
        url="https://api.stlouisfed.org/fred/series",
        code=code,
        msg=msg,
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


class _FakeRunRepo:
    """In-memory stand-in for the persistence layer used by track_run."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        self.rows[run_id] = {
            "run_id": run_id,
            "collector": collector,
            "started_at": started_at,
            "status": "running",
            "completed_at": None,
            "rows_written": None,
            "error_summary": None,
        }

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        self.rows[run_id].update(
            status="success",
            completed_at=completed_at,
            rows_written=rows_written,
        )

    def update_failed(self, run_id: str, error_summary: str) -> None:
        self.rows[run_id].update(
            status="failed",
            error_summary=error_summary,
            completed_at=datetime.now(UTC).isoformat(),
        )


# ---------------------------------------------------------------------------
# AC: _common.py exports the required names
# ---------------------------------------------------------------------------


class TestPublicInterface:
    def test_exports_load_config(self) -> None:
        from alphamind.data_sources._common import load_config as lc

        assert callable(lc)

    def test_exports_retry_shape_enum(self) -> None:
        from alphamind.data_sources._common import RetryShape

        assert RetryShape.critical is not None
        assert RetryShape.important is not None
        assert RetryShape.optional is not None

    def test_exports_with_retries(self) -> None:
        from alphamind.data_sources._common import with_retries as wr

        assert callable(wr)

    def test_exports_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter

        assert callable(RateLimiter)

    def test_exports_track_run(self) -> None:
        from alphamind.data_sources._common import track_run as tr

        assert callable(tr)


# ---------------------------------------------------------------------------
# AC: load_config — reads all three YAML files and returns immutable object
# ---------------------------------------------------------------------------


_DATA_SOURCES_YAML = """
providers:
  polygon:
    api_key_env: POLYGON_API_KEY
    rate_limit_per_minute: 100
    retry_shape: critical
retry_shapes:
  critical: {attempts: 3, backoff: exponential, failover: true}
  important: {attempts: 2, backoff: exponential, failover: false}
  optional:  {attempts: 1, backoff: none, failover: false}
categories:
  q1_price_volume:
    tier: critical
    freshness_max_seconds: 300
    primary: polygon
    failover: []
"""

_COLLECTOR_SCHEDULE_YAML = (
    'timezone: US/Eastern\ncollectors:\n  polygon.equity: {cron: "*/15 9-16 * * mon-fri"}\n'
)

_NEWS_OUTLETS_YAML = "outlets:\n  Reuters: {tier: tier_1}\n"


def _seed_canonical_config_dir(tmp_path: Path) -> None:
    """Drop a minimal valid YAML tree covering every file ``load_config`` reads."""
    (tmp_path / "data_sources.yaml").write_text(_DATA_SOURCES_YAML, encoding="utf-8")
    (tmp_path / "collector_schedule.yaml").write_text(_COLLECTOR_SCHEDULE_YAML, encoding="utf-8")
    (tmp_path / "news_outlets.yaml").write_text(_NEWS_OUTLETS_YAML, encoding="utf-8")
    repo_root = Path(__file__).parent.parent
    shutil.copy(repo_root / "config" / "distillation.yaml", tmp_path / "distillation.yaml")


class TestLoadConfig:
    def test_returns_immutable_config_with_all_sections(self, tmp_path: Path) -> None:
        """load_config reads every data-layer YAML and freezes the aggregate."""
        env_file = tmp_path / ".env"
        env_file.write_text("POLYGON_API_KEY=test\n")
        _seed_canonical_config_dir(tmp_path)

        cfg = load_config(config_dir=str(tmp_path), env_file=str(env_file))

        assert cfg.collector_schedule is not None
        assert cfg.data_sources is not None
        assert cfg.distillation is not None
        assert cfg.news_outlets is not None
        # Immutable: Pydantic model instance cannot be modified after creation
        with pytest.raises((TypeError, AttributeError, ValueError)):
            cfg.data_sources = None  # type: ignore[misc,assignment]  # tests read-only enforcement

    def test_load_config_includes_distillation_aggregate(self, tmp_path: Path) -> None:
        """load_config exposes ``distillation`` parsed from distillation.yaml."""
        from alphamind.config.models import DistillationConfig

        env_file = tmp_path / ".env"
        env_file.write_text("POLYGON_API_KEY=test\n")
        _seed_canonical_config_dir(tmp_path)

        cfg = load_config(config_dir=str(tmp_path), env_file=str(env_file))
        assert isinstance(cfg.distillation, DistillationConfig)
        # Round-trip a representative scalar to prove the YAML traversed parse-time.
        assert cfg.distillation.anomaly_detection.volume_anomaly_sigma == 2.5

    def test_missing_required_env_var_raises_named_error(self, tmp_path: Path) -> None:
        """A missing *_env reference must raise an error that names the variable."""
        env_file = tmp_path / ".env"
        env_file.write_text("")  # empty — POLYGON_API_KEY is missing
        _seed_canonical_config_dir(tmp_path)

        with pytest.raises(Exception, match="POLYGON_API_KEY"):
            load_config(config_dir=str(tmp_path), env_file=str(env_file))


# ---------------------------------------------------------------------------
# AC: with_retries — critical shape (multiple attempts, exponential backoff)
# ---------------------------------------------------------------------------


class TestWithRetriesCritical:
    def test_retries_multiple_times_on_retryable_error(self) -> None:
        """critical shape retries up to max_attempts on httpx.TimeoutException."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            return "ok"

        result = flaky()
        assert result == "ok"
        assert call_count == 3

    def test_critical_exhausts_all_attempts_then_raises(self) -> None:
        """After all attempts fail, the last exception propagates."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def always_fails() -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("timeout")

        with pytest.raises(httpx.TimeoutException):
            always_fails()

        # critical shape = 3 attempts
        assert call_count == 3

    def test_critical_propagates_non_retryable_immediately(self) -> None:
        """4xx auth error (HTTPStatusError with 401) must not be retried."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def auth_failure() -> None:
            nonlocal call_count
            call_count += 1
            request = httpx.Request("GET", "https://api.example.com/data")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

        with pytest.raises(httpx.HTTPStatusError):
            auth_failure()

        assert call_count == 1  # no retries

    def test_critical_retries_on_5xx(self) -> None:
        """5xx server error is retryable."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def server_error() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                request = httpx.Request("GET", "https://api.example.com/data")
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError(
                    "503 Service Unavailable", request=request, response=response
                )
            return "ok"

        result = server_error()
        assert result == "ok"
        assert call_count == 2

    def test_critical_retries_on_429(self) -> None:
        """429 rate-limit response is retryable."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def rate_limited() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                request = httpx.Request("GET", "https://api.example.com/data")
                response = httpx.Response(429, request=request)
                raise httpx.HTTPStatusError(
                    "429 Too Many Requests", request=request, response=response
                )
            return "ok"

        result = rate_limited()
        assert result == "ok"
        assert call_count == 2

    def test_critical_retries_on_urllib_5xx(self) -> None:
        """fredapi uses urllib directly; urllib.error.HTTPError 5xx is retryable."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def server_error() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise _make_http_error(code=500, msg="Internal Server Error")
            return "ok"

        result = server_error()
        assert result == "ok"
        assert call_count == 2

    def test_critical_propagates_urllib_4xx_immediately(self) -> None:
        """urllib.error.HTTPError 4xx (auth) must not be retried."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def unauthorized() -> str:
            nonlocal call_count
            call_count += 1
            raise _make_http_error(code=401, msg="Unauthorized")

        with pytest.raises(urllib.error.HTTPError):
            unauthorized()
        assert call_count == 1

    def test_critical_retries_on_fredapi_value_error_5xx(self) -> None:
        """fredapi catches urllib HTTPError and re-raises as ValueError; 5xx is retryable."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def fred_500() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("Internal Server Error")
            return "ok"

        result = fred_500()
        assert result == "ok"
        assert call_count == 2

    def test_critical_propagates_unrelated_value_error(self) -> None:
        """A ValueError without a transient-HTTP message must not be retried."""
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def bad_input() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid literal for int()")

        with pytest.raises(ValueError):
            bad_input()
        assert call_count == 1

    def test_critical_retries_on_fredapi_value_error_none_with_502_context(self) -> None:
        # When FRED is fronted by a CDN that returns an HTML 5xx error page,
        # fredapi cannot parse a `message` field and raises `ValueError(None)`
        # with the underlying HTTPError on `__context__`. Matcher must retry.
        call_count = 0

        @with_retries(RetryShape.critical, _sleep=_no_sleep)
        def cdn_502() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                val_err = ValueError(None)
                val_err.__context__ = _make_http_error(code=502, msg="Bad Gateway")
                raise val_err
            return "ok"

        result = cdn_502()
        assert result == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# AC: with_retries — important shape (limited attempts)
# ---------------------------------------------------------------------------


class TestWithRetriesImportant:
    def test_important_retries_limited_attempts(self) -> None:
        """important shape has 2 attempts (1 retry)."""
        call_count = 0

        @with_retries(RetryShape.important, _sleep=_no_sleep)
        def always_fails() -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("timeout")

        with pytest.raises(httpx.TimeoutException):
            always_fails()

        assert call_count == 2  # 2 attempts total

    def test_important_propagates_non_retryable_immediately(self) -> None:
        """4xx is propagated immediately from important shape too."""
        call_count = 0

        @with_retries(RetryShape.important, _sleep=_no_sleep)
        def auth_failure() -> None:
            nonlocal call_count
            call_count += 1
            request = httpx.Request("GET", "https://api.example.com/data")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("403 Forbidden", request=request, response=response)

        with pytest.raises(httpx.HTTPStatusError):
            auth_failure()

        assert call_count == 1


# ---------------------------------------------------------------------------
# AC: with_retries — optional shape (single retry)
# ---------------------------------------------------------------------------


class TestWithRetriesOptional:
    def test_optional_makes_one_retry_on_retryable_failure(self) -> None:
        """optional shape = 1 retry (2 total attempts), then raises."""
        call_count = 0

        @with_retries(RetryShape.optional, _sleep=_no_sleep)
        def flaky() -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("timeout")

        with pytest.raises(httpx.TimeoutException):
            flaky()

        # optional has attempts=1 per spec, meaning 1 base attempt + 1 retry = 2 total
        # BUT the design doc says "single retry" = try twice total.
        # DataSourcesConfig retry_shapes.optional.attempts = 1 means 1 retry, so 2 total calls.
        assert call_count == 2

    def test_optional_propagates_non_retryable_immediately(self) -> None:
        call_count = 0

        @with_retries(RetryShape.optional, _sleep=_no_sleep)
        def auth_failure() -> None:
            nonlocal call_count
            call_count += 1
            request = httpx.Request("GET", "https://api.example.com/data")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

        with pytest.raises(httpx.HTTPStatusError):
            auth_failure()

        assert call_count == 1

    def test_optional_succeeds_after_one_retry(self) -> None:
        call_count = 0

        @with_retries(RetryShape.optional, _sleep=_no_sleep)
        def sometimes_fails() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.TimeoutException("timeout")
            return "ok"

        result = sometimes_fails()
        assert result == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# AC: with_retries — vendor_outage_extended shape (4 attempts, 5s/15s/45s)
# ---------------------------------------------------------------------------


class TestWithRetriesVendorOutageExtended:
    """ALP-285: tier added for FRED-style vendors with multi-minute 5xx outages."""

    def test_vendor_outage_extended_has_four_attempts(self) -> None:
        """4 attempts total (3 retries) per the documented schedule."""
        call_count = 0

        @with_retries(RetryShape.vendor_outage_extended, _sleep=_no_sleep)
        def always_fails() -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.TimeoutException("timeout")

        with pytest.raises(httpx.TimeoutException):
            always_fails()

        assert call_count == 4

    def test_vendor_outage_extended_uses_documented_backoff(self) -> None:
        """Sleeps follow 5s, 15s, 45s — the schedule documented on RetryShape."""
        sleeps: list[float] = []

        @with_retries(RetryShape.vendor_outage_extended, _sleep=sleeps.append)
        def always_fails() -> None:
            raise httpx.TimeoutException("timeout")

        with pytest.raises(httpx.TimeoutException):
            always_fails()

        assert sleeps == [5.0, 15.0, 45.0]

    def test_vendor_outage_extended_recovers_after_two_failures(self) -> None:
        """Succeeds on attempt 3 → returns the value, no further sleeps."""
        call_count = 0
        sleeps: list[float] = []

        @with_retries(RetryShape.vendor_outage_extended, _sleep=sleeps.append)
        def server_error() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _make_http_error(code=502, msg="Bad Gateway")
            return "ok"

        assert server_error() == "ok"
        assert call_count == 3
        assert sleeps == [5.0, 15.0]

    def test_vendor_outage_extended_retries_on_url_error(self) -> None:
        """Connection-reset / refused surface as URLError without status — retry."""
        call_count = 0

        @with_retries(RetryShape.vendor_outage_extended, _sleep=_no_sleep)
        def reset_then_ok() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.URLError("Connection reset by peer")
            return "ok"

        assert reset_then_ok() == "ok"
        assert call_count == 2

    def test_vendor_outage_extended_retries_on_connection_reset(self) -> None:
        """Bare ConnectionResetError (no URLError wrapping) is also retryable."""
        call_count = 0

        @with_retries(RetryShape.vendor_outage_extended, _sleep=_no_sleep)
        def reset_then_ok() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("peer reset")
            return "ok"

        assert reset_then_ok() == "ok"
        assert call_count == 2


# ---------------------------------------------------------------------------
# AC: RateLimiter — blocks when budget exhausted
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_acquire_allows_calls_within_budget(self) -> None:
        """Calls within the per-minute budget complete without blocking."""
        limiter = RateLimiter()
        limiter.set_limit("polygon", rate_per_minute=60)

        start = time.monotonic()
        # Acquire 5 tokens well within a 60/min budget (plenty of capacity)
        for _ in range(5):
            limiter.acquire("polygon")
        elapsed = time.monotonic() - start

        # Should complete very quickly — well under 1 second
        assert elapsed < 1.0

    def test_acquire_blocks_when_budget_exhausted(self) -> None:
        """When all tokens are consumed, acquire blocks until refill."""
        # 6 requests/min → one token every 10 seconds.
        # Consume 6 tokens instantly to drain the bucket, then the 7th should block.
        # We use a fake clock to avoid actually sleeping.
        limiter = RateLimiter()
        limiter.set_limit("slow", rate_per_minute=6)

        # Manually drain the bucket by advancing time
        # We'll mock time.monotonic to fast-forward
        base = time.monotonic()
        tick = [0.0]

        def fake_time() -> float:
            return base + tick[0]

        def advance(seconds: float) -> None:
            tick[0] += seconds

        with patch(
            "alphamind.data_sources._common.rate_limit.time.monotonic",
            side_effect=fake_time,
        ):
            # Drain all tokens (bucket starts full at capacity = rate_per_minute)
            # After drain, next acquire must wait
            for _ in range(6):
                limiter.acquire("slow")

            # Now advance time by just less than one refill interval
            advance(9.0)  # 9s < 10s (one token interval at 6/min)

            blocked = threading.Event()
            released = threading.Event()

            def try_acquire() -> None:
                blocked.set()
                limiter.acquire("slow")
                released.set()

            t = threading.Thread(target=try_acquire)
            t.start()
            blocked.wait(timeout=1.0)

            # Not released yet (still need to wait)
            assert not released.is_set()

            # Advance time past the refill threshold
            advance(2.0)  # now at 11s > 10s

            t.join(timeout=2.0)
            assert released.is_set()

    def test_acquire_is_thread_safe_under_concurrent_calls(self) -> None:
        """50 concurrent threads racing on acquire() produce no over-issuance.

        Uses a very high rate limit so all threads complete quickly.
        The post-condition: exactly n tokens are consumed from the bucket,
        not more — i.e. the token counter is never incremented by two threads
        simultaneously for the same token.
        """
        n = 50
        # High rate so tokens are available immediately and threads don't block
        rate = 6000  # tokens per minute = 100/sec — plenty for 50 threads
        limiter = RateLimiter()
        limiter.set_limit("concurrent_provider", rate_per_minute=rate)

        acquired: list[str] = []
        list_lock = threading.Lock()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                limiter.acquire("concurrent_provider")
                with list_lock:
                    acquired.append(threading.current_thread().name)
            except Exception as e:
                with list_lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, name=f"w{i}") for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert not errors, f"Thread errors: {errors}"
        # Every thread completed successfully — no deadlock, no over-issuance
        assert len(acquired) == n
        # No thread acquired twice (each thread name is unique)
        assert len(set(acquired)) == n


# ---------------------------------------------------------------------------
# AC: track_run — context manager behaviors
# ---------------------------------------------------------------------------


class TestTrackRun:
    def _make_repo(self) -> _FakeRunRepo:
        return _FakeRunRepo()

    def test_enter_inserts_running_row(self) -> None:
        """On enter, track_run inserts a row with status='running'."""
        repo = self._make_repo()
        with track_run("polygon.equity", _repo=repo) as run:
            assert len(repo.rows) == 1
            row = next(iter(repo.rows.values()))
            assert row["status"] == "running"
            assert row["collector"] == "polygon.equity"
            run.rows_written = 0  # normal exit

    def test_success_updates_status_and_rows_written(self) -> None:
        """On normal exit, updates to status='success' with rows_written."""
        repo = self._make_repo()
        with track_run("polygon.equity", _repo=repo) as run:
            run.rows_written = 42

        row = next(iter(repo.rows.values()))
        assert row["status"] == "success"
        assert row["rows_written"] == 42
        assert row["completed_at"] is not None

    def test_success_sets_completed_at(self) -> None:
        """completed_at is set on success."""
        repo = self._make_repo()
        with track_run("polygon.equity", _repo=repo) as run:
            run.rows_written = 0

        row = next(iter(repo.rows.values()))
        assert row["completed_at"] is not None

    def test_exception_updates_to_failed_and_reraises(self) -> None:
        """On exception, updates to status='failed' and re-raises."""
        repo = self._make_repo()
        with pytest.raises(RuntimeError, match="boom"), track_run("polygon.equity", _repo=repo):
            raise RuntimeError("boom")

        row = next(iter(repo.rows.values()))
        assert row["status"] == "failed"
        assert row["error_summary"] is not None
        assert "boom" in row["error_summary"]

    def test_failed_row_has_error_summary(self) -> None:
        """error_summary field is populated on failure."""
        repo = self._make_repo()
        with pytest.raises(ValueError), track_run("fred.macro", _repo=repo):
            raise ValueError("bad data from api")

        row = next(iter(repo.rows.values()))
        assert "bad data from api" in row["error_summary"]

    def test_failed_row_has_completed_at(self) -> None:
        """completed_at is set on failure too — operators need a fixed end-time."""
        repo = self._make_repo()
        with pytest.raises(RuntimeError), track_run("polygon.equity", _repo=repo):
            raise RuntimeError("boom")

        row = next(iter(repo.rows.values()))
        assert row["status"] == "failed"
        assert row["completed_at"] is not None

    def test_run_object_has_rows_written_attribute(self) -> None:
        """The run object yielded by track_run has a mutable rows_written."""
        repo = self._make_repo()
        with track_run("polygon.equity", _repo=repo) as run:
            assert hasattr(run, "rows_written")
            run.rows_written = 100

        row = next(iter(repo.rows.values()))
        assert row["rows_written"] == 100


# ---------------------------------------------------------------------------
# resume_since / active_universe_tickers helpers
# ---------------------------------------------------------------------------


class TestResumeSince:
    """resume_since computes a 'since' value from MAX(column) - overlap."""

    @pytest.fixture
    def session_factory(self) -> sessionmaker[Session]:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        from alphamind.persistence.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return _sessionmaker(bind=engine, expire_on_commit=False)

    def test_falls_back_to_default_lookback_when_table_empty(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        from alphamind.persistence.models import MacroObservations

        result = resume_since(
            column=MacroObservations.observation_date,
            default_lookback=_td(days=7),
            session_factory=session_factory,
        )
        # Within a few seconds of "now - 7d"
        delta = abs((_dt.now(result.tzinfo) - _td(days=7)) - result).total_seconds()
        assert delta < 5

    def test_returns_max_minus_overlap(self, session_factory: sessionmaker[Session]) -> None:
        from datetime import timedelta as _td

        from alphamind.persistence.models import MacroObservations

        with session_factory() as sess:
            sess.add(
                MacroObservations(
                    source="fred",
                    series_id="DGS10",
                    observation_date="2026-04-20",
                    revision_number=0,
                    value=4.3,
                    units="pct",
                    frequency="daily",
                    ingested_at="2026-04-20T00:00:00+00:00",
                )
            )
            sess.commit()

        result = resume_since(
            column=MacroObservations.observation_date,
            filters=(MacroObservations.source == "fred",),
            default_lookback=_td(days=30),
            overlap=_td(days=2),
            session_factory=session_factory,
        )
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 18  # 2026-04-20 minus 2-day overlap

    def test_filters_isolate_per_source(self, session_factory: sessionmaker[Session]) -> None:
        """A filter on source='fred' ignores rows from other sources."""
        from datetime import timedelta as _td

        from alphamind.persistence.models import MacroObservations

        with session_factory() as sess:
            sess.add_all(
                [
                    MacroObservations(
                        source="bls",
                        series_id="X",
                        observation_date="2030-01-01",
                        revision_number=0,
                        value=1.0,
                        units="pct",
                        frequency="monthly",
                        ingested_at="2030-01-01T00:00:00+00:00",
                    ),
                ]
            )
            sess.commit()

        # Filtering on source='fred' should NOT see the bls row
        result = resume_since(
            column=MacroObservations.observation_date,
            filters=(MacroObservations.source == "fred",),
            default_lookback=_td(days=1),
            session_factory=session_factory,
        )
        from datetime import datetime as _dt

        assert (_dt.now(result.tzinfo) - result).days < 2  # used the default lookback


class TestActiveUniverseTickers:
    """active_universe_tickers reads asset_universe with role filters."""

    @pytest.fixture
    def session_factory(self) -> sessionmaker[Session]:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        from alphamind.persistence.models import AssetUniverse, Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        sf = _sessionmaker(bind=engine, expire_on_commit=False)
        with sf() as sess:
            sess.add_all(
                [
                    AssetUniverse(
                        asset_id="1",
                        ticker=Symbol("AAPL"),
                        full_name="Apple",
                        asset_class="equity",
                        asset_role="universe",
                        exchange="NASDAQ",
                        is_active=1,
                        added_date="2026-01-01",
                        last_updated="2026-01-01T00:00:00+00:00",
                    ),
                    AssetUniverse(
                        asset_id="2",
                        ticker=Symbol("SPY"),
                        full_name="SPY",
                        asset_class="equity",
                        asset_role="broad_market",
                        exchange="NYSE",
                        is_active=1,
                        added_date="2026-01-01",
                        last_updated="2026-01-01T00:00:00+00:00",
                    ),
                    AssetUniverse(
                        asset_id="3",
                        ticker=Symbol("DELISTED"),
                        full_name="DELISTED",
                        asset_class="equity",
                        asset_role="universe",
                        exchange="NASDAQ",
                        is_active=0,
                        added_date="2020-01-01",
                        last_updated="2020-01-01T00:00:00+00:00",
                    ),
                ]
            )
            sess.commit()
        return sf

    def test_includes_universe_and_benchmarks_by_default(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        result = active_universe_tickers(session_factory=session_factory)
        assert set(result) == {"AAPL", "SPY"}

    def test_excludes_benchmarks_when_requested(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        result = active_universe_tickers(include_benchmarks=False, session_factory=session_factory)
        assert result == ["AAPL"]

    def test_skips_inactive_rows(self, session_factory: sessionmaker[Session]) -> None:
        result = active_universe_tickers(session_factory=session_factory)
        assert "DELISTED" not in result
